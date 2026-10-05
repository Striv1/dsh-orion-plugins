import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.wren_project_tools import call_project_tool
from services.realtime_qa.analytics_api import register_analytics_api
from services.realtime_qa.analytics_models import AnalyticsQueryRequest
from tests.unit.test_wren_analysis import setup


def client_fixture(tmp_path, monkeypatch, *, truncated=False):
    monkeypatch.setattr("services.structured_data.source_registration.DEFAULT_REGISTRY_ROOT", tmp_path / "sources")
    _, payload, store, runtime, release = setup(tmp_path)
    runtime.ensure_available = lambda: None
    runtime.binding = runtime.binding.model_copy(update={"snapshot_set_id": "SS-TEST"})
    descriptor = {"key": "project-1", "adapter_version": "test", "mdl_sha256": "sha256:" + "c" * 64,
        "source_tables": [{"name": "st_0", "source_id": "source1", "source_table": "orders", "dataset_id": "DS-A", "expected_count": 10_000_000}],
        "models": [{"name": "Sale"}], "cubes": [], "relationships": [], "views": [], "knowledge": {}, "omitted": []}
    output = {"rows": [{"month": "2026-09", "total": "123456.78"}], "variables": ["month", "total"],
        "row_types": {"month": "string", "total": "decimal128(38, 2)"}, "truncated": truncated,
        "sql": 'SELECT month,SUM(amount) AS total FROM Sale GROUP BY month', "expanded_sql": "checked",
        "mdl_sha256": descriptor["mdl_sha256"], "engine_version": "0.15.0", "core_version": "0.8.0"}
    monkeypatch.setattr("services.realtime_qa.wren_project.build_project", lambda _: descriptor)
    calls = []
    def run(*args):
        calls.append(args[-1])
        return copy.deepcopy(output)
    monkeypatch.setattr("services.realtime_qa.analytics_api.run_project_query", run)
    from services.realtime_qa.analysis_assets import AnalysisAssetStore
    app = FastAPI()
    register_analytics_api(app, store, lambda _: runtime, assets=AnalysisAssetStore(tmp_path / "assets"))
    identity = {k: payload[k] for k in ("session_id", "expected_release")}
    request = {**identity, "question": "每月销售额", "sql": output["sql"], "chart": {"kind": "line", "x": "month", "y": ["total"]}}
    return TestClient(app), request, store, release, calls


