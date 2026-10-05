"""Audited document selection changes before S0 acceptance.

The complete replacement batch is verified by the existing ingestion receipt
contract. Database authorizations and CQ inputs are deliberately preserved.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import Any


def replace_document_sources(service: Any, *, project_id: str, source_path: str,
                             expected_revision: int, actor: str, reason: str) -> dict[str, Any]:
    from services.ingestion.document_jobs import (
        document_input_root,
        document_source_manifest,
        document_source_scope,
        read_document_job,
        verified_document_source,
    )

    from .source_scope import SourceScopeError, normalize_source_scope
    from .workflow import WorkflowError, WorkflowGateError

    if type(expected_revision) is not int or expected_revision < 1:
        raise WorkflowError("expected_revision 必须为当前工程的正整数修订号。")
    if not actor.strip() or not reason.strip():
        raise WorkflowError("actor 和 reason 必须说明资料更正的责任人与原因。")
    with service._project_mutation_lock(project_id) as project_dir:
        _, state = service._require_stage(project_id, "S0")
        service._require_expected_revision(state, expected_revision)
        if not service._joint_design_enabled(state) or state.get("intake_mode") == "DATABASE_ONLY":
            raise WorkflowGateError("G-S0-SOURCE-SCOPE", "资料更正仅适用于新版 DOCUMENT_ONLY/HYBRID 工程；不能隐式改变接入模式或数据库授权。")
        if state.get("stage_fingerprints", {}).get("S0"):
            raise WorkflowGateError("G-S0-SOURCE-SCOPE", "已验收来源须先预览影响并正式重开 S0，不能直接替换。")
        root = document_input_root()
        marker = project_dir / ".s0-document-job.json"
        job_ids = set()
        if marker.exists():
            job_ids.add(str(service._read_json(marker).get("job_id") or ""))
        for request_path in (root / ".orion-s0-jobs").glob("JOB-*/request.json"):
            request = service._read_json(request_path)
            if request.get("project_id") == project_id:
                job_ids.add(request_path.parent.name)
        for job_id in job_ids:
            job = read_document_job(project_id=project_id, job_id=job_id, root=root)
            if job.get("status") not in {"FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "COMMITTED"} or job.get("runner_state") in {"QUEUED", "STARTING", "RUNNING"}:
                raise WorkflowGateError("G-S0-SOURCE-SCOPE", "存在活动或待复核资料任务；请先取消或完成任务，再更正来源。")
        stage_dir = project_dir / "00-document-evidence"
        scope_path = stage_dir / "source-scope.json"
        before = service._read_json(scope_path)
        project = service._read_json(project_dir / "project.json")
        if project.get("source_scope") != before:
            raise WorkflowGateError("G-S0-SOURCE-SCOPE", "工程与 S0 来源声明不一致，不能执行更正。")
        snapshot = verified_document_source(root, source_path)
        file_scope = document_source_scope(snapshot, source_path=source_path)
        desired = {**before, "sources": [s for s in before["sources"] if s["kind"] == "DATABASE"] + file_scope["sources"]}
        # Explicit exclusions remain authoritative. Conflicting replacements fail
        # the same normalizer / subsequent source gate as initial registration.
        desired["declaration"] = "EXPLICIT"
        try:
            revised = normalize_source_scope(intake_mode=state["intake_mode"], source_scope=desired)
            from .source_scope import validate_document_sources
            validate_document_sources(revised, [
                {**s, "document_id": "DOC-" + s["source_sha256"].removeprefix("sha256:")[:16].upper()}
                for s in file_scope["sources"]
            ])
        except SourceScopeError as exc:
            raise WorkflowGateError(exc.gate, str(exc)) from exc
        if revised == before:
            return {**service._status_payload(project_dir, state), "source_change": {"status": "UNCHANGED", "source_path": source_path}}
        manifest = document_source_manifest(snapshot)
        if document_source_manifest(verified_document_source(root, source_path)) != manifest:
            raise WorkflowGateError("G-S0-SOURCE-SCOPE", "更正期间资料批次已变化，未修改工程。")
        audit_dir = stage_dir / "source-changes" / f"revision-{expected_revision + 1}"
        if audit_dir.exists():
            raise WorkflowError("该修订的来源变更记录已存在，请核对工程审计。")
        preserve = {"source-scope.json", "cq-intake.json", "source-changes", "source-identity-reconciliations"}
        stale = [p for p in stage_dir.iterdir() if p.name not in preserve]
        if marker.exists():
            stale.append(marker)
        receipt = {
            "schema_version": 1, "operation": "REPLACE_DOCUMENT_SOURCES", "status": "REPLACED",
            "project_id": project_id, "previous_revision": expected_revision, "revision": expected_revision + 1,
            "actor": actor.strip(), "reason": reason.strip(), "changed_at": datetime.now(UTC).isoformat(),
            "source_path": source_path, "source_snapshot": manifest,
            "previous_scope": before, "source_scope": revised,
            "database_scope_preserved": True, "cq_preserved": True,
            "invalidated_artifacts": [p.relative_to(project_dir).as_posix() for p in stale],
            "receipt_path": (audit_dir / "change.json").relative_to(project_dir).as_posix(),
            "next_action": "使用新 revision 和 source_path 重新提交受控资料解析，再通过原有 S0 门禁。",
        }
        saved = {p: p.read_bytes() if p.exists() else None for p in (
            scope_path, project_dir / "project.json", project_dir / "workflow-state.json",
            project_dir / "events/agent-trace.jsonl",
        )}
        moved = []
        try:
            service._write_json(audit_dir / "change.json", receipt)
            for path in stale:
                target = audit_dir / "before" / path.relative_to(project_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
                moved.append((path, target))
            service._write_json(scope_path, revised)
            project["source_scope"] = revised
            service._write_json(project_dir / "project.json", project)
            state["last_error"] = None
            state["resume_point"] = "S0: 来源已更正，需重新解析并通过资料质量与来源门禁"
            service._save_state(project_dir, state)
            service._append_event(project_dir, "DOCUMENT_SOURCES_REPLACED", state, {
                "stage": "S0", "actor": actor.strip(), "reason": reason.strip(),
                "receipt_path": receipt["receipt_path"], "source_path": source_path,
            })
        except Exception:
            for original, target in reversed(moved):
                original.parent.mkdir(parents=True, exist_ok=True)
                target.rename(original)
            for path, content in saved.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    service._atomic_write(path, content.decode("utf-8"))
            if audit_dir.exists():
                shutil.rmtree(audit_dir)
            raise
        service._refresh_manifest(project_dir)
        return {**service._status_payload(project_dir, state), "source_change": receipt}
