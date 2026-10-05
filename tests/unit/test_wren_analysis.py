import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness import realtime_qa_mcp
from services.realtime_qa.models import RealtimeSessionAnswer
from services.realtime_qa.result_pages import register_result_page_api
from services.realtime_qa.wren_analysis import WREN_PYTHON, describe_columns, load_source
from services.realtime_qa.wren_api import register_wren_api
from services.realtime_qa.wren_models import AnalysisRequest, AnalysisSource
from tests.unit.test_core_realtime_api import _Runtime
from tests.unit.test_realtime_evidence_receipts import stored_answer


def setup(tmp_path, rows=None, *, complete=True, truncated=False, reported=None, rdf_terms=None):
    rows = (
        rows
        if rows is not None
        else [
            {"id": "a", "车间": "甲", "值": 7, "日期": "2026-01-01"},
            {"id": "a", "车间": "甲", "值": 8, "日期": "2026-01-02"},
            {"id": "b", "车间": None, "值": None, "日期": "2026-02-01"},
        ]
    )
    store, _, answer = stored_answer(tmp_path)
    bundle = answer["answer"]["evidence_bundle"]
    release = tmp_path / bundle["release"]["project_id"] / "07-release"
    package = release / "package"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text('{"files":[]}')
    fingerprint = "sha256:" + hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    bundle["release"].update(package_path=str(package), release_fingerprint=fingerprint)
    answer["runtime_binding"]["release_fingerprint"] = fingerprint
    (release / "publication.json").write_text(
        json.dumps(
            {
                "approval_decision": "APPROVED",
                "release_version": bundle["release"]["release_version"],
                "integrity_verification_status": "PASSED",
                "package_path": "07-release/package",
            }
        )
    )
    (release / "gate-results.json").write_text('{"stage":"S7","status":"PASSED"}')
    bundle["complete"] = complete
    bundle["evidence"] = [
        {
            "evidence_id": "EV-source",
            "source_kind": "document",
            "source_ref": "source-facts",
            "observed_at": "2026-09-28T00:00:00Z",
            "payload": {
                "cq_results": [
                    {
                        "capability_name": "sample",
                        "rows": rows,
                        "row_count": len(rows) if reported is None else reported,
                        "truncated": truncated,
                    }
                ]
            },
        }
    ]
    if rdf_terms is not None:
        record = bundle["evidence"][0]
        record["source_kind"] = "structured_db"
        record["payload"] = {"query_template": "sample", "rows": rows, "row_count": len(rows),
                             "variables": sorted({key for row in rows for key in row}), "row_terms": rdf_terms}
    response = RealtimeSessionAnswer.model_validate(answer)
    reference = store.save(response)
    runtime = _Runtime()
    runtime.binding = response.answer.evidence_bundle.release
    app = FastAPI()
    register_wren_api(app, store, lambda _: runtime)
    register_result_page_api(app, store, lambda _: runtime)
    payload = {
        "session_id": reference["session_id"],
        "source_query_id": reference["query_id"],
        "receipt_id": reference["receipt_id"],
        "receipt_sha256": reference["sha256"],
        "expected_release": {
            key: reference[key] for key in ("project_id", "release_version", "release_fingerprint")
        },
        "capability_name": "sample",
    }
    return TestClient(app), payload, store, runtime, release


def plan(payload, **kwargs):
    return {
        **payload,
        "question": "按车间统计记录和不同对象",
        "dimensions": [{"field": "车间"}],
        "metrics": [
            {"name": "m_count", "operation": "count_rows"},
            {"name": "m_objects", "operation": "count_distinct", "field": "id"},
            {"name": "m_sum", "operation": "sum", "field": "值"},
        ],
        **kwargs,
    }


