from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness import realtime_qa_mcp

SESSION_ID = "session-11111111-1111-1111-1111-111111111111"
FINGERPRINT = "sha256:" + "a" * 64


def arguments() -> dict[str, Any]:
    return {
        "session_id": SESSION_ID,
        "project_id": "project-realtime",
        "release_version": "1.0.0",
        "release_fingerprint": FINGERPRINT,
        "question": "这个订单当前是什么情况？",
        "structured_query": {
            "name": "order_live",
            "parameters": {"code": "PO-001"},
        },
        "entity_iris": ["urn:orion:order:1"],
    }


def test_tool_catalog_contains_answer_and_description_readonly_tools() -> None:
    response = realtime_qa_mcp.RealtimeQaMcpServer().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )

    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["answer_realtime_ontology_question", "describe_ontology_capability", "read_ontology_query_results", "describe_ontology_query_space", "execute_checked_ontology_query", "describe_ontology_analysis", "analyze_ontology_results", "describe_ontology_analytics", "query_ontology_analytics", "manage_ontology_analysis_assets", "manage_ontology_analysis_exports"]
    assert all(tool["annotations"]["readOnlyHint"] is (tool["name"] not in {"manage_ontology_analysis_assets", "manage_ontology_analysis_exports"}) for tool in tools)
    assert all(tool["annotations"]["destructiveHint"] is False for tool in tools)


def test_tool_schemas_preserve_ptc_types_in_installed_harness():
    runtime_root = os.environ.get("ORION_DSH_RUNTIME_ROOT")
    if not runtime_root:
        pytest.fail("Set ORION_DSH_RUNTIME_ROOT to the installed DeepSeek Harness 0.2.0-rc.2 runtime; the official schema check is required")
    package = Path(runtime_root).resolve() / "node_modules/@deepseek-ai/dsh-tools/lib/index.js"
    node = os.environ.get("ORION_DSH_NODE") or shutil.which("node")
    if not package.is_file() or not node:
        pytest.fail("Install the official Harness 0.2.0-rc.2 SDK and Node.js; ORION_DSH_RUNTIME_ROOT/ORION_DSH_NODE must identify usable inputs")
    script = """
    const {assertSupportedJsonSchema, jsonSchemaToTs} = await import(process.argv[1]);
    let data = ''; for await (const chunk of process.stdin) data += chunk;
    for (const tool of JSON.parse(data)) {
      assertSupportedJsonSchema(tool.inputSchema);
      const type = jsonSchemaToTs(tool.inputSchema);
      if (type.trim() === 'unknown' || !type.includes('session_id: string')) throw Error(type);
      console.log(tool.name + ': supported');
    }
    """
    result = subprocess.run([node, "--input-type=module", "-e", script, package.as_uri()],
                            input=json.dumps([tool for tool in realtime_qa_mcp.RealtimeQaMcpServer().handle({"id": 1, "method": "tools/list"})["result"]["tools"] if tool["name"] in {"answer_realtime_ontology_question", "describe_ontology_capability", "describe_ontology_analysis", "analyze_ontology_results"}]),
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(": supported") == 4


def description_arguments(**selection) -> dict[str, Any]:
    return {key: arguments()[key] for key in ("session_id", "project_id", "release_version", "release_fingerprint")} | selection


def describe_call(arguments):
    return realtime_qa_mcp.RealtimeQaMcpServer().handle({
        "jsonrpc": "2.0", "id": 90, "method": "tools/call",
        "params": {"name": "describe_ontology_capability", "arguments": arguments},
    })


@pytest.mark.parametrize("selection", [{}, {"kind": "structured", "name": "order_live"}, {"kind": "reasoning"}, {"kind": "document"}])
def test_description_calls_only_bound_metadata_endpoint(monkeypatch, selection):
    captured = {}

    def fake_request(path, payload):
        captured.update(path=path, payload=payload)
        return {"session_id": SESSION_ID, "expected_release": payload["expected_release"],
                "release_match": "verified", "description": {"mode": "detail" if "name" in selection else "catalog"}}

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fake_request)
    response = describe_call(description_arguments(**selection))
    assert "result" in response
    assert captured["path"] == "/ontology/realtime/session-capability"
    assert captured["payload"] == {"session_id": SESSION_ID, "expected_release": {
        "project_id": "project-realtime", "release_version": "1.0.0", "release_fingerprint": FINGERPRINT,
    }, **selection}


