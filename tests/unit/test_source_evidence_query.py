from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from harness.orion_workflow_mcp import READ_ONLY_TOOL_NAMES, TOOLS, McpServer, OrionWorkflowTools
from services.ontology_engineering import OntologyWorkflowService, WorkflowError
from services.structured_data import source_evidence
from services.structured_data.pipeline import StructuredDataImportError, StructuredDataPipeline

PROJECT = "ontology-project-evidence-test"
HASH = "sha256:" + "a" * 64
DATASET = "DS-REGISTERED"
SCHEMA = {"tables": [{"schema": "orion_data", "name": "current_registered",
                      "physical_version_table": "ds_registered_001", "source_sheet": "检测记录",
                      "source_ref": f"dataset:{DATASET}:sheet:检测记录",
                      "columns": ["param_code", "param_value", "check_result"]}]}
INVENTORY = {"datasets": [{"project_id": PROJECT, "dataset_id": DATASET,
                          "document_id": "DOC-FILE", "status": "READY", "source_sha256": HASH}]}
RECEIPT = {"source_sha256": HASH, "sheets": [{"table_name": "ds_registered_001",
            "view_name": "current_registered", "source_name": "检测记录",
            "columns": [{"column_name": name} for name in SCHEMA["tables"][0]["columns"]]}]}


class Cursor:
    def __init__(self, rows=None, owner=(PROJECT, "READY")):
        self.rows = [("0", "OK", 7, 12, 3), ("1", "NG", 3, 12, 3), ("2", "NG", 2, 12, 3)] if rows is None else rows
        self.owner = owner
        self.calls = []

    def execute(self, statement, parameters=None):
        self.calls.append((statement if isinstance(statement, str) else statement.as_string(), parameters))

    def fetchone(self):
        return self.owner

    def fetchall(self):
        return self.rows


def install_reader(monkeypatch, *, rows=None, owner=(PROJECT, "READY"), receipt=None):
    cursor = Cursor(rows, owner)
    connection = type("Connection", (), {"cursor": lambda self: nullcontext(cursor)})()
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda url: nullcontext(connection))
    monkeypatch.setattr(StructuredDataPipeline, "_receipt", lambda *args, **kwargs: copy.deepcopy(receipt or RECEIPT))
    return cursor


def request(**overrides):
    return {"reader_url": "postgresql://reader@localhost/test", "project_id": PROJECT,
            "schema_snapshot": copy.deepcopy(SCHEMA), "datasource_inventory": copy.deepcopy(INVENTORY),
            "table": "current_registered", "group_by": ["param_value", "check_result"],
            "equals": {"param_code": "COMBINED-CODE"}, "max_groups": 2, **overrides}


@pytest.mark.parametrize("table", ["current_registered", "ds_registered_001", "orion_data.current_registered", "orion_data.ds_registered_001"])
def test_registered_aliases_use_same_physical_version_and_full_counts(monkeypatch, table):
    cursor = install_reader(monkeypatch)
    result = source_evidence.query_source_evidence_counts(**request(table=table))
    assert result["physical_version_table"] == "ds_registered_001"
    assert (result["matched_row_count"], result["total_group_count"], result["returned_group_count"]) == (12, 3, 2)
    assert result["truncated"] is True
    assert sum(group["count"] for group in result["groups"]) == 10  # Distinct from full 12.
    assert result["readonly_verified"] is True
    assert "READ ONLY" in cursor.calls[0][0]
    assert "statement_timeout = 15000" in cursor.calls[1][0]
    statement, parameters = cursor.calls[-1]
    assert 'FROM orion_data."ds_registered_001"' in statement
    assert "SUM(COUNT(*)) OVER ()" in statement
    assert '"param_code" IS NOT DISTINCT FROM %s' in statement
    assert "COMBINED-CODE" not in statement
    assert parameters == ["COMBINED-CODE", 3]


def test_no_matches_are_an_explicit_full_zero_not_missing_evidence(monkeypatch):
    install_reader(monkeypatch, rows=[])
    result = source_evidence.query_source_evidence_counts(**request())
    assert result["groups"] == []
    assert result["matched_row_count"] == result["total_group_count"] == 0
    assert result["truncated"] is False