def test_source_aggregation_receipt_and_report_readback(tmp_path, monkeypatch):
    client, request, store, _, _ = client_fixture(tmp_path, monkeypatch)
    result = client.post("/ontology/realtime/session-analytics-query", json=request)
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["analysis"]["input_sampled"] is False
    assert result["analysis"]["source_row_count"] == 10_000_000
    receipt = result["evidence_receipt"]
    envelope = json.loads(store.read(receipt))
    assert envelope["response"]["answer"]["evidence_bundle"]["evidence"][0]["payload"]["cq_results"][0]["rows"] == result["rows"]
    ref = {k: receipt[k] for k in ("receipt_id", "sha256", "query_id")}
    identity = {k: request[k] for k in ("session_id", "expected_release")}
    saved = client.post("/ontology/realtime/session-analytics-assets", json={**identity, "action": "report", "title": "销售月报", "receipts": [ref]}).json()
    restored = client.post("/ontology/realtime/session-analytics-assets", json={**identity, "action": "get", "asset_id": saved["asset"]["asset_id"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["results"][0]["rows"] == result["rows"]
    assert restored.json()["asset"]["approval_status"] == "NOT_BUSINESS_APPROVED"


def test_confirmation_memory_and_real_replay_evaluation(tmp_path, monkeypatch):
    client, request, _, _, calls = client_fixture(tmp_path, monkeypatch)
    receipt = client.post("/ontology/realtime/session-analytics-query", json=request).json()["evidence_receipt"]
    ref = {k: receipt[k] for k in ("receipt_id", "sha256", "query_id")}
    identity = {k: request[k] for k in ("session_id", "expected_release")}
    body = {**identity, "action": "remember", "question": "每月销售额", "receipts": [ref]}
    assert client.post("/ontology/realtime/session-analytics-assets", json=body).status_code == 422
    assert client.post("/ontology/realtime/session-analytics-assets", json={**body, "confirmed": True}).status_code == 200
    memories = client.post("/ontology/realtime/session-analytics-assets", json={**identity, "action": "search", "question": "销售"}).json()["memories"]
    assert memories and memories[0]["semantic_embedding"] is False
    baseline = client.post("/ontology/realtime/session-analytics-assets", json={**body, "action": "evaluation", "confirmed": True}).json()["asset"]
    replay = client.post("/ontology/realtime/session-analytics-assets", json={**identity, "action": "evaluate", "asset_id": baseline["asset_id"]})
    assert replay.status_code == 200, replay.text
    assert len(calls) == 2
    assert replay.json()["execution"]["evidence_receipt"]["receipt_id"] != receipt["receipt_id"]


@pytest.mark.parametrize("change", ["receipt_hash", "receipt_session", "release", "revoked", "truncated"])
def test_unusable_receipt_cannot_become_memory(tmp_path, monkeypatch, change):
    client, request, _, release, _ = client_fixture(tmp_path, monkeypatch, truncated=change == "truncated")
    receipt = client.post("/ontology/realtime/session-analytics-query", json=request).json()["evidence_receipt"]
    ref = {k: receipt[k] for k in ("receipt_id", "sha256", "query_id")}
    body = {k: request[k] for k in ("session_id", "expected_release")}
    body.update(action="remember", receipts=[ref], confirmed=True)
    if change == "receipt_hash":
        ref["sha256"] = "sha256:" + "f" * 64
    elif change == "receipt_session":
        body["session_id"] = "session-22222222-2222-2222-2222-222222222222"
    elif change == "release":
        body["expected_release"]["release_version"] = "2.0"
    elif change == "revoked":
        (release / "release-revocation.json").write_text('{"status":"REVOKED"}')
    assert client.post("/ontology/realtime/session-analytics-assets", json=body).status_code == 409


def test_catalog_does_not_claim_input_limit_or_serialize_connections(tmp_path, monkeypatch):
    client, request, _, _, _ = client_fixture(tmp_path, monkeypatch)
    result = client.post("/ontology/realtime/session-analytics-catalog", json={k: request[k] for k in ("session_id", "expected_release")})
    assert result.status_code == 200
    assert result.json()["input_row_limit"] is None
    assert "connection_info" not in result.text


def test_query_shape_rejects_double_plan_and_oversized_output():
    identity = {"session_id": "session-11111111-1111-1111-1111-111111111111",
                "expected_release": {"project_id": "test-project", "release_version": "1.0", "release_fingerprint": "sha256:" + "1" * 64}}
    for addition in ({"sql": "select 1", "cube_query": {}}, {"sql": "select 1", "limit": 501}, {"sql": "select 1", "connection_info": {}}):
        with pytest.raises(ValueError):
            AnalyticsQueryRequest.model_validate({**identity, "question": "每月销售", **addition})


def test_mcp_validates_returned_identity_and_receipt(tmp_path, monkeypatch):
    client, request, _, _, _ = client_fixture(tmp_path, monkeypatch)
    args = {**request, **request["expected_release"]}
    args.pop("expected_release")
    result = call_project_tool("query_ontology_analytics", args, lambda path, body: client.post(path, json=body).json())
    assert "ORION_EVIDENCE_RECEIPT_V1 " in result["content"][0]["text"]
    with pytest.raises(ValueError, match="身份"):
        call_project_tool("query_ontology_analytics", args, lambda *_: {"session_id": "other"})


def test_catalog_preserves_semantic_details_and_exposes_authorized_lineage(tmp_path, monkeypatch):
    from services.realtime_qa import wren_project

    client, request, _, _, _ = client_fixture(tmp_path, monkeypatch)
    descriptor = wren_project.build_project(None)
    descriptor["source_tables"][0]["columns"] = ["order_id", "amount"]
    descriptor["source_tables"][0]["connection_info"] = {"password": "SOURCE_SECRET_SENTINEL"}
    descriptor["models"] = [{"name": "Sale", "primaryKey": "_key", "refSql": 'SELECT order_id, amount FROM approved.orders',
        "columns": [{"name": "_key", "type": "varchar"}, {"name": "amount", "type": "decimal"},
                    {"name": "customerName", "type": "varchar", "isCalculated": True, "expression": 'customer.name'}]}]
    descriptor["relationships"] = [{"name": "sale_customer", "models": ["Sale", "Customer"],
        "joinType": "MANY_TO_ONE", "condition": 'Sale.customer_id = Customer._key'}]
    descriptor["views"] = [{"name": "SaleRows", "statement": 'SELECT amount FROM Sale'}]
    descriptor["cubes"] = [{"name": "Sales", "baseObject": "Sale", "measures": [
        {"name": "total", "type": "decimal", "expression": 'SUM(amount)'}],
        "dimensions": [{"name": "customer", "type": "varchar", "expression": 'customerName'}],
        "timeDimensions": [{"name": "month", "type": "date", "expression": 'saleDate'}]}]
    descriptor["model_projections"] = [{"model": "Sale", "source_name": "st_0", "identity_columns": ["order_id"],
        "connection_info": {"password": "PROJECTION_SECRET_SENTINEL"},
        "columns": [{"physical": "order_id", "name": "_key", "type": "varchar", "private": "COLUMN_SECRET_SENTINEL"},
                    {"physical": "amount", "name": "amount", "type": "decimal"},
                    {"physical": "UNAUTHORIZED_COLUMN_SENTINEL", "name": "amount", "type": "decimal"}]},
        {"model": "UNPUBLISHED_MODEL_SENTINEL", "source_name": "st_0", "columns": []},
        {"model": "Sale", "source_name": "UNBOUND_SOURCE_SENTINEL", "columns": []}]
    before = copy.deepcopy(descriptor)
    identity = {key: request[key] for key in ("session_id", "expected_release")}
    response = client.post("/ontology/realtime/session-analytics-catalog", json=identity)
    assert response.status_code == 200, response.text
    data = response.json()
    for key in ("models", "relationships", "views", "cubes"):
        assert data[key] == descriptor[key]
    assert data["sources"][0]["name"] == "st_0"
    assert data["sources"][0]["columns"] == ["order_id", "amount"]
    assert data["model_projections"] == [{"model": "Sale", "source_name": "st_0", "identity_columns": ["order_id"],
        "columns": [{"physical": "order_id", "name": "_key", "type": "varchar"},
                    {"physical": "amount", "name": "amount", "type": "decimal"}]}]
    assert "SENTINEL" not in response.text
    assert descriptor == before


def test_catalog_lineage_does_not_guess_legacy_model_source_mapping(tmp_path, monkeypatch):
    client, request, _, _, _ = client_fixture(tmp_path, monkeypatch)
    response = client.post("/ontology/realtime/session-analytics-catalog", json={
        key: request[key] for key in ("session_id", "expected_release")})
    assert response.status_code == 200
    assert response.json()["model_projections"] == []
    assert response.json()["sources"][0]["name"] == "st_0"
