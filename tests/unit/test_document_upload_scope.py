import asyncio
import hashlib
import json

import pytest

from harness.orion_workflow_mcp import OrionWorkflowTools
from services.ingestion import document_jobs
from services.ingestion.s0_batch_executor import (
    _run_job,
    assert_reference_file,
    assert_reference_snapshot_root,
    queued_source_snapshot,
    selected_document_source_scope,
    upload_snapshot_expectations,
)
from services.ontology_engineering import OntologyWorkflowService, WorkflowError, WorkflowGateError


@pytest.fixture
def upload(tmp_path, monkeypatch):
    root = tmp_path / "intake"
    batch = ".orion-s0-uploads/UPLOAD-12345678-ABCDEF12"
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(root))

    def add(name="资料/规则.md", content=b"# Verified source", hard_link=False):
        digest = hashlib.sha256(content).hexdigest()
        content_ref = f"sha256/{digest[:2]}/{digest}"
        blob = root / ".orion-s0-content" / content_ref
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            blob.write_bytes(content)
        alias = root / batch / name
        alias.parent.mkdir(parents=True, exist_ok=True)
        if hard_link:
            alias.hardlink_to(blob)
        else:
            alias.write_bytes(content)
        receipt = root / ".orion-s0-content/references/uploads" / batch.split("/")[-1]
        receipt.mkdir(parents=True, exist_ok=True)
        receipt /= hashlib.sha256(name.encode()).hexdigest() + ".json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "upload_id": batch.split("/")[-1],
                    "file_name": name,
                    "batch_source_path": batch,
                    "digest": f"sha256:{digest}",
                    "bytes": len(content),
                    "content_path": content_ref,
                    "batch_reference_mode": "HARD_LINK" if hard_link else "COPY_FALLBACK",
                }
            )
        )
        return alias, blob, receipt

    return root, batch, add


def test_upload_is_verified_and_project_scope_precedes_content_inspection(
    upload, tmp_path, monkeypatch
):
    root, batch, add = upload
    alias, _, _ = add(hard_link=True)
    service = OntologyWorkflowService(tmp_path / "workflows")
    source_scope = {
        "sources": [{"kind": "DOCUMENT", "source_path": batch, "path_scope": "DIRECTORY"}]
    }
    denied = service.create_project(
        project_name="上传排除边界",
        domain="test",
        intake_mode="DOCUMENT_ONLY",
        source_scope={**source_scope, "excluded_source_refs": [alias.relative_to(root).as_posix()]},
    )
    calls = []

    def inspect(path):
        calls.append(path)
        return {"file_count": 1, "requires_structured_import": False}

    monkeypatch.setattr(document_jobs, "inspect_source", inspect)
    with pytest.raises(WorkflowGateError, match="排除"):
        document_jobs.enqueue_document_job(
            service,
            project_id=denied["project_id"],
            source_path=batch,
            expected_revision=denied["revision"],
            actor="test",
        )
    assert calls == []
    assert not (root / ".orion-s0-jobs").exists()
    allowed = service.create_project(
        project_name="上传允许边界",
        domain="test",
        intake_mode="DOCUMENT_ONLY",
        source_scope=source_scope,
    )
    result = document_jobs.enqueue_document_job(
        service,
        project_id=allowed["project_id"],
        source_path=batch,
        expected_revision=allowed["revision"],
        actor="test",
    )
    request = json.loads((root / ".orion-s0-jobs" / result["job_id"] / "request.json").read_text())
    assert calls == [root / batch]
    assert request["source_scope_preflight"]["validation_scope"] == "UPLOAD_MANIFEST_ONLY"
    assert request["source_snapshot"]["kind"] == "UPLOAD"
    snapshot = queued_source_snapshot(root, request)
    assert_reference_snapshot_root(snapshot)
    assert_reference_file(alias, snapshot["files"])
    # A valid additional upload still changes the approved job's exact selection.
    add("资料/后来添加.md", b"new source")
    with pytest.raises(ValueError, match="入队时"):
        queued_source_snapshot(root, request)
    with pytest.raises(ValueError, match="入队时"):
        asyncio.run(_run_job(root / ".orion-s0-jobs" / result["job_id"] / "request.json", None))
    # The real worker exits before its content profiling, routing, or OCR work.
    assert calls == [root / batch]
    with pytest.raises(ValueError, match="路径链"):
        assert_reference_snapshot_root(snapshot)


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_receipt",
        "wrong_name",
        "wrong_cas_path",
        "changed_blob",
        "changed_alias",
        "symlink",
        "extra_file",
        "extra_receipt",
    ],
)
def test_upload_receipt_and_content_tampering_fail_before_parse(upload, monkeypatch, tamper):
    root, batch, add = upload
    alias, blob, receipt = add()
    if tamper == "missing_receipt":
        receipt.unlink()
    elif tamper in {"wrong_name", "wrong_cas_path"}:
        value = json.loads(receipt.read_text())
        value["file_name" if tamper == "wrong_name" else "content_path"] = "../unselected.md"
        receipt.write_text(json.dumps(value))
    elif tamper == "changed_blob":
        blob.write_bytes(b"modified")
    elif tamper == "changed_alias":
        alias.write_bytes(b"modified")
    elif tamper == "symlink":
        alias.unlink()
        alias.symlink_to(blob)
    elif tamper == "extra_file":
        (alias.parent / "unregistered.md").write_text("unregistered")
    elif tamper == "extra_receipt":
        (receipt.parent / "extra.json").write_text("{}")
    with pytest.raises(WorkflowError, match="UPLOAD"):
        document_jobs.verified_document_source(root, batch)