@pytest.mark.parametrize("change", [
    {"table": "other_project"}, {"table": "current_registered; DELETE FROM x"},
    {"group_by": ["unregistered_secret"]}, {"group_by": ["param_value", "param_value"]},
    {"group_by": ["count(*)"]}, {"equals": {"secret": "x"}},
    {"equals": {"param_code": {"sql": "SELECT 1"}}}, {"equals": {"param_value": float("nan")}},
    {"max_groups": 0}, {"max_groups": True}, {"max_groups": 501}, {"reader_url": ""},
])
def test_invalid_queries_fail_before_opening_a_connection(monkeypatch, change):
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(StructuredDataImportError):
        source_evidence.query_source_evidence_counts(**request(**change))


def test_equal_value_is_data_even_if_it_contains_sql(monkeypatch):
    cursor = install_reader(monkeypatch)
    value = "x' OR 1=1; DELETE FROM hidden --"
    source_evidence.query_source_evidence_counts(**request(equals={"param_code": value}))
    assert value not in cursor.calls[-1][0]
    assert cursor.calls[-1][1][0] == value


@pytest.mark.parametrize("owner", [("other-project", "READY"), (PROJECT, "PENDING"), None])
def test_live_catalog_owner_and_status_are_required(monkeypatch, owner):
    cursor = install_reader(monkeypatch, owner=owner)
    with pytest.raises(StructuredDataImportError, match="实时目录 dataset"):
        source_evidence.query_source_evidence_counts(**request())
    assert len(cursor.calls) == 3


@pytest.mark.parametrize("field,value", [("source_sha256", "other-hash"), ("view_name", "other_view"), ("table_name", "other_table"), ("source_name", "other_sheet")])
def test_catalog_drift_rejects_query(monkeypatch, field, value):
    receipt = copy.deepcopy(RECEIPT)
    (receipt if field == "source_sha256" else receipt["sheets"][0])[field] = value
    cursor = install_reader(monkeypatch, receipt=receipt)
    with pytest.raises(StructuredDataImportError, match="身份不一致"):
        source_evidence.query_source_evidence_counts(**request())
    assert len(cursor.calls) == 3


def ready_service(tmp_path: Path, stage="S2", schema=None):
    service = OntologyWorkflowService(tmp_path, metadata_database_url="", metadata_required=False)
    created = service.create_project(project_name="只读证据核验", domain="evidence", intake_mode="HYBRID",
                                     source_scope={"sources": [{"kind": "STRUCTURED_FILE", "source_sha256": HASH}]})
    project_id = created["project_id"]
    project_dir = tmp_path / project_id
    s1_dir = project_dir / "01-data-understanding"
    s1_dir.mkdir(exist_ok=True)
    inventory = copy.deepcopy(INVENTORY)
    inventory["datasets"][0]["project_id"] = project_id
    for name, value in [("datasource-inventory", inventory), ("schema-snapshot", schema or SCHEMA), ("data-profile", {})]:
        (s1_dir / f"{name}.json").write_text(json.dumps(value))
    state = service._read_state(project_dir)
    state["current_stage"] = stage
    state["stage_statuses"].update({"S1": "PASSED", stage: "RUNNING"})
    state["stage_fingerprints"]["S1"] = {"output": service._stage_fingerprint(s1_dir)}
    service._save_state(project_dir, state)
    return service, project_id, project_dir


@pytest.mark.parametrize("stage", ["S2", "S3", "S4", "S5", "S6", "S7"])
def test_mcp_query_uses_real_scope_guards_and_preserves_all_project_assets(tmp_path, monkeypatch, stage):
    service, project_id, project_dir = ready_service(tmp_path, stage)
    state = service._read_state(project_dir)
    install_reader(monkeypatch, owner=(project_id, "READY"))
    monkeypatch.setenv("ORION_SOURCE_DATA_READER_URL", "postgresql://reader@localhost/test")
    monkeypatch.setenv("ORION_MCP_READ_ONLY", "true")
    before = {p.relative_to(project_dir): p.read_bytes() for p in project_dir.rglob("*") if p.is_file()}
    text, result = OrionWorkflowTools(service).call("query_source_evidence", {
        "project_id": project_id, "expected_revision": state["revision"],
        "table": "ds_registered_001", "group_by": ["param_value", "check_result"],
    })
    after = {p.relative_to(project_dir): p.read_bytes() for p in project_dir.rglob("*") if p.is_file()}
    assert before == after
    assert result["current_stage"] == stage and result["revision"] == state["revision"]
    assert "完整匹配 12 行" in text