@pytest.mark.parametrize(
    "change",
    [
        "incomplete",
        "truncated",
        "count",
        "limit",
        "session",
        "hash",
        "version",
        "revoked",
        "path",
        "sql",
    ],
)
def test_ineligible_sources_and_scope_fail_before_worker(tmp_path, monkeypatch, change):
    kwargs = (
        {"complete": False}
        if change == "incomplete"
        else {"truncated": True}
        if change == "truncated"
        else {"reported": 999}
        if change == "count"
        else {}
    )
    client, payload, _, _, release = setup(tmp_path, **kwargs)
    request = plan(payload)
    if change == "limit":
        request["limit"] = True
    if change == "session":
        request["session_id"] = "session-22222222-2222-2222-2222-222222222222"
    if change == "hash":
        request["receipt_sha256"] = "sha256:" + "f" * 64
    if change == "version":
        request["expected_release"]["release_version"] = "other"
    if change == "revoked":
        (release / "release-revocation.json").write_text('{"status":"REVOKED"}')
    if change == "path":
        request["database_path"] = "/etc/passwd"
    if change == "sql":
        request["sql"] = "DELETE FROM source_rows"
    monkeypatch.setattr(
        "services.realtime_qa.wren_api.run_analysis",
        lambda *args: pytest.fail("worker must not run"),
    )
    result = client.post("/ontology/realtime/session-analysis", json=request)
    assert result.status_code in {409, 422}, result.text


def test_pagination_preserves_complete_rows_and_type_boundaries(tmp_path):
    rows = [{"id": f"C{i}", "value": i, "missing": None} for i in range(121)]
    _, payload, store, runtime, _ = setup(tmp_path, rows)
    loaded = load_source(store, runtime, AnalysisSource.model_validate(payload))
    assert loaded["rows"] == rows
    assert next(c for c in loaded["columns"] if c["field"] == "missing")["null_count"] == 121
    for invalid in (
        [{"x": {"nested": 1}}],
        [{"x": "1"}, {"x": 1}],
        [{"x": float("nan")}],
        [{"x": 2**64}],
        [{"x": 10**400}],
    ):
        with pytest.raises(ValueError):
            describe_columns(invalid)


def test_rdf_metadata_is_preserved_through_pagination_without_guessing(tmp_path):
    rows = [{"days": str(i), "code": str(i)} for i in range(121)]
    terms = [{key: {"type": "literal", "value": value, "datatype": "http://www.w3.org/2001/XMLSchema#" + ("integer" if key == "days" else "string")}
              for key, value in row.items()} for row in rows]
    _, payload, store, runtime, _ = setup(tmp_path, rows, rdf_terms=terms)
    loaded = load_source(store, runtime, AnalysisSource.model_validate(payload))
    assert loaded["rows"] == rows
    assert loaded["analysis_rows"] == [{"days": i, "code": str(i)} for i in range(121)]
    assert {column["field"]: column["type"] for column in loaded["columns"]} == {"days": "bigint", "code": "varchar"}
    assert loaded["type_decoding"]["basis"] == "RDF_LITERAL_METADATA"


@pytest.mark.parametrize("defect", ["value", "length", "invalid_integer"])
def test_rdf_metadata_must_match_complete_lexical_rows(tmp_path, defect):
    terms = [{"days": {"type": "literal", "value": "7", "datatype": "http://www.w3.org/2001/XMLSchema#integer"}}]
    rows = [{"days": "7"}]
    if defect == "value":
        terms[0]["days"]["value"] = "8"
    elif defect == "length":
        terms.append(terms[0])
    else:
        rows[0]["days"] = terms[0]["days"]["value"] = "seven"
    client, payload, _, _, _ = setup(tmp_path, rows, rdf_terms=terms)
    assert client.post("/ontology/realtime/session-analysis-context", json=payload).status_code == 409


@pytest.mark.skipif(not WREN_PYTHON.exists(), reason="official optional Wren engine is required")
def test_official_wren_averages_rdf_integers_and_retains_original_detail(tmp_path):
    rows = [{"days": "0"}, {"days": "8"}]
    terms = [{"days": {"type": "literal", "value": row["days"], "datatype": "http://www.w3.org/2001/XMLSchema#integer"}} for row in rows]
    client, payload, _, _, _ = setup(tmp_path, rows, rdf_terms=terms)
    response = client.post("/ontology/realtime/session-analysis", json=plan(payload, dimensions=[], metrics=[{"name": "m_avg", "operation": "avg", "field": "days"}]))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rows"] == [{"m_avg": 4.0}]
    assert result["analysis"]["detail_rows"] == rows
    assert result["analysis"]["type_decoding"]["basis"] == "RDF_LITERAL_METADATA"


