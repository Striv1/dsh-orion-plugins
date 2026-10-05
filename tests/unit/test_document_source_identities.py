from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from harness.orion_workflow_mcp import OrionWorkflowTools
from services.ingestion.document_jobs import enqueue_document_job
from services.ontology_engineering import OntologyWorkflowService, WorkflowError


def checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def selection(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in {
        "说明.md": "# 本体建设资料",
        "副本/说明.md": "# 本体建设资料",
        "检验.csv": "id,result\n1,NG\n",
        "工艺.csv": "id,limit\n1,1\n",
        "参数.tsv": "id\tvalue\n1\t2\n",
        "业务规则.md": "# 参数大于1需要复核",
    }.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    root = tmp_path / "input"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(root))
    monkeypatch.delenv("ORION_REQUIRE_UI_CREATE_CONFIRMATION", raising=False)
    service = OntologyWorkflowService(
        tmp_path / "workflows",
        metadata_database_url="",
        metadata_required=False,
        release_deployment_automation=object(),
    )
    api = OrionWorkflowTools(service)
    _, snapshot = api.call(
        "snapshot_workspace_sources",
        {
            "workspace_root": str(workspace),
            "references": list(p.name for p in workspace.iterdir()),
        },
    )
    return service, api, root, snapshot


def create_broken(selection, amend=None, **kwargs):
    service, _, _, snapshot = selection
    scope = deepcopy(snapshot["source_scope"])
    scope["business_goal"] = "从六份资料建设业务本体"
    for index, row in enumerate(scope["sources"]):
        row["source_id"] = f"model-invented-{index}"
    if amend:
        amend(scope)
    result = service.create_project(
        project_name="来源编号恢复",
        domain="source-identity",
        intake_mode="HYBRID",
        source_scope=scope,
        **kwargs,
    )
    return result


def arguments(selection, state):
    return dict(
        project_id=state["project_id"],
        source_path=selection[3]["source_path"],
        expected_revision=state["revision"],
        actor="工程负责人",
        reason="纠正模型自编技术编号",
    )


def files(project: Path):
    return {
        p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()
    }


def test_verified_scope_can_create_duplicate_content_and_queue_without_invented_ids(selection):
    service, api, root, snapshot = selection
    _, preflight = api.call(
        "preflight_workspace_snapshot", {"source_path": snapshot["source_path"]}
    )
    assert preflight["source_scope"] == snapshot["source_scope"]
    assert len(snapshot["source_scope"]["sources"]) == 6
    assert all("source_id" not in row for row in snapshot["source_scope"]["sources"])
    _, created = api.call(
        "create_ontology_project",
        {
            "project_name": "真实来源登记",
            "domain": "verified-sources",
            "intake_mode": "HYBRID",
            "source_scope": snapshot["source_scope"],
        },
    )
    queued = enqueue_document_job(
        service, **{k: v for k, v in arguments(selection, created).items() if k != "reason"}
    )
    assert queued["status"] == "QUEUED"
    request = json.loads((root / ".orion-s0-jobs" / queued["job_id"] / "request.json").read_text())
    assert request["source_scope_preflight"]["status"] == "PASSED"
    assert service.get_status(created["project_id"])["stage_statuses"]["S0"] == "RUNNING"