@pytest.mark.parametrize("selection", [{"path": "/etc/passwd"}, {"sql": "SELECT 1"}, {"name": "../../mapping"}, {"kind": "execute"}, {"release_fingerprint": "unbound"}, {"session_id": "other"}])
def test_description_rejects_unbound_or_unrestricted_inputs_before_network(monkeypatch, selection):
    monkeypatch.setattr(realtime_qa_mcp, "_request_json", lambda *_: pytest.fail("invalid inputs must not reach API"))
    response = describe_call(description_arguments(**selection))
    assert response["error"]["code"] == -32000


@pytest.mark.parametrize("change", [{"session_id": "session-other"}, {"release_match": "unknown"}, {"expected_release": {}}, {"description": []}])
def test_description_rejects_response_identity_mismatch(monkeypatch, change):
    def fake_request(_path, payload):
        return {"session_id": SESSION_ID, "release_match": "verified", "expected_release": payload["expected_release"], "description": {}, **change}
    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fake_request)
    assert describe_call(description_arguments())["error"]["code"] == -32000


def test_tool_calls_only_the_session_bound_answer_endpoint(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update({"path": path, "payload": payload})
        return {
            "session_id": SESSION_ID,
            "release_match": "verified",
            "runtime_binding": {
                "project_id": "project-realtime",
                "release_version": "1.0.0",
                "release_fingerprint": FINGERPRINT,
                "artifact_verified": True,
                "runtime_verified": True,
                "database_access_mode": "READ_ONLY",
            },
            "answer": {
                "answer_status": "partial",
                "complete": False,
                "answer": "只能给出部分回答。",
                "citations": [
                    {
                        "citation_id": "C1",
                        "source_kind": "ontology_release",
                        "source_ref": "orion:project-realtime@1.0.0",
                    }
                ],
                "warnings": ["Ontop runtime unavailable"],
                "evidence_bundle": {
                    "mode": "hybrid",
                    "generated_at": "2026-09-03T01:00:00+08:00",
                    "release": {
                        "project_id": "project-realtime",
                        "release_version": "1.0.0",
                        "release_fingerprint": FINGERPRINT,
                        "identity_contracts": [
                            {
                                "contract_id": "ER-EMPLOYEE-0001",
                                "canonical_entity": "Employee",
                                "normalization": "STABLE_HASH",
                                "cardinality": "ONE_TO_ONE",
                                "collision_policy": "FAIL",
                            }
                        ],
                    },
                    "snapshot_set_id": "SS-" + "A" * 24,
                    "source_evidence_v2": [
                        {
                            "source_id": "employee_pg",
                            "engine": "POSTGRESQL",
                            "database": "employees",
                            "schema_name": "public",
                            "tables": ["employees"],
                            "dataset_id": "DS-EMPLOYEE-0001",
                            "snapshot_version": "v1",
                            "freshness": "FRESH",
                            "pii_scope": "STABLE_HASHED_IDENTIFIER_ONLY",
                            "masking": "STABLE_HASH",
                            "rule_id": "RULE-001",
                        }
                    ],
                    "source_status": {
                        "structured_db": {
                            "status": "degraded",
                            "source_ref": "ontop:order_live",
                            "queried_at": "2026-09-03T01:00:00+08:00",
                            "version": "1.0.0",
                            "degraded_reason": "Ontop runtime unavailable",
                        },
                        "document": {
                            "status": "fresh",
                            "source_ref": "fuseki:versioned-documents",
                            "queried_at": "2026-09-03T01:00:01+08:00",
                            "version": "3",
                            "degraded_reason": None,
                        },
                    },
                },
            },
        }

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fake_request)
    response = realtime_qa_mcp.RealtimeQaMcpServer().handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "answer_realtime_ontology_question",
                "arguments": arguments(),
            },
        }
    )

    assert response is not None
    assert captured["path"] == "/ontology/realtime/session-answer"
    assert captured["payload"]["expected_release"] == {
        "project_id": "project-realtime",
        "release_version": "1.0.0",
        "release_fingerprint": FINGERPRINT,
    }
    assert captured["payload"]["session_id"] == SESSION_ID
    assert (
        captured["payload"]["answer_request"]["evidence_request"]["project_id"]
        == "project-realtime"
    )
    assert (
        captured["payload"]["answer_request"]["evidence_request"]["structured_query"]["name"]
        == "order_live"
    )
    assert response["result"]["structuredContent"]["release_match"] == "verified"
    text = response["result"]["content"][0]["text"]
    assert "可作为完整结论：`否`" in text
    assert "artifact_verified: `true`" in text
    assert "runtime_verified: `true`" in text
    assert "structured_db: status=degraded" in text
    assert "document: status=fresh" in text
    assert "queried_at=2026-09-03T01:00:01+08:00" in text
    assert "Ontop runtime unavailable" in text
    assert "snapshot_set_id: `SS-AAAAAAAAAAAAAAAAAAAAAAAA`" in text
    assert "employee_pg [POSTGRESQL]" in text
    assert "ER-EMPLOYEE-0001" in text
    assert "未执行实时补查（快照主路径）" in text


