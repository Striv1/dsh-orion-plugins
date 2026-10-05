"""Controlled workflow access to the existing 3081 persistent document queue.

This is a queue producer, not another scheduler or a browser-auth workaround.
Only an existing S0 project and a verified reference or upload batch may be queued.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.ingestion.s0_batch_executor import (
    atomic_json,
    reference_snapshot_expectations,
    upload_snapshot_expectations,
)
from services.ingestion.source_preflight import TABULAR_SUFFIXES, inspect_source
from services.ontology_engineering import WorkflowError, WorkflowGateError
from services.ontology_engineering.source_scope import SourceScopeError, validate_document_sources
from services.ontology_engineering.stage_contracts import (
    STAGE_CONTRACT_VERSION,
    project_stage_contract_version,
)

JOB_ID = re.compile(r"^JOB-[A-Z0-9-]{8,80}$")


def document_input_root() -> Path:
    return (
        Path(
            os.getenv("ORION_DOCUMENT_INGESTION_ROOT")
            or os.getenv("ORION_DOCUMENT_INPUT_ROOT")
            or Path(__file__).resolve().parents[2] / "data/unstructured"
        )
        .expanduser()
        .resolve()
    )


def verified_document_source(root: Path, source_path: str) -> dict[str, Any]:
    relative = Path(source_path)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != ".orion-s0-uploads"
        or not re.fullmatch(r"(?:REFERENCE|UPLOAD)-[A-Z0-9-]{8,80}", relative.name)
    ):
        raise WorkflowError(
            "只接受 snapshot_workspace_sources 返回的 REFERENCE 或浏览器上传返回的 UPLOAD 完整受控批次。"
        )
    try:
        snapshot = (
            upload_snapshot_expectations(root, source_path)
            if relative.name.startswith("UPLOAD-")
            else reference_snapshot_expectations(root, source_path)
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    if snapshot is None:
        raise WorkflowError("缺少不可变资料快照或受控上传回执。")
    return snapshot


def document_source_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Serializable selection evidence; it does not grant an existing project scope."""
    return {
        "kind": snapshot.get("source_kind", "REFERENCE"),
        "manifest_sha256": snapshot["manifest_sha256"],
        "files": [
            {key: item[key] for key in ("path", "bytes", "sha256")}
            for item in snapshot["files"].values()
        ],
    }


def document_source_scope(snapshot: dict[str, Any], *, source_path: str) -> dict[str, Any]:
    """Create file declarations from a verified batch without inventing source IDs.

    Content-derived document IDs can repeat for distinct file paths, so exact
    path/name/hash identities are the portable creation contract.
    """
    return {
        "sources": [
            {
                "kind": "STRUCTURED_FILE"
                if Path(item["path"]).suffix.lower() in TABULAR_SUFFIXES
                else "DOCUMENT",
                "source_path": (Path(source_path) / item["path"]).as_posix(),
                "path_scope": "FILE",
                "source_name": Path(item["path"]).name,
                "source_sha256": item["sha256"],
            }
            for item in snapshot["files"].values()
        ]
    }


def validate_declared_document_source_identities(source_scope: Any) -> None:
    """Reject invented IDs before MCP creates an exactly identified batch file."""
    if not isinstance(source_scope, dict):
        return
    batches: dict[str, dict[str, Any]] = {}
    for row in source_scope.get("sources") or []:
        if not isinstance(row, dict) or not row.get("source_id"):
            continue
        path = Path(str(row.get("source_path") or ""))
        if (
            row.get("kind") not in {"DOCUMENT", "STRUCTURED_FILE"}
            or row.get("path_scope", "FILE") != "FILE"
            or path.is_absolute()
            or len(path.parts) < 3
            or path.parts[0] != ".orion-s0-uploads"
            or not re.fullmatch(r"(?:REFERENCE|UPLOAD)-[A-Z0-9-]{8,80}", path.parts[1])
        ):
            continue
        batch = Path(*path.parts[:2]).as_posix()
        if batch not in batches:
            snapshot = verified_document_source(document_input_root(), batch)
            batches[batch] = {
                item["source_path"]: item
                for item in document_source_scope(snapshot, source_path=batch)["sources"]
            }
        actual = batches[batch].get(path.as_posix())
        actual_id = (
            f"DOC-{actual['source_sha256'].removeprefix('sha256:')[:16].upper()}"
            if actual
            else None
        )
        if (
            not actual
            or row["source_id"] != actual_id
            or any(
                row.get(key) and row[key] != actual[key]
                for key in ("source_name", "source_sha256", "kind")
            )
        ):
            raise WorkflowGateError(
                "G-S0-SOURCE-SCOPE",
                "文件来源编号或身份与受控批次不一致；请直接使用 snapshot/preflight 返回的 source_scope，不得自编 source_id。工程尚未创建。",
            )