@pytest.mark.parametrize("defect", ["stage", "revision", "s1_failed", "invalidated", "tamper", "scope"])
def test_workflow_rejects_untrusted_scope_before_database_access(tmp_path, monkeypatch, defect):
    service, project_id, project_dir = ready_service(tmp_path)
    state = service._read_state(project_dir)
    expected_revision = state["revision"]
    if defect == "stage":
        state["current_stage"] = "S1"
    elif defect == "revision":
        expected_revision -= 1
    elif defect == "s1_failed":
        state["stage_statuses"]["S1"] = "FAILED"
    elif defect == "invalidated":
        state["artifact_lifecycle"] = {"01-data-understanding/schema-snapshot.json": {"status": "INVALIDATED"}}
    elif defect == "tamper":
        (project_dir / "01-data-understanding/schema-snapshot.json").write_text("{}")
    elif defect == "scope":
        scope_path = project_dir / "00-document-evidence/source-scope.json"
        scope = json.loads(scope_path.read_text())
        scope["sources"][0]["source_sha256"] = "sha256:" + "b" * 64
        scope_path.write_text(json.dumps(scope))
    service._save_state(project_dir, state)
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(WorkflowError):
        service.query_source_evidence(project_id=project_id, expected_revision=expected_revision,
                                      table="current_registered", group_by=["param_code"])


def test_mcp_contract_is_read_only_and_has_no_sql_input():
    assert "query_source_evidence" in READ_ONLY_TOOL_NAMES
    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == "query_source_evidence")
    assert set(schema["properties"]) == {"project_id", "expected_revision", "table", "group_by", "equals", "max_groups", "query_plan", "describe_table"}
    assert schema["oneOf"][1]["required"] == ["query_plan"]
    assert "sql" not in schema["properties"]["query_plan"]["properties"]
    assert schema["additionalProperties"] is False
    assert "expected_revision" in schema["required"]


