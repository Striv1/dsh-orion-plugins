from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from harness import realtime_qa_mcp
from services.realtime_qa.api import create_core_realtime_app
from services.realtime_qa.evidence_receipts import EvidenceReceiptError, EvidenceReceiptStore
from services.realtime_qa.incremental_pipeline import DocumentCurrentRegistry
from services.realtime_qa.models import (
    RealtimeEvidenceRequest,
    RealtimeSessionAnswer,
    ReasoningQueryRequest,
)
from services.realtime_qa.service import HybridRealtimeEvidenceService
from tests.unit.test_core_realtime_api import _Provider
from tests.unit.test_realtime_qa import NOW, FakeFuseki, FakeOntop, FakeSemantica, reasoning_binding

SESSION = "session-11111111-1111-1111-1111-111111111111"


def request():
    return {
        "session_id": SESSION,
        "expected_release": {"project_id": "core-api-project", "release_version": "1.0.0", "release_fingerprint": "sha256:" + "1" * 64},
        "answer_request": {"question": "本次规则的依据是什么？", "evidence_request": {"project_id": "core-api-project", "query_id": "Q-RECEIPT", "entity_iris": ["urn:orion:record:1"]}},
    }


def stored_answer(tmp_path):
    store = EvidenceReceiptStore(tmp_path / "receipts")
    provider = _Provider()
    app = create_core_realtime_app(provider, evidence_receipts=store)
    client = TestClient(app)
    result = client.post("/ontology/realtime/session-answer", json=request())
    assert result.status_code == 200, result.text
    return store, client, result.json()