def _document_job_review(directory: Path, status: dict[str, Any]) -> dict[str, Any]:
    """Expose actual queue outputs for review; never synthesize formal S0 assets."""
    def existing_file(relative: str) -> Path | None:
        path = directory / relative
        if Path(relative).is_absolute() or path.is_symlink():
            return None
        resolved = path.resolve()
        return resolved if resolved.is_relative_to(directory.resolve()) and resolved.is_file() else None

    artifacts = {}
    for name in ("batch-review.html", "document-register.json", "ingestion-quality-report.json", "evidence-index.json"):
        path = existing_file(name)
        if path:
            artifacts[name] = str(path)
    result: dict[str, Any] = {
        "location": "QUEUE_OUTPUTS",
        "job_directory": str(directory.resolve()),
        "artifacts": artifacts,
        "notice": "这些是受控队列中的解析复核产物。READY_FOR_REVIEW 表示等待复核，尚未正式提交 S0；"
                  "不要在工程 00-document-evidence 下猜测尚未提交的路径。仅使用此处实际返回的路径，"
                  "下方摘录不替代全文、警告和质量报告检查。",
        "next_action": "REVIEW_QUEUE_OUTPUTS_THEN_COMMIT" if status.get("status") == "READY_FOR_REVIEW"
                       else "READ_COMMIT_RESULT" if status.get("status") == "COMMITTED"
                       else "INSPECT_FAILURE" if status.get("status") == "FAILED" else "WAIT_FOR_PARSING",
        "next_tool": "commit_document_ingestion_job" if status.get("status") == "READY_FOR_REVIEW"
                     else "get_document_ingestion_job",
    }
    errors = []
    for name, key in (("ingestion-quality-report.json", "quality_summary"), ("document-register.json", "documents")):
        path = existing_file(name)
        if not path:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if key == "quality_summary" and isinstance(value, dict):
                result[key] = value
            elif key == "documents" and isinstance(value, list):
                documents = []
                for item in value[:5]:
                    if not isinstance(item, dict):
                        continue
                    document = {field: item.get(field) for field in (
                        "document_id", "source_name", "source_sha256", "processing_method",
                        "structured_markdown_sha256",
                    )}
                    relative = item.get("structured_markdown_path")
                    markdown = existing_file(relative) if isinstance(relative, str) and relative else None
                    document["structured_markdown_path"] = str(markdown) if markdown else None
                    if markdown:
                        with markdown.open(encoding="utf-8") as content:
                            preview = content.read(1201)
                        document["preview"] = preview[:1200]
                        document["preview_truncated"] = len(preview) > 1200
                    else:
                        document["read_error"] = "当前队列内无可安全回读的 Markdown 产物。"
                    documents.append(document)
                result.update(documents=documents, document_count=len(value), documents_truncated=len(value) > 5)
            else:
                errors.append(f"{name} 格式无效。")
        except (OSError, ValueError):
            errors.append(f"{name} 当前不可读取；不能视为已完成复核。")
    if errors:
        result["read_errors"] = errors
    return result


