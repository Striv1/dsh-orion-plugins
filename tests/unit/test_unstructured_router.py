from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ingestion import s0_batch_executor
from services.ingestion.s0_batch_executor import (
    collect_files,
    extract_pdf_text_layer,
    rename_job_markdown_outputs,
    structure_upgrade_reason,
    structured_markdown_filename,
)
from services.ingestion.unstructured_router import catalog_payload, route_file

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_PDF = ROOT / "tests" / "fixtures" / "synthetic_chinese_text.pdf"


def create_reference_batch(
    tmp_path: Path,
    *,
    filename: str = "资料.md",
    content: str = "这是一份真实业务资料，必须通过清单校验后才能进入 S0。",
) -> tuple[Path, Path]:
    content_bytes = content.encode("utf-8")
    content_hash = "sha256:" + hashlib.sha256(content_bytes).hexdigest()
    fingerprint = hashlib.sha256(
        (f"{filename}\0{len(content_bytes)}\0{content_hash.removeprefix('sha256:')}\n").encode()
    ).hexdigest()
    reference = tmp_path / ".orion-s0-uploads" / f"REFERENCE-{fingerprint[:20].upper()}"
    reference.mkdir(parents=True)
    document = reference / filename
    document.write_text(content, encoding="utf-8")
    s0_batch_executor.atomic_json(
        reference / "manifest.json",
        {
            "reference_id": reference.name,
            "workspace_root": str(tmp_path),
            "source_path": reference.relative_to(tmp_path).as_posix(),
            "file_count": 1,
            "total_bytes": document.stat().st_size,
            "files": [
                {
                    "path": document.name,
                    "bytes": document.stat().st_size,
                    "sha256": content_hash,
                }
            ],
        },
    )
    return reference, document


def test_resume_structured_import_keeps_s0_and_fences_s1_revision(tmp_path, monkeypatch):
    job_dir = tmp_path / "JOB-IMPORT-RESUME"
    job_dir.mkdir()
    state = {"project_id": "ontology-project-test", "revision": 2, "current_stage": "S1"}
    s0_batch_executor.atomic_json(job_dir / "status.json", {
        "job_id": job_dir.name, "status": "COMMITTED", "warnings": [],
        "workflow": state, "structured_data": {"status": "IMPORTING"}})
    s0_batch_executor.atomic_json(job_dir / "request.json", {
        "workflow_home": str(tmp_path), "input_root": str(tmp_path),
        "project_id": state["project_id"], "project_revision": 1, "intake_mode": "HYBRID"})
    s0_batch_executor.atomic_json(job_dir / "bundle.json", {
        "documents": [], "quality_report": {}, "processing_trace": {}, "evidence_index": []})
    calls = []

    class Service:
        def __init__(self, root):
            pass

        def _require_stage(self, project_id, stage):
            assert stage == "S1"
            return tmp_path, state

        def _require_expected_revision(self, current, expected):
            assert expected == current["revision"] == 2

        def _verify_project_integrity(self, project_dir, current, stages):
            assert stages == ("S0",)
            return {"status": "PASSED"}

        def get_status(self, **kwargs):
            return state

        def record_document_evidence(self, **kwargs):
            pytest.fail("恢复导入不能重新提交 S0")

        def record_data_understanding(self, **kwargs):
            assert kwargs["expected_revision"] == 2
            calls.append("S1")
            return {**state, "revision": 3, "current_stage": "S2"}

    class Pipeline:
        def import_documents(self, **kwargs):
            calls.append("IMPORT")
            return {"receipts": [{"sheet_count": 1, "row_count": 10}], "s1": {
                "datasource_inventory": {}, "schema_snapshot": {}, "data_profile": {},
                "relation_candidates": [], "evidence_sql": []}}

    monkeypatch.setattr(s0_batch_executor, "OntologyWorkflowService", Service)
    monkeypatch.setattr(s0_batch_executor, "StructuredDataPipeline", Pipeline)
    result = s0_batch_executor.commit_job(job_dir, "tester", "恢复已授权全量导入", False, "IMPORT", resume_import=True)
    assert calls == ["IMPORT", "S1"]
    assert result["structured_data"]["status"] == "READY_FOR_SEMANTIC_REVIEW"
    assert result["workflow"]["revision"] == 3
    with pytest.raises(ValueError, match="可恢复"):
        s0_batch_executor.commit_job(job_dir, "tester", "重复请求", False, "IMPORT", resume_import=True)


def managed_worker_job(
    tmp_path: Path,
    invocation_id: str = "WRK-TEST-A",
) -> tuple[Path, Path]:
    source = tmp_path / "受控批处理资料.txt"
    source.write_text(
        "这是一份用于验证 S0 worker invocation 隔离和取消保护的正式测试资料。",
        encoding="utf-8",
    )
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-MANAGED-WORKER"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": "JOB-MANAGED-WORKER",
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": source.name,
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
            "worker_invocation_id": invocation_id,
        },
    )
    s0_batch_executor.atomic_json(
        job_dir / "runner.json",
        {
            "state": "STARTING",
            "worker_invocation_id": invocation_id,
        },
    )
    s0_batch_executor.atomic_json(
        job_dir / "worker-fence.json",
        {"worker_invocation_id": invocation_id},
    )
    s0_batch_executor.atomic_json(
        job_dir / "status.json",
        {
            "job_id": "JOB-MANAGED-WORKER",
            "status": "QUEUED",
            "message": "等待 worker 启动。",
        },
    )
    return request_path, source