def test_real_session_answer_route_persists_before_issuing_reference(tmp_path):
    store, client, answer = stored_answer(tmp_path)
    reference = answer["evidence_receipt"]
    assert reference["stored_full_response"] is True
    content = store.read(reference)
    assert reference["sha256"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert reference["byte_count"] == len(content)
    envelope = json.loads(content)
    assert envelope["response"]["answer"] == answer["answer"]
    assert envelope["query_id"] == "Q-RECEIPT"
    assert not list(store.root.glob("*.tmp"))
    assert (store.root / (reference["receipt_id"] + ".json")).stat().st_mode & 0o777 == 0o600
    read = client.get(f"/ontology/realtime/evidence-receipts/{reference['receipt_id']}", params=reference)
    assert read.status_code == 200
    assert read.content == content
    assert read.headers["cache-control"] == "no-store"
    repeated = client.post("/ontology/realtime/session-answer", json=request()).json()
    assert repeated["evidence_receipt"]["receipt_id"] != reference["receipt_id"]
    assert store.read(reference) == content


@pytest.mark.parametrize("field", ["session_id", "query_id", "project_id", "release_version", "release_fingerprint", "sha256"])
def test_receipt_read_rejects_identity_or_integrity_mismatch(tmp_path, field):
    store, client, answer = stored_answer(tmp_path)
    reference = {**answer["evidence_receipt"]}
    reference[field] = "sha256:" + "f" * 64 if field in {"sha256", "release_fingerprint"} else SESSION.replace("1", "2") if field == "session_id" else "other"
    with pytest.raises(EvidenceReceiptError):
        store.read(reference)
    assert client.get(f"/ontology/realtime/evidence-receipts/{reference['receipt_id']}", params=reference).status_code == 404


def test_tampered_file_symlink_and_arbitrary_paths_are_rejected(tmp_path):
    store, _, answer = stored_answer(tmp_path)
    reference = answer["evidence_receipt"]
    path = store.root / (reference["receipt_id"] + ".json")
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(EvidenceReceiptError, match="integrity"):
        store.read(reference)
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(EvidenceReceiptError):
        store.read(reference)
    with pytest.raises(EvidenceReceiptError):
        store.read({**reference, "receipt_id": "../outside"})


def test_storage_failure_does_not_issue_false_audit_reference(tmp_path, monkeypatch):
    store = EvidenceReceiptStore(tmp_path / "receipts")
    monkeypatch.setattr(store, "save", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    client = TestClient(create_core_realtime_app(_Provider(), evidence_receipts=store))
    result = client.post("/ontology/realtime/session-answer", json=request())
    assert result.status_code == 503
    assert "未签发" in result.json()["detail"]
    assert not store.root.exists()


def test_executed_input_facts_and_trace_are_preserved_in_receipt(tmp_path):
    engine = FakeSemantica()
    service = HybridRealtimeEvidenceService(reasoning_binding(), FakeOntop(), FakeFuseki(), DocumentCurrentRegistry(tmp_path / "current"), semantica=engine, now=lambda: NOW)
    bundle = service.collect(RealtimeEvidenceRequest(query_id="Q-FULL", reasoning_query=ReasoningQueryRequest(name="order_expediting", parameters={"purchase_order_code": "PO-202608-017"})))
    record = next(item.payload for item in bundle.evidence if item.source_kind == "reasoning")
    assert record["input_facts"] == engine.calls[0]["facts"]
    assert record["rules"][0]["expression"] == engine.calls[0]["rules"][0]
    assert record["trace"][0]["premises"] == record["input_facts"]
    store, _, answer = stored_answer(tmp_path)
    response = RealtimeSessionAnswer.model_validate(answer)
    response.answer.evidence_bundle = bundle
    response.answer.evidence_bundle.query_id = "Q-FULL"
    reference = store.save(response)
    saved = json.loads(store.read(reference))
    persisted = next(item["payload"] for item in saved["response"]["answer"]["evidence_bundle"]["evidence"] if item["source_kind"] == "reasoning")
    assert persisted["input_facts"] == engine.calls[0]["facts"]
    assert persisted["trace"] == record["trace"]


def test_mcp_real_handler_emits_durable_markdown_reference_without_raw_big_bundle(tmp_path, monkeypatch):
    _, client, _ = stored_answer(tmp_path)
    monkeypatch.setattr(realtime_qa_mcp, "_request_json", lambda path, payload: client.post(path, json=payload).json())
    args = {"session_id": SESSION, **request()["expected_release"], "query_id": "Q-MCP", "question": "本次规则的依据是什么？", "entity_iris": ["urn:orion:record:1"]}
    result = realtime_qa_mcp.RealtimeQaMcpServer().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "answer_realtime_ontology_question", "arguments": args}})["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    marker = next(line for line in text.splitlines() if line.startswith("ORION_EVIDENCE_RECEIPT_V1 "))
    reference = json.loads(marker.removeprefix("ORION_EVIDENCE_RECEIPT_V1 "))["evidence_receipt"]
    assert reference["query_id"] == "Q-MCP"
    assert client.get(f"/ontology/realtime/evidence-receipts/{reference['receipt_id']}", params=reference).status_code == 200
    projection = result["structuredContent"]
    assert "evidence" not in projection["answer"]["evidence_bundle"]
    assert projection["projection"] == "BOUNDED_SUMMARY_FULL_EVIDENCE_IN_RECEIPT"


def test_mcp_summary_bounds_large_results_and_rejects_wrong_receipt_identity(tmp_path):
    store, _, response = stored_answer(tmp_path)
    response["answer"]["evidence_bundle"]["evidence"].append({
        **response["answer"]["evidence_bundle"]["evidence"][0],
        "evidence_id": "EVD-BIG", "source_kind": "reasoning",
        "payload": {"input_facts": [f"Fact({i})" for i in range(149000)]},
    })
    response["evidence_receipt"] = store.save(RealtimeSessionAnswer.model_validate(response))
    complete = json.loads(store.read(response["evidence_receipt"]))
    assert complete["response"]["answer"]["evidence_bundle"]["evidence"][-1]["payload"]["input_facts"][-1] == "Fact(148999)"
    # The model projection never copies raw evidence arrays, regardless of size.
    projection = realtime_qa_mcp._safe_answer_projection(response, request())
    assert len(json.dumps(projection)) < 20000
    assert "Fact(148999)" not in json.dumps(projection)
    response["evidence_receipt"]["query_id"] = "other"
    with pytest.raises(realtime_qa_mcp.RealtimeQaToolError, match="不一致"):
        realtime_qa_mcp._safe_answer_projection(response, request())


def test_document_cq_receipt_keeps_all_rows_beyond_answer_preview(tmp_path):
    store, _, original = stored_answer(tmp_path)
    response = RealtimeSessionAnswer.model_validate(original)
    rows = [{'code': f'C{i:04d}', 'months': 12+i} for i in range(58)]
    response.answer.evidence_bundle.evidence[-1].payload = {'cq_results': [{
        'rows': rows, 'row_count': 58, 'query_sha256': 'sha256:'+'a'*64,
        'fact_sha256': 'sha256:'+'b'*64, 'source_refs': ['EV-1'],
    }]}
    reference = store.save(response)
    restored = json.loads(store.read(reference))['response']['answer']['evidence_bundle']
    assert restored['evidence'][-1]['payload']['cq_results'][0]['rows'] == rows
    assert reference['stored_full_response'] is True