@pytest.mark.skipif(
    not WREN_PYTHON.exists(), reason="make wren-runtime installs official optional engine"
)
def test_official_wren_grouping_nulls_distinct_and_receipt_drilldown(tmp_path):
    client, payload, store, _, _ = setup(tmp_path)
    result = client.post("/ontology/realtime/session-analysis", json=plan(payload))
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["rows"] == [
        {"group_1": "甲", "m_count": 2, "m_objects": 1, "m_sum": 15},
        {"group_1": None, "m_count": 1, "m_objects": 1, "m_sum": None},
    ]
    analysis = data["analysis"]
    assert analysis["strict_mode"] and not analysis["fallback"]
    assert analysis["groups"][0]["source_row_numbers"] == [0, 1]
    assert analysis["detail_rows"][2]["值"] is None
    assert "receipt.main.input_rows" in analysis["expanded_sql"]
    saved = json.loads(store.read(data["evidence_receipt"]))
    assert (
        saved["response"]["answer"]["evidence_bundle"]["evidence"][0]["payload"]["cq_results"][0][
            "analysis"
        ]
        == analysis
    )
    reference = data["evidence_receipt"]
    page = client.post(
        "/ontology/realtime/session-result-page",
        json={
            "session_id": payload["session_id"],
            "expected_release": payload["expected_release"],
            "receipt_id": reference["receipt_id"],
            "receipt_sha256": reference["sha256"],
            "query_id": reference["query_id"],
            "capability_name": "wren_analysis",
        },
    )
    assert page.status_code == 200 and page.json()["rows"] == data["rows"]


@pytest.mark.skipif(not WREN_PYTHON.exists(), reason="requires optional official Wren engine")
def test_date_grouping_empty_input_filters_and_invalid_fields(tmp_path):
    client, payload, _, _, _ = setup(tmp_path)
    request = plan(
        payload,
        dimensions=[{"field": "日期", "period": "month"}],
        metrics=[{"name": "m_rows", "operation": "count_rows"}],
    )
    response = client.post("/ontology/realtime/session-analysis", json=request)
    assert response.status_code == 200, response.text
    assert [row["m_rows"] for row in response.json()["rows"]] == [2, 1]
    request.update(dimensions=[], filters=[{"field": "值", "operator": "gt", "value": 99}])
    assert client.post("/ontology/realtime/session-analysis", json=request).json()["rows"] == [
        {"m_rows": 0}
    ]
    request["filters"][0]["field"] = 'x"; DROP TABLE input_rows;--'
    assert client.post("/ontology/realtime/session-analysis", json=request).status_code == 409
    request["filters"] = [{"field": "值", "operator": "eq", "value": "7"}]
    assert client.post("/ontology/realtime/session-analysis", json=request).status_code == 409


def test_revocation_during_wren_prevents_new_receipt(tmp_path, monkeypatch):
    client, payload, store, _, release = setup(tmp_path)
    before = set(store.root.iterdir())

    def revoke(*args):
        (release / "release-revocation.json").write_text('{"status":"REVOKED"}')
        return {}

    monkeypatch.setattr("services.realtime_qa.wren_api.run_analysis", revoke)
    assert client.post("/ontology/realtime/session-analysis", json=plan(payload)).status_code == 409
    assert set(store.root.iterdir()) == before


def test_mcp_context_uses_real_receipt_and_rejects_extra_parameters(tmp_path, monkeypatch):
    client, payload, _, _, _ = setup(tmp_path)

    def request(path, body):
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", request)
    args = {**payload, **payload["expected_release"]}
    del args["expected_release"]
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "describe_ontology_analysis", "arguments": args},
    }
    result = realtime_qa_mcp.RealtimeQaMcpServer().handle(call)
    assert result["result"]["structuredContent"]["source_row_count"] == 3
    args["database_url"] = "postgres://elsewhere"
    assert "error" in realtime_qa_mcp.RealtimeQaMcpServer().handle(call)


def test_metric_contract_cannot_silently_change_null_semantics():
    with pytest.raises(ValueError):
        AnalysisRequest.model_validate({"metrics": []})