def test_structured_markdown_filename_preserves_chinese_source_name() -> None:
    used_names: set[str] = set()

    first = structured_markdown_filename(
        "广州市社会医疗保险规定_2022-11-02.pdf",
        "DOC-1234567890ABCDEF",
        used_names,
    )
    duplicate = structured_markdown_filename(
        "广州市社会医疗保险规定_2022-11-02.docx",
        "DOC-FEDCBA0987654321",
        used_names,
    )

    assert first == "广州市社会医疗保险规定_2022-11-02.md"
    assert duplicate == "广州市社会医疗保险规定_2022-11-02__87654321.md"
    assert len(duplicate.encode("utf-8")) <= 220


def test_rename_job_markdown_outputs_updates_files_and_metadata(tmp_path: Path) -> None:
    job_dir = tmp_path / "JOB-TEST"
    markdown_dir = job_dir / "structured-markdown"
    markdown_dir.mkdir(parents=True)
    old_relative = "structured-markdown/DOC-ABCDEF1234567890.md"
    old_markdown = job_dir / old_relative
    old_markdown.write_text("# 供应商管理制度\n", encoding="utf-8")
    document = {
        "document_id": "DOC-ABCDEF1234567890",
        "source_name": "供应商管理制度.pdf",
        "structured_markdown_path": old_relative,
    }
    (job_dir / "bundle.json").write_text(
        json.dumps(
            {
                "documents": [document],
                "processing_trace": {"batch_job_id": "JOB-TEST"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_dir / "document-register.json").write_text(
        json.dumps([document], ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "status.json").write_text(
        json.dumps({"files": [document]}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = rename_job_markdown_outputs(job_dir)

    expected_relative = "structured-markdown/供应商管理制度.md"
    assert result["renamed"] == 1
    assert not old_markdown.exists()
    assert (job_dir / expected_relative).read_text(encoding="utf-8") == "# 供应商管理制度\n"
    for metadata_name in ("bundle.json", "document-register.json", "status.json"):
        payload = json.loads((job_dir / metadata_name).read_text(encoding="utf-8"))
        item = (
            payload[0]
            if isinstance(payload, list)
            else (payload.get("documents") or payload["files"])[0]
        )
        assert item["structured_markdown_path"] == expected_relative


def test_router_uses_file_signature_before_misleading_extension(tmp_path: Path) -> None:
    disguised = tmp_path / "看起来像Word.docx"
    disguised.write_bytes(b"%PDF-1.7\n")

    decision = route_file(disguised)

    assert decision.category == "pdf"
    assert decision.detected_format == ".pdf"
    assert decision.strategy == "PDF_TEXT_LAYER_DIRECT"
    assert "PDF 文档" in decision.label


def test_router_catalog_separates_ready_and_connector_required_formats() -> None:
    payload = catalog_payload()
    categories = {item["category"]: item for item in payload["categories"]}

    assert payload["policy"].startswith("direct-first")
    assert categories["pdf"]["availability"] == "READY"
    assert categories["image"]["availability"] == "READY_MCP"
    assert categories["audio"]["availability"] == "SERVICE_REQUIRED"
    assert categories["design_cad"]["primary"] == "DOMAIN_NATIVE_CONNECTOR"


def test_direct_pdf_reports_every_completed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        s0_batch_executor,
        "extract_pdf_text_layer",
        lambda _path: {
            "direct_ready": True,
            "page_count": 2,
            "readable_pages": 2,
            "markdown": "## 第 1 页\n\n第一页资料内容。\n\n## 第 2 页\n\n第二页资料内容。",
            "reason": "2/2 页具有可用文字层。",
            "pages": [
                {"page": 1, "text": "第一页资料内容。", "direct_ready": True},
                {"page": 2, "text": "第二页资料内容。", "direct_ready": True},
            ],
        },
    )
    events: list[dict[str, object]] = []

    result = asyncio.run(
        s0_batch_executor.process_pdf(
            Path("不用读取的测试文件.pdf"),
            "不用读取的测试文件.pdf",
            {},
            events.append,
        )
    )

    assert result[3] == "PDF_TEXT_LAYER_DIRECT"
    assert [event["page"] for event in events] == [1, 2]
    assert all(event["status"] == "SUCCEEDED" for event in events)
    assert all(event["phase"] == "DIRECT_TEXT" for event in events)


def test_scanned_pdf_uses_light_ocr_without_unnecessary_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = tmp_path / "page.png"
    rendered.write_bytes(b"test")
    rendered_dpis: list[int] = []

    def fake_render(_path: Path, _page: int, _root: Path, dpi: int) -> Path:
        rendered_dpis.append(dpi)
        return rendered

    monkeypatch.setattr(
        s0_batch_executor,
        "render_pdf_page",
        fake_render,
    )
    monkeypatch.setattr(
        s0_batch_executor,
        "validate_recognition_image",
        lambda _path: {
            "format": "PNG",
            "mode": "RGB",
            "width": 1200,
            "height": 1800,
            "size_bytes": 4,
        },
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(_endpoint: str, tool: str, arguments: dict[str, object]):
        calls.append((tool, arguments))
        return "这是普通政策正文，不包含需要恢复的表格结构，使用快速文字识别即可。", {
            "server_name": "test",
            "server_version": "1",
            "protocol_version": "1",
        }

    monkeypatch.setattr(s0_batch_executor, "call_mcp_tool", fake_call)
    events: list[dict[str, object]] = []
    text_layer = {
        "direct_ready": False,
        "page_count": 1,
        "readable_pages": 0,
        "markdown": "",
        "reason": "文字层不可用。",
        "pages": [{"page": 1, "text": "", "direct_ready": False}],
    }

    result = asyncio.run(
        s0_batch_executor.process_pdf(
            Path("扫描政策.pdf"),
            "扫描政策.pdf",
            {},
            events.append,
            text_layer,
        )
    )

    assert result[3] == "PADDLEOCR_OCR"
    assert [tool for tool, _arguments in calls] == ["ocr"]
    assert rendered_dpis == [150]
    assert events[-1]["method"] == "PADDLEOCR_OCR"


def test_scanned_table_upgrades_from_light_ocr_to_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = tmp_path / "table.png"
    rendered.write_bytes(b"test")
    rendered_dpis: list[int] = []

    def fake_render(_path: Path, _page: int, _root: Path, dpi: int) -> Path:
        rendered_dpis.append(dpi)
        return rendered

    monkeypatch.setattr(
        s0_batch_executor,
        "render_pdf_page",
        fake_render,
    )
    monkeypatch.setattr(
        s0_batch_executor,
        "validate_recognition_image",
        lambda _path: {
            "format": "PNG",
            "mode": "RGB",
            "width": 1200,
            "height": 1800,
            "size_bytes": 4,
        },
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(_endpoint: str, tool: str, arguments: dict[str, object]):
        calls.append((tool, arguments))
        if tool == "ocr":
            return "| 项目 | 条件 |\n| --- | --- |\n| 医保 | 在保 |", {
                "server_name": "ocr",
                "server_version": "1",
                "protocol_version": "1",
            }
        return "| 项目 | 条件 |\n| --- | --- |\n| 医保 | 在保 |", {
            "server_name": "structure",
            "server_version": "1",
            "protocol_version": "1",
        }

    monkeypatch.setattr(s0_batch_executor, "call_mcp_tool", fake_call)
    text_layer = {
        "direct_ready": False,
        "page_count": 1,
        "readable_pages": 0,
        "markdown": "",
        "reason": "文字层不可用。",
        "pages": [{"page": 1, "text": "", "direct_ready": False}],
    }

    result = asyncio.run(
        s0_batch_executor.process_pdf(
            Path("扫描表格.pdf"),
            "扫描表格.pdf",
            {},
            text_layer=text_layer,
        )
    )

    assert structure_upgrade_reason("| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert [tool for tool, _arguments in calls] == ["ocr", "pp_structurev3"]
    assert rendered_dpis == [150, 200]
    structure_arguments = calls[1][1]
    assert structure_arguments["runtime_params"]["use_table_recognition"] is True
    assert result[3] == "HYBRID_PAGE_ROUTE"


def test_root_scan_skips_internal_uploads_but_explicit_upload_batch_is_allowed(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "正式资料.txt"
    uploaded = tmp_path / ".orion-s0-uploads" / "UPLOAD-TEST" / "上传资料.txt"
    cached = tmp_path / ".orion-s0-cache" / "v1" / "缓存资料.txt"
    content_object = tmp_path / ".orion-s0-content" / "sha256" / "aa" / "内部原件.txt"
    uploaded.parent.mkdir(parents=True)
    cached.parent.mkdir(parents=True)
    content_object.parent.mkdir(parents=True)
    regular.write_text("正式资料内容足够进入资料路由。", encoding="utf-8")
    uploaded.write_text("上传资料内容足够进入资料路由。", encoding="utf-8")
    cached.write_text("缓存内部文件不能被重新作为资料扫描。", encoding="utf-8")
    content_object.write_text("内容寻址原件不能被普通资料扫描。", encoding="utf-8")

    root_scan = collect_files(tmp_path, ".")
    upload_scan = collect_files(tmp_path, ".orion-s0-uploads/UPLOAD-TEST")

    assert root_scan == [regular]
    assert upload_scan == [uploaded]


def test_reference_batch_uses_verified_manifest_and_never_processes_it(
    tmp_path: Path,
) -> None:
    reference, document = create_reference_batch(tmp_path)

    assert collect_files(tmp_path, reference.relative_to(tmp_path).as_posix()) == [document]

    document.write_text("内容已经被篡改。", encoding="utf-8")
    with pytest.raises(ValueError, match="文件内容与清单不一致"):
        collect_files(tmp_path, reference.relative_to(tmp_path).as_posix())

    tampered_manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    tampered_manifest["files"][0]["bytes"] = document.stat().st_size
    tampered_manifest["files"][0]["sha256"] = s0_batch_executor.sha256_file(document)
    tampered_manifest["total_bytes"] = document.stat().st_size
    s0_batch_executor.atomic_json(reference / "manifest.json", tampered_manifest)
    with pytest.raises(ValueError, match="reference_id 与文件内容指纹不一致"):
        collect_files(tmp_path, reference.relative_to(tmp_path).as_posix())


def test_reference_directory_transient_swap_is_rejected_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, _document = create_reference_batch(tmp_path, filename="资料.txt")
    uploads = reference.parent
    impostor_uploads = tmp_path / "REFERENCE-TRANSIENT-IMPOSTOR-UPLOADS"
    impostor = impostor_uploads / reference.name
    impostor.mkdir(parents=True)
    (impostor / "资料.txt").write_text(
        "MALICIOUS TRANSIENT CONTENT 不得进入结构化结果。",
        encoding="utf-8",
    )
    observed: list[str] = []

    def swapped_text_to_markdown(path: Path):
        parked = tmp_path / ".orion-s0-uploads-PARKED"
        displaced = tmp_path / "REFERENCE-TRANSIENT-DISPLACED-UPLOADS"
        uploads.rename(parked)
        impostor_uploads.rename(uploads)
        try:
            malicious = path.read_text(encoding="utf-8")
            observed.append(malicious)
        finally:
            uploads.rename(displaced)
            parked.rename(uploads)
            shutil.rmtree(displaced)
        return malicious, 1, [{"locator": "第 1 行", "content": malicious}], []

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", swapped_text_to_markdown)
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-REFERENCE-TRANSIENT-SWAP"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": job_dir.name,
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": reference.relative_to(tmp_path).as_posix(),
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert observed and "MALICIOUS TRANSIENT CONTENT" in observed[0]
    assert status["status"] == "FAILED"
    assert "快照路径链身份或状态已变化" in status["error"]
    outputs = list((job_dir / "structured-markdown").glob("*.md"))
    assert not outputs


def test_content_addressed_hardlink_keeps_batch_path_and_evidence_hash(tmp_path: Path) -> None:
    content = "内容寻址原件通过批次硬链接进入 S0，证据仍保留批次相对路径。"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    content_object = tmp_path / ".orion-s0-content" / "sha256" / digest[:2] / digest
    content_object.parent.mkdir(parents=True)
    content_object.write_text(content, encoding="utf-8")
    uploaded = tmp_path / ".orion-s0-uploads" / "UPLOAD-LINKED-12345678" / "资料.txt"
    uploaded.parent.mkdir(parents=True)
    os.link(content_object, uploaded)
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-LINKED-12345678"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": "JOB-LINKED-12345678",
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": ".orion-s0-uploads/UPLOAD-LINKED-12345678",
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))
    bundle = json.loads((job_dir / "bundle.json").read_text(encoding="utf-8"))
    document = bundle["documents"][0]

    assert document["source_path"] == ".orion-s0-uploads/UPLOAD-LINKED-12345678/资料.txt"
    assert document["source_sha256"] == f"sha256:{digest}"
    assert uploaded.stat().st_ino == content_object.stat().st_ino


def test_required_minio_snapshot_is_verified_and_parsed_from_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "实时资料.txt"
    source.write_text("必须先进入 MinIO，回读校验成功后才能由 S0 解析。", encoding="utf-8")
    objects: dict[str, bytes] = {}

    class FakeClient:
        def fget_object(self, bucket: str, object_key: str, target: str) -> None:
            assert bucket == "test-originals"
            Path(target).write_bytes(objects[object_key])

    class FakeStore:
        bucket = "test-originals"
        client = FakeClient()

        def store_original(self, path: Path, checksum: str) -> SimpleNamespace:
            object_key = f"sha256/{checksum.removeprefix('sha256:')}"
            objects[object_key] = path.read_bytes()
            return SimpleNamespace(object_key=object_key)

    fake_store = FakeStore()
    monkeypatch.setenv("ORION_S0_MINIO_SNAPSHOT_REQUIRED", "true")
    monkeypatch.setattr(
        s0_batch_executor.MinioArtifactStore,
        "from_env",
        classmethod(lambda cls: fake_store),
    )
    observed_paths: list[Path] = []
    original_parser = s0_batch_executor.text_to_markdown

    def observe_readback(path: Path):
        observed_paths.append(path)
        return original_parser(path)

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", observe_readback)
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-MINIO-BEFORE-PARSE"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": job_dir.name,
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": source.name,
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    bundle = json.loads((job_dir / "bundle.json").read_text(encoding="utf-8"))
    document = bundle["documents"][0]
    assert status["status"] == "READY_FOR_REVIEW"
    assert status["original_snapshot"] == {
        "required": True,
        "backend": "minio",
        "verified": True,
        "file_count": 1,
    }
    assert observed_paths[0] != source
    assert observed_paths[0].parent.name == ".minio-original-readback"
    assert document["original_storage_backend"] == "minio"
    assert document["original_source_uri"].startswith("minio://test-originals/")
    assert document["original_snapshot_sha256"] == s0_batch_executor.sha256_file(source)
    assert any(
        item.get("tool") == "orion__minio__snapshot_original"
        for item in bundle["processing_trace"]["tool_invocations"]
    )
    assert not (job_dir / ".minio-original-readback").exists()


def test_verified_minio_readback_ignores_scheduler_sibling_churn_for_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, _document = create_reference_batch(tmp_path, filename="资料.txt")
    objects: dict[str, bytes] = {}

    class FakeClient:
        def fget_object(self, bucket: str, object_key: str, target: str) -> None:
            Path(target).write_bytes(objects[object_key])

    class FakeStore:
        bucket = "test-originals"
        client = FakeClient()

        def store_original(self, path: Path, checksum: str) -> SimpleNamespace:
            object_key = f"sha256/{checksum.removeprefix('sha256:')}"
            objects[object_key] = path.read_bytes()
            # The real scheduler creates/removes this sibling lock while workers run.
            (tmp_path / ".orion-document-scheduler.lock").write_text("active")
            return SimpleNamespace(object_key=object_key)

    monkeypatch.setenv("ORION_S0_MINIO_SNAPSHOT_REQUIRED", "true")
    monkeypatch.setattr(
        s0_batch_executor.MinioArtifactStore,
        "from_env",
        classmethod(lambda cls: FakeStore()),
    )
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-REFERENCE-MINIO-SCHEDULER"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": job_dir.name,
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": reference.relative_to(tmp_path).as_posix(),
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "READY_FOR_REVIEW"
    assert status["reference_snapshot"]["verified"] is True
    assert status["original_snapshot"]["verified"] is True


def test_required_minio_snapshot_hash_mismatch_stops_before_s0_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "被篡改资料.txt"
    source.write_text("原始内容必须保持不变。", encoding="utf-8")

    class TamperingClient:
        def fget_object(self, bucket: str, object_key: str, target: str) -> None:
            Path(target).write_text("回读内容已被篡改。", encoding="utf-8")

    class TamperingStore:
        bucket = "test-originals"
        client = TamperingClient()

        def store_original(self, path: Path, checksum: str) -> SimpleNamespace:
            return SimpleNamespace(object_key=checksum.removeprefix("sha256:"))

    monkeypatch.setenv("ORION_S0_MINIO_SNAPSHOT_REQUIRED", "true")
    monkeypatch.setattr(
        s0_batch_executor.MinioArtifactStore,
        "from_env",
        classmethod(lambda cls: TamperingStore()),
    )
    parser_called = False

    def forbidden_parser(path: Path):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("SHA-256 mismatch must stop before parsing")

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", forbidden_parser)
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-MINIO-HASH-MISMATCH"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": job_dir.name,
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": source.name,
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED"
    assert "MinIO 原件回读 SHA-256 不一致" in status["error"]
    assert parser_called is False
    assert not (job_dir / "bundle.json").exists()
    assert not (job_dir / ".minio-original-readback").exists()


def test_s0_content_cache_requires_same_hash_and_processing_profile(tmp_path: Path) -> None:
    request = {
        "ocr_render_dpi": 150,
        "structure_render_dpi": 200,
        "ocr_endpoint": "http://127.0.0.1:10827/mcp",
        "structure_endpoint": "http://127.0.0.1:10826/mcp",
        "reuse_content_cache": True,
    }
    source_hash = "sha256:" + "a" * 64
    markdown = "# 缓存资料\n\n这是可以跨工程复用的结构化正文。"
    s0_batch_executor.save_s0_cache(
        tmp_path,
        source_hash,
        ".pdf",
        request,
        markdown=markdown,
        units=2,
        raw_evidence=[{"locator": "PDF 第 1 页", "section": "第 1 页", "content": "缓存正文"}],
        method="PADDLEOCR_OCR",
        document_fields={"page_count": 2},
        warnings=[],
        route_strategy="STRUCTURE_REQUIRED",
        route_reason="文字层不可用。",
    )

    cached = s0_batch_executor.load_s0_cache(
        tmp_path,
        source_hash,
        ".pdf",
        request,
    )
    changed_profile = s0_batch_executor.load_s0_cache(
        tmp_path,
        source_hash,
        ".pdf",
        {**request, "ocr_render_dpi": 175},
    )

    assert cached is not None
    assert cached["markdown"] == markdown
    assert cached["processing_profile"]["ocr_render_dpi"] == 150
    assert changed_profile is None


def test_second_job_reuses_cross_project_content_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "重复使用的政策资料.txt"
    source.write_text("同一份正式资料在新工程中不应重复执行解析。", encoding="utf-8")
    calls: list[str] = []
    original = s0_batch_executor.text_to_markdown

    def counted(path: Path):
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", counted)
    for job_id in ("JOB-CACHE-FIRST", "JOB-CACHE-SECOND"):
        job_dir = tmp_path / ".orion-s0-jobs" / job_id
        job_dir.mkdir(parents=True)
        request_path = job_dir / "request.json"
        s0_batch_executor.atomic_json(
            request_path,
            {
                "job_id": job_id,
                "input_root": str(tmp_path),
                "workflow_home": str(tmp_path / "workflows"),
                "source_path": source.name,
                "intake_mode": "DOCUMENT_ONLY",
                "reuse_content_cache": True,
                "actor": "test-engineer",
            },
        )
        asyncio.run(s0_batch_executor.run_job(request_path))

    second_bundle = json.loads(
        (tmp_path / ".orion-s0-jobs" / "JOB-CACHE-SECOND" / "bundle.json").read_text(
            encoding="utf-8"
        )
    )
    second_document = second_bundle["documents"][0]

    assert calls == [source.name]
    assert second_document["cache_reused"] is True
    assert second_bundle["quality_report"]["content_cache_reused_documents"] == 1
    assert second_bundle["processing_trace"]["executor_version"] == "1.1.0"
    assert second_bundle["processing_trace"]["processing_profile"]["ocr_render_dpi"] == 150
    assert second_bundle["processing_trace"]["content_cache_reused_documents"] == 1
    assert any(
        item.get("tool") == "orion__batch__content_cache_reuse"
        for item in second_bundle["processing_trace"]["tool_invocations"]
    )


def test_new_jobs_do_not_reuse_content_cache_unless_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "需要重新解析的资料.txt"
    source.write_text("相同文件用于新工程时，默认仍然重新执行本次 S0 解析。", encoding="utf-8")
    calls: list[str] = []
    original = s0_batch_executor.text_to_markdown

    def counted(path: Path):
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", counted)
    for job_id in ("JOB-NO-CACHE-FIRST", "JOB-NO-CACHE-SECOND"):
        job_dir = tmp_path / ".orion-s0-jobs" / job_id
        job_dir.mkdir(parents=True)
        request_path = job_dir / "request.json"
        s0_batch_executor.atomic_json(
            request_path,
            {
                "job_id": job_id,
                "input_root": str(tmp_path),
                "workflow_home": str(tmp_path / "workflows"),
                "source_path": source.name,
                "intake_mode": "DOCUMENT_ONLY",
                "actor": "test-engineer",
            },
        )
        asyncio.run(s0_batch_executor.run_job(request_path))

    assert calls == [source.name, source.name]
    second_bundle = json.loads(
        (tmp_path / ".orion-s0-jobs" / "JOB-NO-CACHE-SECOND" / "bundle.json").read_text(
            encoding="utf-8"
        )
    )
    assert second_bundle["processing_trace"]["content_cache_enabled"] is False
    assert second_bundle["quality_report"]["content_cache_reused_documents"] == 0


def test_commit_rejects_document_only_action_when_preflight_requires_import(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-LARGE-TABLE-GATE"
    job_dir.mkdir(parents=True)
    s0_batch_executor.atomic_json(
        job_dir / "status.json",
        {
            "status": "READY_FOR_REVIEW",
            "warnings": [],
            "source_preflight": {"requires_structured_import": True},
        },
    )
    s0_batch_executor.atomic_json(
        job_dir / "request.json",
        {"intake_mode": "HYBRID"},
    )

    with pytest.raises(ValueError, match="必须选择 IMPORT"):
        s0_batch_executor.commit_job(
            job_dir,
            reviewed_by="test-engineer",
            rationale="已核对大表，应进入结构化数据区。",
            accept_warnings=False,
            structured_data_action="DOCUMENT_ONLY",
        )


def test_managed_worker_requires_matching_environment_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, _source = managed_worker_job(tmp_path)
    monkeypatch.setenv("ORION_WORKER_INVOCATION_ID", "WRK-OTHER")

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((request_path.parent / "status.json").read_text(encoding="utf-8"))
    assert status == {
        "job_id": "JOB-MANAGED-WORKER",
        "status": "QUEUED",
        "message": "等待 worker 启动。",
    }
    assert not (request_path.parent / "bundle.json").exists()


def test_managed_worker_completes_with_matching_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_id = "WRK-TEST-A"
    request_path, _source = managed_worker_job(tmp_path, invocation_id)
    monkeypatch.setenv("ORION_WORKER_INVOCATION_ID", invocation_id)

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((request_path.parent / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "READY_FOR_REVIEW"
    assert status["completed_files"] == 1
    assert (request_path.parent / "bundle.json").is_file()
    assert (request_path.parent / "ingestion-quality-report.json").is_file()
    assert (request_path.parent / "batch-review.html").is_file()
    trace = json.loads((request_path.parent / "processing-trace.json").read_text(encoding="utf-8"))
    assert trace["actor"] == "test-engineer"


@pytest.mark.parametrize("actor", [None, "", "  ", 42, "operator\nother"])
def test_managed_worker_rejects_missing_or_invalid_request_actor_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actor,
) -> None:
    invocation_id = "WRK-TEST-A"
    request_path, _source = managed_worker_job(tmp_path, invocation_id)
    monkeypatch.setenv("ORION_WORKER_INVOCATION_ID", invocation_id)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if actor is None:
        request.pop("actor")
    else:
        request["actor"] = actor
    s0_batch_executor.atomic_json(request_path, request)

    def reject_processing(*_args, **_kwargs):
        pytest.fail("Actor validation must precede document processing")

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", reject_processing)
    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((request_path.parent / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED"
    assert "操作人" in status["error"]
    assert not (request_path.parent / "bundle.json").exists()
    assert not (request_path.parent / "processing-trace.json").exists()


def test_stale_managed_worker_never_overwrites_cancelled_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_id = "WRK-TEST-A"
    request_path, _source = managed_worker_job(tmp_path, invocation_id)
    monkeypatch.setenv("ORION_WORKER_INVOCATION_ID", invocation_id)
    original = s0_batch_executor.text_to_markdown

    def cancel_while_processing(path: Path):
        s0_batch_executor.atomic_json(
            request_path.parent / "worker-fence.json",
            {"worker_invocation_id": "WRK-FENCED-B"},
        )
        s0_batch_executor.atomic_json(
            request_path.parent / "status.json",
            {
                "job_id": "JOB-MANAGED-WORKER",
                "status": "CANCELLED",
                "message": "测试取消已经成为新终态。",
            },
        )
        return original(path)

    monkeypatch.setattr(
        s0_batch_executor,
        "text_to_markdown",
        cancel_while_processing,
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((request_path.parent / "status.json").read_text(encoding="utf-8"))
    assert status == {
        "job_id": "JOB-MANAGED-WORKER",
        "status": "CANCELLED",
        "message": "测试取消已经成为新终态。",
    }
    assert not (request_path.parent / "ingestion-quality-report.json").exists()
    assert not (request_path.parent / "batch-review.html").exists()


def test_worker_flock_serializes_same_job_and_rechecks_fence_while_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_id = "WRK-TEST-A"
    request_path, _source = managed_worker_job(tmp_path, invocation_id)
    monkeypatch.setenv("ORION_WORKER_INVOCATION_ID", invocation_id)
    managed_lease = s0_batch_executor.WorkerInvocationLease.from_request(request_path)
    legacy_lease = s0_batch_executor.WorkerInvocationLease(request_path, "", "")

    async def exercise_lock() -> bool:
        contender_entered = False

        async def contend() -> None:
            nonlocal contender_entered
            async with s0_batch_executor.worker_execution_lock(
                request_path.parent,
                managed_lease,
                timeout_seconds=0.5,
                poll_seconds=0.01,
            ):
                contender_entered = True

        async with s0_batch_executor.worker_execution_lock(
            request_path.parent,
            legacy_lease,
            timeout_seconds=0.5,
            poll_seconds=0.01,
        ):
            contender = asyncio.create_task(contend())
            await asyncio.sleep(0.04)
            s0_batch_executor.atomic_json(
                request_path.parent / "worker-fence.json",
                {"worker_invocation_id": "WRK-FENCED-B"},
            )
            with pytest.raises(s0_batch_executor.StaleWorkerInvocation):
                await contender
        return contender_entered

    assert asyncio.run(exercise_lock()) is False


def test_workload_router_puts_direct_documents_before_specialist_ocr(
    tmp_path: Path,
) -> None:
    scanned_pdf = tmp_path / "扫描件.pdf"
    direct_pdf = tmp_path / "文字版.pdf"
    text_file = tmp_path / "说明.txt"
    scanned_pdf.write_bytes(b"%PDF-1.7\n/Type /Page\n")
    direct_pdf.write_bytes(b"%PDF-1.7\n/Type /Page\n")
    text_file.write_text("可直接处理的说明资料。", encoding="utf-8")
    files = [scanned_pdf, direct_pdf, text_file]

    ordered, summary = s0_batch_executor.order_processing_lanes(
        files,
        {
            scanned_pdf: {"direct_ready": False},
            direct_pdf: {"direct_ready": True},
        },
    )

    assert ordered == [direct_pdf, text_file, scanned_pdf]
    assert summary == {
        "fast_lane_total": 2,
        "specialist_lane_total": 1,
        "specialist_concurrency": 1,
    }
    assert s0_batch_executor.recommended_runtime_seconds(71, 412) == 9195


def test_failed_only_retry_preserves_successes_and_selects_no_success_file(
    tmp_path: Path,
) -> None:
    success = tmp_path / "已成功资料.txt"
    failed = tmp_path / "待重试资料.txt"
    success.write_text("已经成功生成的结构化资料内容。", encoding="utf-8")
    failed.write_text("需要重新处理的结构化资料内容。", encoding="utf-8")
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-RETRY"
    job_dir.mkdir(parents=True)
    s0_batch_executor.atomic_json(
        job_dir / "retry-base-status.json",
        {
            "total_files": 2,
            "total_pages": 0,
            "files": [
                {
                    "relative_path": success.name,
                    "source_name": success.name,
                    "status": "SUCCEEDED",
                    "position": "1/2",
                },
                {
                    "relative_path": failed.name,
                    "source_name": failed.name,
                    "status": "FAILED",
                    "position": "2/2",
                },
            ],
        },
    )
    s0_batch_executor.atomic_json(
        job_dir / "bundle.json",
        {
            "documents": [{"source_path": success.name, "source_sha256": "sha256:ok"}],
            "evidence_index": [{"document_id": "DOC-OK"}],
            "processing_trace": {"tool_invocations": []},
        },
    )

    context = s0_batch_executor.load_retry_context(
        tmp_path,
        job_dir,
        {
            "retry_failed_only": True,
            "retry_failed_paths": [failed.name],
        },
    )

    assert context is not None
    assert context["files"] == [failed]
    assert [item["relative_path"] for item in context["successful_files"]] == [success.name]
    assert all(path != success for path in context["files"])


def test_cancelled_job_retry_preserves_success_and_selects_only_retryable_paths(
    tmp_path: Path,
) -> None:
    success = tmp_path / "已完成资料.txt"
    interrupted = tmp_path / "被取消资料.txt"
    success.write_text("已经完成且不能重复处理的资料内容。", encoding="utf-8")
    interrupted.write_text("取消后允许按原任务继续处理的资料内容。", encoding="utf-8")
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-CANCEL-RETRY"
    job_dir.mkdir(parents=True)
    s0_batch_executor.atomic_json(
        job_dir / "retry-base-status.json",
        {
            "status": "CANCELLED",
            "total_files": 2,
            "retryable_paths": [interrupted.name],
            "files": [
                {"relative_path": success.name, "status": "SUCCEEDED", "position": "1/2"},
                {"relative_path": interrupted.name, "status": "CANCELLED", "position": "2/2"},
            ],
        },
    )
    s0_batch_executor.atomic_json(
        job_dir / "bundle.json",
        {
            "documents": [{"source_path": success.name, "source_sha256": "sha256:kept"}],
            "evidence_index": [{"document_id": "DOC-KEPT"}],
            "processing_trace": {"tool_invocations": []},
        },
    )

    context = s0_batch_executor.load_retry_context(
        tmp_path,
        job_dir,
        {"retry_failed_only": True, "retry_failed_paths": [interrupted.name]},
    )

    assert context is not None
    assert context["files"] == [interrupted]
    assert [item["relative_path"] for item in context["successful_files"]] == [success.name]


def test_failed_only_retry_merges_new_result_without_reprocessing_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = tmp_path / "已成功资料.txt"
    failed = tmp_path / "待重试资料.txt"
    success.write_text("已经成功生成且必须保留的结构化资料内容。", encoding="utf-8")
    failed.write_text("第一次失败、第二次成功的结构化资料内容。", encoding="utf-8")
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-RETRY-MERGE"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    request = {
        "job_id": "JOB-RETRY-MERGE",
        "input_root": str(tmp_path),
        "workflow_home": str(tmp_path / "workflows"),
        "source_path": ".",
        "intake_mode": "DOCUMENT_ONLY",
        "actor": "test-engineer",
    }
    s0_batch_executor.atomic_json(request_path, request)
    original_text_to_markdown = s0_batch_executor.text_to_markdown
    attempts: list[str] = []

    def fail_one_file(path: Path):
        attempts.append(path.name)
        if path.name == failed.name:
            raise ValueError("安全测试夹具注入的首次失败")
        return original_text_to_markdown(path)

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", fail_one_file)
    asyncio.run(s0_batch_executor.run_job(request_path))
    first_status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    first_bundle = json.loads((job_dir / "bundle.json").read_text(encoding="utf-8"))
    successful_document = dict(first_bundle["documents"][0])
    successful_evidence = list(first_bundle["evidence_index"])
    successful_markdown = (job_dir / successful_document["structured_markdown_path"]).read_bytes()
    assert first_status["status"] == "FAILED"
    assert attempts == [success.name, failed.name]

    s0_batch_executor.atomic_json(job_dir / "retry-base-status.json", first_status)
    s0_batch_executor.atomic_json(
        request_path,
        {
            **request,
            "retry_failed_only": True,
            "retry_failed_paths": [failed.name],
        },
    )
    retry_attempts: list[str] = []

    def retry_text_to_markdown(path: Path):
        retry_attempts.append(path.name)
        return original_text_to_markdown(path)

    monkeypatch.setattr(s0_batch_executor, "text_to_markdown", retry_text_to_markdown)
    asyncio.run(s0_batch_executor.run_job(request_path))

    merged_status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    merged_bundle = json.loads((job_dir / "bundle.json").read_text(encoding="utf-8"))
    assert retry_attempts == [failed.name]
    assert merged_status["status"] == "READY_FOR_REVIEW"
    assert merged_status["completed_files"] == 2
    assert merged_status["failed_files"] == 0
    assert len(merged_status["files"]) == 2
    assert len(merged_bundle["documents"]) == 2
    assert merged_bundle["documents"][0] == successful_document
    assert merged_bundle["evidence_index"][: len(successful_evidence)] == successful_evidence
    assert (
        job_dir / successful_document["structured_markdown_path"]
    ).read_bytes() == successful_markdown
    assert any(
        item.get("tool") == "orion__batch__retry_failed_files" and item.get("status") == "SUCCEEDED"
        for item in merged_bundle["processing_trace"]["tool_invocations"]
    )


def test_development_fail_once_injection_is_gated_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "验收失败一次.txt"
    source.write_text("第一次按安全规则失败，第二次应直接处理成功的资料内容。", encoding="utf-8")
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-FAULT-ONCE"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    request = {
        "job_id": "JOB-FAULT-ONCE",
        "input_root": str(tmp_path),
        "workflow_home": str(tmp_path / "workflows"),
        "source_path": source.name,
        "intake_mode": "DOCUMENT_ONLY",
        "actor": "test-engineer",
        "development_fail_once_paths": [source.name],
    }
    s0_batch_executor.atomic_json(request_path, request)
    monkeypatch.setenv("ORION_ALLOW_DEVELOPMENT_FAULTS", "1")

    asyncio.run(s0_batch_executor.run_job(request_path))
    failed = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert failed["status"] == "FAILED"
    assert "安全开发态失败注入" in failed["files"][0]["error"]

    s0_batch_executor.atomic_json(job_dir / "retry-base-status.json", failed)
    s0_batch_executor.atomic_json(
        request_path,
        {**request, "retry_failed_only": True, "retry_failed_paths": [source.name]},
    )
    asyncio.run(s0_batch_executor.run_job(request_path))
    retried = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert retried["status"] == "READY_FOR_REVIEW"
    assert retried["files"][0]["status"] == "SUCCEEDED"


def test_initial_batch_file_positions_are_sequential(tmp_path: Path) -> None:
    for index in range(1, 4):
        (tmp_path / f"资料-{index}.txt").write_text(
            f"第 {index} 份安全验收资料，用于确认批次序号连续且不会被状态列表别名污染。",
            encoding="utf-8",
        )
    job_dir = tmp_path / ".orion-s0-jobs" / "JOB-POSITIONS"
    job_dir.mkdir(parents=True)
    request_path = job_dir / "request.json"
    s0_batch_executor.atomic_json(
        request_path,
        {
            "job_id": "JOB-POSITIONS",
            "input_root": str(tmp_path),
            "workflow_home": str(tmp_path / "workflows"),
            "source_path": ".",
            "intake_mode": "DOCUMENT_ONLY",
            "actor": "test-engineer",
        },
    )

    asyncio.run(s0_batch_executor.run_job(request_path))

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert [item["position"] for item in status["files"]] == ["1/3", "2/3", "3/3"]
    assert status["current_file_index"] == 3


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="PDFKit direct-text acceptance is available on macOS only",
)
def test_synthetic_chinese_pdf_is_directly_parsed_without_ocr() -> None:
    result = extract_pdf_text_layer(SYNTHETIC_PDF)

    assert result["direct_ready"] is True
    assert result["page_count"] == 2
    assert result["readable_pages"] == 2
    assert result["total_characters"] >= 700
    assert all(page["direct_ready"] for page in result["pages"])
    assert "合成测试文件" in result["markdown"]
    assert "中文文本提取测试" in result["pages"][0]["text"]
    assert "跨页内容保持顺序" in result["pages"][1]["text"]


def test_pdf_text_layer_removes_nul_and_other_unsafe_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "control.pdf"
    source.write_bytes(b"%PDF-test")
    payload = {
        "page_count": 1,
        "pages": [
            {
                "page": 1,
                "text": "电压测试VD\u0000参数值\u0007大于零即异常。" * 5,
                "character_count": 65,
            }
        ],
    }
    monkeypatch.setattr(
        s0_batch_executor,
        "pdf_text_extractor_binary",
        lambda: Path("/tmp/fake-pdf-extractor"),
    )
    monkeypatch.setattr(
        s0_batch_executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False)),
    )

    result = extract_pdf_text_layer(source)

    assert result["control_character_count"] == 10
    assert "\x00" not in result["markdown"]
    assert "\x07" not in result["markdown"]
    assert result["pages"][0]["control_character_count"] == 10