def test_reconcile_audits_only_wrong_ids_then_existing_queue_accepts(selection, monkeypatch):
    service, api, _, snapshot = selection
    state = create_broken(selection)
    project = service.root / state["project_id"]
    scope_path = project / "00-document-evidence/source-scope.json"
    original_bytes = scope_path.read_bytes()
    original = json.loads(original_bytes)
    params = arguments(selection, state)
    with pytest.raises(WorkflowError, match="唯一授权"):
        enqueue_document_job(service, **{k: v for k, v in params.items() if k != "reason"})
    syncs = []
    sync = service._sync_metadata_store

    def record_sync(path):
        syncs.append(path)
        return sync(path)

    monkeypatch.setattr(service, "_sync_metadata_store", record_sync)
    _, result = api.call("reconcile_document_source_identities", params)
    assert result["revision"] == state["revision"] + 1
    assert result["current_stage"] == "S0"
    assert result["stage_statuses"]["S0"] == "RUNNING"
    assert "S0" not in result["stage_fingerprints"]
    assert syncs == [project]
    revised = json.loads(scope_path.read_bytes())
    expected = deepcopy(original)
    for row in expected["sources"]:
        row.pop("source_id")
    assert revised == expected
    assert json.loads((project / "project.json").read_text())["source_scope"] == revised
    receipt = result["source_identity_reconciliation"]
    assert receipt["authorization_scope_changed"] is False
    assert len(receipt["changes"]) == 6
    assert all(
        item["source_id"] is None and item["verified_document_id"].startswith("DOC-")
        for item in receipt["changes"]
    )
    assert (project / receipt["previous_scope_path"]).read_bytes() == original_bytes
    assert receipt["source_scope_sha256"] == checksum(scope_path)
    assert receipt["source_snapshot"]["files"] == snapshot["files"]
    events = service._read_events(project)
    assert events[-1]["event_type"] == "DOCUMENT_SOURCE_IDENTITIES_RECONCILED"
    assert events[-1]["details"]["actor"] == "工程负责人"
    manifest = json.loads((project / "artifact-manifest.json").read_text())
    for item in manifest["files"]:
        assert item["sha256"] == checksum(project / item["path"])
    assert service.verify_project_integrity(state["project_id"])["status"] == "PASSED"
    assert (
        "S0 未通过" in (project / "00-document-evidence/document-evidence-report.html").read_text()
    )
    queued = enqueue_document_job(
        service, **{k: v for k, v in arguments(selection, result).items() if k != "reason"}
    )
    assert queued["status"] == "QUEUED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_path", "unrelated.md"),
        ("source_sha256", "sha256:" + "e" * 64),
        ("source_name", "other.md"),
        ("kind", "DOCUMENT"),
        ("path_scope", "DIRECTORY"),
        ("source_name", None),
        ("source_path", None),
        ("source_sha256", None),
    ],
)
def test_reconcile_refuses_missing_or_changed_file_identity(selection, field, value):
    def amend(scope):
        row = next(item for item in scope["sources"] if item["kind"] == "STRUCTURED_FILE")
        if value is None:
            row.pop(field)
        else:
            row[field] = value

    state = create_broken(selection, amend)
    service = selection[0]
    project = service.root / state["project_id"]
    before = files(project)
    with pytest.raises(WorkflowError):
        service.reconcile_document_source_identities(**arguments(selection, state))
    assert files(project) == before


@pytest.mark.parametrize(
    "case",
    [
        "database",
        "missing_file",
        "duplicate_path",
        "excluded_id",
        "excluded_path",
        "excluded_id_prefix",
    ],
)
def test_reconcile_never_expands_authorization(selection, case):
    def amend(scope):
        if case == "database":
            scope["sources"].append(
                {"kind": "DATABASE", "database": "erp", "authorized_tables": ["orders"]}
            )
        elif case == "missing_file":
            scope["sources"].pop()
        elif case == "duplicate_path":
            scope["sources"][1] = {**scope["sources"][0], "source_id": "different-alias"}
        elif case == "excluded_id_prefix":
            scope["sources"][0]["source_id"] = "excluded/business-doc"
            scope["excluded_source_refs"] = ["excluded/"]
        else:
            key = "source_id" if case == "excluded_id" else "source_path"
            scope["excluded_source_refs"] = [scope["sources"][0][key]]

    state = create_broken(selection, amend)
    service = selection[0]
    project = service.root / state["project_id"]
    before = files(project)
    with pytest.raises(WorkflowError):
        service.reconcile_document_source_identities(**arguments(selection, state))
    assert files(project) == before