def test_real_database_scalars_survive_mcp_json_with_exact_precision_and_hash(tmp_path, monkeypatch):
    column_types = {"amount": "numeric", "day": "date", "observed_at": "timestamp",
                    "clock": "time", "passed": "boolean", "absent": "text"}
    schema = copy.deepcopy(SCHEMA)
    schema["tables"][0]["columns"] = list(column_types)
    service, project_id, project_dir = ready_service(tmp_path, schema=schema)
    receipt = copy.deepcopy(RECEIPT)
    receipt["sheets"][0]["columns"] = [{"column_name": name, "inferred_type": kind}
                                        for name, kind in column_types.items()]
    amount = Decimal("12345678901234567890.000000000000000000123400")
    row = (amount, date(2026, 9, 5), datetime(2026, 9, 5, 12, 34, 56, 123456, tzinfo=UTC),
           time(12, 34, 56, 123456, tzinfo=timezone(timedelta(hours=8))), True, None, 9, Decimal(9), 1)
    install_reader(monkeypatch, rows=[row], owner=(project_id, "READY"), receipt=receipt)
    monkeypatch.setenv("ORION_SOURCE_DATA_READER_URL", "postgresql://reader@localhost/test")
    response = McpServer(OrionWorkflowTools(service)).handle({
        "jsonrpc": "2.0", "id": 42, "method": "tools/call", "params": {
            "name": "query_source_evidence", "arguments": {
                "project_id": project_id, "expected_revision": service._read_state(project_dir)["revision"],
                "table": "current_registered", "group_by": list(column_types),
            },
        },
    })
    # Exercise both the TextContent projection and final stdio JSON boundary.
    wire = json.loads(json.dumps(response, ensure_ascii=False, allow_nan=False))
    assert "error" not in wire, wire
    result = wire["result"]["structuredContent"]
    assert result == json.loads(wire["result"]["content"][1]["text"])
    assert result["groups"] == [{"values": {
        "amount": str(amount), "day": "2026-09-05", "observed_at": "2026-09-05T12:34:56.123456+00:00",
        "clock": "12:34:56.123456+08:00", "passed": True, "absent": None,
    }, "count": 9}]
    assert result["group_columns"] == [{"name": name, "source_type": kind} for name, kind in column_types.items()]
    hashed = {name: result[name] for name in result["result_hash_fields"]}
    expected = hashlib.sha256(json.dumps(hashed, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert result["result_sha256"] == "sha256:" + expected


# --- SnapshotHub (database source) evidence ---------------------------------
SNAP_SET = "SS-TESTSNAPSHOT"
SNAP_DS = "DS-SNAPSHOT"
MANIFEST = "sha256:" + "c" * 64
SNAP_SCHEMA = {"database": "orion_source_data", "schemas": ["orion_data"], "snapshot_set_id": SNAP_SET,
               "tables": [{"name": "ms_src_plant_abc", "source_id": "src", "source_table": "plant",
                           "dataset_id": SNAP_DS,
                           "columns": ["dataset_id", "row_ordinal", "row_sha256", "plant_code", "city"],
                           "column_types": {"plant_code": "text", "city": "text"}}]}
SNAP_INVENTORY = {"datasets": [{"project_id": PROJECT, "dataset_id": SNAP_DS, "source_id": "src",
                                "snapshot_set_id": SNAP_SET, "status": "READY", "source_sha256": HASH,
                                "manifest_sha256": MANIFEST, "registered_via": "ORION_SNAPSHOT_HUB"}]}


class SnapshotCursor(Cursor):
    def __init__(self, snapshot_set=(PROJECT, True, MANIFEST), snapshot=None):
        super().__init__(rows=[("北京", 4, 6, 2), ("上海", 2, 6, 2)])
        self.answers = [snapshot_set, snapshot if snapshot is not None else (
            PROJECT, HASH, True, {"tables": [{"table": "plant", "target_table": "ms_src_plant_abc"}]})]

    def fetchone(self):
        return self.answers.pop(0)


def install_snapshot_reader(monkeypatch, **kwargs):
    cursor = SnapshotCursor(**kwargs)
    connection = type("Connection", (), {"cursor": lambda self: nullcontext(cursor)})()
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda url: nullcontext(connection))
    return cursor


def snapshot_request(**overrides):
    return request(**{"schema_snapshot": copy.deepcopy(SNAP_SCHEMA), "datasource_inventory": copy.deepcopy(SNAP_INVENTORY),
                      "table": "plant", "group_by": ["city"], "equals": None, "max_groups": 10, **overrides})


@pytest.mark.parametrize("table", ["plant", "ms_src_plant_abc", "orion_data.ms_src_plant_abc", "src.plant"])
def test_snapshot_table_aliases_pin_the_s1_dataset_version(monkeypatch, table):
    cursor = install_snapshot_reader(monkeypatch)
    result = source_evidence.query_source_evidence_counts(**snapshot_request(table=table))
    assert result["physical_version_table"] == "ms_src_plant_abc"
    assert result["registered_via"] == "ORION_SNAPSHOT_HUB" and result["snapshot_set_id"] == SNAP_SET
    assert result["matched_row_count"] == 6 and result["truncated"] is False
    assert "READ ONLY" in cursor.calls[0][0]
    statement, parameters = cursor.calls[-1]
    assert 'FROM orion_data."ms_src_plant_abc" WHERE "dataset_id" = %s' in statement
    assert parameters == [SNAP_DS, 11]


@pytest.mark.parametrize("change", [{"group_by": ["row_sha256"]}, {"group_by": ["missing"]},
                                    {"equals": {"dataset_id": "DS-OTHER"}}])
def test_snapshot_rejects_system_or_unregistered_columns_before_connecting(monkeypatch, change):
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(StructuredDataImportError):
        source_evidence.query_source_evidence_counts(**snapshot_request(**change))


@pytest.mark.parametrize("field,value", [("registered_via", "FILE"), ("status", "PENDING"),
                                         ("project_id", "other"), ("snapshot_set_id", "SS-OTHER")])
def test_snapshot_inventory_identity_is_required(monkeypatch, field, value):
    monkeypatch.setattr(source_evidence.psycopg, "connect", lambda *args: pytest.fail("must not connect"))
    inventory = copy.deepcopy(SNAP_INVENTORY)
    inventory["datasets"][0][field] = value
    with pytest.raises(StructuredDataImportError, match="快照 dataset"):
        source_evidence.query_source_evidence_counts(**snapshot_request(datasource_inventory=inventory))


@pytest.mark.parametrize("kwargs", [
    {"snapshot_set": None}, {"snapshot_set": ("other", True, MANIFEST)},
    {"snapshot_set": (PROJECT, True, "sha256:" + "d" * 64)},
    {"snapshot": (PROJECT, "sha256:" + "e" * 64, True, {"tables": [{"table": "plant", "target_table": "ms_src_plant_abc"}]})},
    {"snapshot": (PROJECT, HASH, True, {"tables": [{"table": "line", "target_table": "ms_src_plant_abc"}]})},
])
def test_snapshot_catalog_drift_rejects_query(monkeypatch, kwargs):
    cursor = install_snapshot_reader(monkeypatch, **kwargs)
    with pytest.raises(StructuredDataImportError, match="实时快照"):
        source_evidence.query_source_evidence_counts(**snapshot_request())
    assert not any("GROUP BY" in call[0] for call in cursor.calls)