def read_document_job(*, project_id: str, job_id: str, root: Path | None = None) -> dict[str, Any]:
    if not JOB_ID.fullmatch(job_id):
        raise WorkflowError("job_id 格式无效。")
    queue = (root or document_input_root()) / ".orion-s0-jobs"
    directory = queue / job_id
    if directory.is_symlink() or directory.resolve().parent != queue.resolve():
        raise WorkflowError("资料任务路径不在受控队列内。")
    try:
        request = json.loads((directory / "request.json").read_text())
        status = json.loads((directory / "status.json").read_text())
        runner = json.loads((directory / "runner.json").read_text())
    except (OSError, ValueError) as exc:
        raise WorkflowError("资料任务不存在或状态尚不可读取。") from exc
    if request.get("project_id") != project_id:
        raise WorkflowError("资料任务不属于当前工程。")
    return {
        **status,
        "project_id": project_id,
        "project_revision": request.get("project_revision"),
        "runner_state": runner.get("state"),
        "runner_pid": runner.get("pid"),
        "heartbeat_at": runner.get("heartbeat_at"),
        "queue_position": runner.get("queue_position"),
        "review": _document_job_review(directory, status),
    }


def enqueue_document_job(
    service: Any,
    *,
    project_id: str,
    source_path: str,
    expected_revision: int,
    actor: str,
    max_runtime_seconds: int = 1800,
) -> dict[str, Any]:
    root = document_input_root()
    relative = Path(source_path)
    if (
        not actor.strip()
        or type(max_runtime_seconds) is not int
        or not 1 <= max_runtime_seconds <= 14400
    ):
        raise WorkflowError("actor 不能为空，max_runtime_seconds 必须在 1–14400 之间。")
    reference = verified_document_source(root, source_path)
    with service._project_mutation_lock(project_id) as project_dir:
        _, state = service._require_stage(project_id, "S0")
        service._require_expected_revision(state, expected_revision)
        mode = str(state.get("intake_mode") or "")
        if mode not in {"DOCUMENT_ONLY", "HYBRID"}:
            raise WorkflowError("DATABASE_ONLY 工程不执行文档解析，请使用数据库接入入口。")
        scope_preflight = None
        if project_stage_contract_version(state) == STAGE_CONTRACT_VERSION:
            scope_path = project_dir / "00-document-evidence/source-scope.json"
            if not scope_path.is_file():
                raise WorkflowGateError(
                    "G-S0-SOURCE-SCOPE", "新版工程缺少正式来源范围，不能开始资料解析。"
                )
            # Use the existing, content-verified batch manifest. File paths
            # are relative to the ingestion root, exactly as in S0's register.
            # This precedes inspect_source, which may open workbooks for profiling.
            declared_documents = [
                {
                    "document_id": f"DOC-{item['sha256'].removeprefix('sha256:')[:16].upper()}",
                    "source_path": (relative / item["path"]).as_posix(),
                    "source_name": Path(item["path"]).name,
                    "source_sha256": item["sha256"],
                }
                for item in reference["files"].values()
            ]
            try:
                scope_preflight = {
                    **validate_document_sources(service._read_json(scope_path), declared_documents),
                    "validation_scope": f"{reference.get('source_kind', 'REFERENCE')}_MANIFEST_ONLY",
                    "manifest_sha256": reference["manifest_sha256"],
                }
            except SourceScopeError as exc:
                raise WorkflowGateError(exc.gate, str(exc)) from exc
        preflight = inspect_source(root / relative)
        if preflight.get("requires_structured_import") and mode != "HYBRID":
            raise WorkflowError(
                "资料含结构化大表，需先确认 HYBRID 接入范围；不会隐式改变工程模式。"
            )
        queue = root / ".orion-s0-jobs"
        queue.mkdir(parents=True, exist_ok=True)
        for request_path in queue.glob("JOB-*/request.json"):
            try:
                previous = json.loads(request_path.read_text())
            except (OSError, ValueError):
                continue
            if (
                previous.get("project_id") == project_id
                and previous.get("project_revision") == expected_revision
                and previous.get("source_path") == relative.as_posix()
            ):
                result = read_document_job(
                    project_id=project_id, job_id=request_path.parent.name, root=root
                )
                atomic_json(
                    project_dir / ".s0-document-job.json",
                    {"job_id": request_path.parent.name, "project_revision": expected_revision},
                )
                return {**result, "reused_existing_job": True}
        project = service._read_json(project_dir / "project.json")
        queued_at = datetime.now(UTC).isoformat()
        job_id = "JOB-" + uuid4().hex.upper()
        request = {
            "job_id": job_id,
            "created_at": queued_at,
            "queued_at": queued_at,
            "project_id": project_id,
            "project_revision": expected_revision,
            "project_name": project.get("project_name"),
            "domain": project.get("domain"),
            "intake_mode": mode,
            "requested_intake_mode": mode,
            "intake_rationale": project.get("intake_rationale"),
            "actor": actor,
            "project_request_id": f"s0:{project_id}:{expected_revision}:{relative.name}",
            "source_path": relative.as_posix(),
            "input_root": str(root),
            "workflow_home": str(service.root),
            "source_preflight": preflight,
            "source_snapshot": document_source_manifest(reference),
            "source_scope_preflight": scope_preflight,
            "recommended_structured_data_action": preflight.get(
                "recommended_structured_data_action"
            ),
            "structure_endpoint": os.getenv(
                "ORION_PADDLEOCR_STRUCTURE_URL", "http://127.0.0.1:10826/mcp"
            ),
            "ocr_endpoint": os.getenv("ORION_PADDLEOCR_OCR_URL", "http://127.0.0.1:10827/mcp"),
            "reuse_content_cache": False,
            "max_runtime_seconds": max_runtime_seconds,
            "retry_failed_only": False,
            "submitted_via": "ORION_WORKFLOW_MCP",
        }
        status = {
            key: request[key]
            for key in (
                "job_id",
                "project_id",
                "project_name",
                "domain",
                "source_path",
                "intake_mode",
                "project_request_id",
                "created_at",
                "queued_at",
                "source_preflight",
                "recommended_structured_data_action",
            )
        }
        status.update(
            {
                "status": "QUEUED",
                "progress_phase": "QUEUED",
                "files": [],
                "warnings": [],
                "discovered_paths": [],
                "total_files": 0,
                "completed_files": 0,
                "failed_files": 0,
                "total_pages": 0,
                "processed_pages": 0,
                "current_file": None,
                "message": "已进入 3081 持久队列，等待现有后台执行器；尚未通过 S0。",
            }
        )
        runner = {
            "state": "QUEUED",
            "queued_at": queued_at,
            "pid": None,
            "heartbeat_at": None,
            "attempt": 0,
            "recovery_count": 0,
            "max_runtime_seconds": max_runtime_seconds,
            "timeout_policy": "ADAPTIVE_WORKLOAD",
        }
        # Publish the complete queue record atomically, so the running scheduler
        # can never launch a half-written request.
        staging = Path(tempfile.mkdtemp(prefix=".enqueue-", dir=queue))
        atomic_json(staging / "request.json", request)
        atomic_json(staging / "runner.json", runner)
        atomic_json(staging / "status.json", status)
        staging.rename(queue / job_id)
        atomic_json(
            project_dir / ".s0-document-job.json",
            {"job_id": job_id, "project_revision": expected_revision},
        )
        service._append_event(
            project_dir,
            "DOCUMENT_JOB_QUEUED",
            state,
            {"stage": "S0", "job_id": job_id, "source_path": relative.as_posix(), "actor": actor},
        )
        service._refresh_manifest(project_dir)
        return {**status, "project_revision": expected_revision, "reused_existing_job": False}


def commit_document_job(
    service: Any,
    *,
    project_id: str,
    job_id: str,
    reviewed_by: str,
    rationale: str,
    expected_revision: int,
    accept_warnings: bool = False,
    structured_data_action: str = "DOCUMENT_ONLY",
) -> dict[str, Any]:
    from services.ingestion.s0_batch_executor import commit_job

    status = read_document_job(project_id=project_id, job_id=job_id)
    resume_import = status.get("status") == "COMMITTED" and structured_data_action == "IMPORT"
    bound_revision = (
        (status.get("workflow") or {}).get("revision")
        if resume_import
        else status.get("project_revision")
    )
    if bound_revision != expected_revision:
        raise WorkflowError("资料任务属于旧修订，不允许写入当前工程。")
    service._require_expected_revision(
        service._read_state(service._resolve_project(project_id)), expected_revision
    )
    if not reviewed_by.strip():
        raise WorkflowError("必须提供真实复核执行者。")
    return commit_job(
        document_input_root() / ".orion-s0-jobs" / job_id,
        reviewed_by,
        rationale,
        accept_warnings,
        structured_data_action,
        resume_import=resume_import,
    )