@pytest.mark.parametrize(
    "condition", ["v1", "stale", "submitted", "historical_pass", "project_mismatch", "no_change"]
)
def test_reconcile_requires_unsubmitted_current_v2(selection, condition):
    service = selection[0]
    state = create_broken(
        selection,
        **({"_stage_contract_version": "s0-s7-stage-contract-v1"} if condition == "v1" else {}),
    )
    project = service.root / state["project_id"]
    if condition == "submitted":
        (project / "00-document-evidence/document-register.json").write_text("[]")
    elif condition == "historical_pass":
        service._append_event(project, "STAGE_PASSED", state, {"stage": "S0"})
    elif condition == "project_mismatch":
        data = service._read_json(project / "project.json")
        data["source_scope"]["sources"][0]["source_id"] = "drifted"
        service._write_json(project / "project.json", data)
    elif condition == "no_change":
        service.reconcile_document_source_identities(**arguments(selection, state))
        state = service.get_status(state["project_id"])
    params = arguments(selection, state)
    if condition == "stale":
        params["expected_revision"] += 1
    before = files(project)
    with pytest.raises(WorkflowError):
        service.reconcile_document_source_identities(**params)
    assert files(project) == before


@pytest.mark.parametrize(
    "status,runner",
    [
        ("QUEUED", "QUEUED"),
        ("READY_FOR_REVIEW", "COMPLETED"),
        ("FAILED", "RUNNING"),
        ("FAILED", "STARTING"),
    ],
)
def test_reconcile_refuses_active_jobs_even_without_project_pointer(selection, status, runner):
    service, _, root, _ = selection
    state = create_broken(selection)
    job = root / ".orion-s0-jobs/JOB-12345678"
    job.mkdir(parents=True)
    for name, value in {
        "request": {"project_id": state["project_id"]},
        "status": {"status": status},
        "runner": {"state": runner},
    }.items():
        (job / (name + ".json")).write_text(json.dumps(value))
    project = service.root / state["project_id"]
    before = files(project)
    with pytest.raises(WorkflowError, match="活动"):
        service.reconcile_document_source_identities(**arguments(selection, state))
    assert files(project) == before


def test_reconcile_rejects_source_tampering_and_storage_block_before_writes(selection, monkeypatch):
    service, _, root, snapshot = selection
    state = create_broken(selection)
    project = service.root / state["project_id"]
    before = files(project)

    def block(_path):
        raise WorkflowError("ledger readback stale")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_require_current_storage_before_mutation", block)
        with pytest.raises(WorkflowError, match="ledger"):
            service.reconcile_document_source_identities(**arguments(selection, state))
    path = root / snapshot["source_path"] / snapshot["files"][0]["path"]
    path.chmod(0o600)
    path.write_text("tampered source")
    with pytest.raises(WorkflowError, match="快照校验失败"):
        service.reconcile_document_source_identities(**arguments(selection, state))
    assert files(project) == before


def test_competing_reconcile_calls_commit_only_one_revision(selection):
    service = selection[0]
    state = create_broken(selection)
    second = OntologyWorkflowService(
        service.root,
        metadata_database_url="",
        metadata_required=False,
        release_deployment_automation=object(),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(s.reconcile_document_source_identities, **arguments(selection, state))
            for s in (service, second)
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except WorkflowError:
                outcomes.append(None)
    assert sum(result is not None for result in outcomes) == 1
    assert service.get_status(state["project_id"])["revision"] == state["revision"] + 1
    assert (
        sum(
            event["event_type"] == "DOCUMENT_SOURCE_IDENTITIES_RECONCILED"
            for event in service._read_events(service.root / state["project_id"])
        )
        == 1
    )


def test_mcp_rejects_invented_file_id_before_project_creation(selection):
    service, api, _, snapshot = selection
    scope = deepcopy(snapshot["source_scope"])
    scope["sources"][0]["source_id"] = "invented-business-label"
    with pytest.raises(WorkflowError, match="工程尚未创建"):
        api.call(
            "create_ontology_project",
            {
                "project_name": "创建前拦截",
                "domain": "guard",
                "intake_mode": "HYBRID",
                "source_scope": scope,
            },
        )
    assert not list(service.root.glob("*/project.json"))