def test_browser_preflight_provides_exact_scope_for_later_project_creation(upload, tmp_path):
    root, batch, add = upload
    add()
    add("资料/数据.csv", b"id,value\n1,2\n")
    service = OntologyWorkflowService(tmp_path / "workflows")
    _, result = OrionWorkflowTools(service).call(
        "preflight_workspace_snapshot", {"source_path": batch}
    )
    assert result["file_count"] == 2
    request = {
        "source_path": batch,
        "input_root": str(root),
        "intake_mode": "HYBRID",
        "source_snapshot": result["source_snapshot"],
    }
    selected = selected_document_source_scope(request)
    assert {item["source_path"] for item in selected["sources"]} == {
        f"{batch}/资料/规则.md",
        f"{batch}/资料/数据.csv",
    }
    assert {item["kind"] for item in selected["sources"]} == {"DOCUMENT", "STRUCTURED_FILE"}
    assert all(item["source_sha256"].startswith("sha256:") for item in selected["sources"])
    created = service.create_project(
        project_name="所选来源建项", domain="test", intake_mode="HYBRID", source_scope=selected
    )
    scope = json.loads(
        (
            tmp_path
            / "workflows"
            / created["project_id"]
            / "00-document-evidence/source-scope.json"
        ).read_text()
    )
    assert scope["declaration"] == "EXPLICIT"
    # Legacy jobs retain their old worker path unless they carry the new receipt.
    assert queued_source_snapshot(root, {"source_path": batch}) is None
    assert (
        selected_document_source_scope(
            {key: value for key, value in request.items() if key != "source_snapshot"}
        )
        == selected
    )


def test_other_upload_batches_do_not_invalidate_current_batch(upload):
    root, batch, add = upload
    add()
    snapshot = upload_snapshot_expectations(root, batch)
    (root / ".orion-s0-uploads/UPLOAD-99999999-NEWFILES").mkdir()
    (root / ".orion-s0-content/references/uploads/UPLOAD-99999999-NEWFILES").mkdir()
    assert_reference_snapshot_root(snapshot)


def test_legacy_path_job_registers_only_receipts_inside_the_selected_path(tmp_path):
    root = tmp_path / "intake"
    selected = root / "chosen"
    selected.mkdir(parents=True)
    original = selected / "rules.md"
    original.write_text("selected")
    digest = "sha256:" + hashlib.sha256(original.read_bytes()).hexdigest()
    request = {"input_root": str(root), "source_path": "chosen", "intake_mode": "DOCUMENT_ONLY"}
    documents = [
        {"document_id": "DOC-A", "source_path": "chosen/rules.md", "source_sha256": digest}
    ]
    declared = selected_document_source_scope(request, documents=documents)
    assert declared == {
        "sources": [{"kind": "DOCUMENT", "source_path": "chosen/rules.md", "source_sha256": digest}]
    }
    assert (
        selected_document_source_scope(
            {**request, "source_path": "chosen/rules.md"}, documents=documents
        )
        == declared
    )
    with pytest.raises(ValueError, match="超出用户明确选择"):
        selected_document_source_scope(
            request, documents=[{**documents[0], "source_path": "unselected/rules.md"}]
        )