def test_tool_rejects_unbound_or_malformed_requests_before_network(
    monkeypatch,
) -> None:
    called = False

    def fail_if_called(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fail_if_called)
    payload = arguments()
    payload["release_fingerprint"] = "dashboard-summary"
    response = realtime_qa_mcp.RealtimeQaMcpServer().handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "answer_realtime_ontology_question",
                "arguments": payload,
            },
        }
    )

    assert response is not None
    assert response["error"]["code"] == -32000
    assert "package manifest SHA-256" in response["error"]["message"]
    assert called is False


def test_tool_forwards_typed_parameters_and_bounded_document_search(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update({"path": path, "payload": payload})
        return {
            "release_match": "verified",
            "runtime_binding": {"artifact_verified": True, "runtime_verified": True},
            "answer": {
                "answer_status": "no_evidence",
                "complete": False,
                "answer": "没有证据。",
                "citations": [],
                "warnings": [],
                "evidence_bundle": {
                    "release": {
                        "project_id": "project-realtime",
                        "release_version": "1.0.0",
                        "release_fingerprint": FINGERPRINT,
                    },
                    "source_status": {},
                },
            },
        }

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fake_request)
    payload = arguments()
    payload["structured_query"]["parameters"] = {
        "filter_param_value": 1,
        "operator_param_value": "GT",
        "include_ng": False,
    }
    payload["document_query"] = {
        "text": "电压测试VD1",
        "document_ids": ["DOC-001"],
        "limit": 5,
    }

    response = realtime_qa_mcp.RealtimeQaMcpServer().handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "answer_realtime_ontology_question",
                "arguments": payload,
            },
        }
    )

    assert response is not None and "result" in response
    evidence_request = captured["payload"]["answer_request"]["evidence_request"]
    assert evidence_request["execution_mode"] == "SNAPSHOT_ONLY"
    assert evidence_request["structured_query"]["parameters"]["filter_param_value"] == 1
    assert evidence_request["structured_query"]["parameters"]["include_ng"] is False
    assert evidence_request["document_query"] == {
        "text": "电压测试VD1",
        "document_ids": ["DOC-001"],
        "limit": 5,
    }


def test_tool_forwards_a_release_bound_reasoning_query(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update({"path": path, "payload": payload})
        return {
            "release_match": "verified",
            "runtime_binding": {
                "artifact_verified": True,
                "runtime_verified": True,
                "reasoning_runtime_verified": True,
            },
            "answer": {
                "answer_status": "complete",
                "complete": True,
                "answer": "已推出需集中性分析批次。",
                "citations": [],
                "warnings": [],
                "evidence_bundle": {
                    "mode": "reasoning",
                    "release": {
                        "project_id": "project-realtime",
                        "release_version": "1.0.0",
                        "release_fingerprint": FINGERPRINT,
                    },
                    "source_status": {
                        "reasoning": {
                            "status": "fresh",
                            "source_ref": "semantica:concentration_analysis",
                            "version": "1.0.0",
                        }
                    },
                },
            },
        }

    monkeypatch.setattr(realtime_qa_mcp, "_request_json", fake_request)
    payload = arguments()
    payload.pop("structured_query")
    payload.pop("entity_iris")
    payload["reasoning_query"] = {
        "name": "concentration_analysis",
        "parameters": {"threshold": 0.002},
    }

    response = realtime_qa_mcp.RealtimeQaMcpServer().handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "answer_realtime_ontology_question",
                "arguments": payload,
            },
        }
    )

    assert response is not None and "result" in response
    reasoning_query = captured["payload"]["answer_request"]["evidence_request"]["reasoning_query"]
    assert reasoning_query == {
        "name": "concentration_analysis",
        "parameters": {"threshold": 0.002},
    }
    assert "reasoning: status=fresh" in response["result"]["content"][0]["text"]
