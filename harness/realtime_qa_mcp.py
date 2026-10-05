from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

API_URL = os.getenv("ONTOLOGY_AGENT_API_URL", "http://127.0.0.1:8091").rstrip("/")
PROTOCOL_VERSION = "2025-06-18"
SESSION_ID = re.compile(r"^session-[a-f0-9-]{36}$")
FINGERPRINT = re.compile(r"^sha256:[a-f0-9]{64}$")
ENTITY_IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")


class RealtimeQaToolError(RuntimeError):
    """A fail-closed error from the release-bound realtime Q&A adapter."""


def _request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{API_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=65) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RealtimeQaToolError(f"实时问答 API 返回 {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RealtimeQaToolError(
            f"无法连接发布绑定的实时问答 API {API_URL}: {exc.reason}"
        ) from exc
    if not isinstance(value, dict):
        raise RealtimeQaToolError("实时问答 API 返回了无效响应")
    return value


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise RealtimeQaToolError(f"{name} 不能为空")
    return value


def _validated_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = _required_text(arguments, "session_id")
    project_id = _required_text(arguments, "project_id")
    release_version = _required_text(arguments, "release_version")
    release_fingerprint = _required_text(arguments, "release_fingerprint")
    question = _required_text(arguments, "question")
    execution_mode = str(arguments.get("execution_mode") or "SNAPSHOT_ONLY")
    if execution_mode not in {"SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID"}:
        raise RealtimeQaToolError("execution_mode 格式无效")
    if not SESSION_ID.fullmatch(session_id):
        raise RealtimeQaToolError("session_id 格式无效")
    if not FINGERPRINT.fullmatch(release_fingerprint):
        raise RealtimeQaToolError("release_fingerprint 必须是 package manifest SHA-256")
    if len(question) > 1000:
        raise RealtimeQaToolError("question 不能超过 1000 个字符")

    entity_iris = arguments.get("entity_iris") or []
    if not isinstance(entity_iris, list) or len(entity_iris) > 20:
        raise RealtimeQaToolError("entity_iris 必须是最多 20 项的数组")
    if any(not ENTITY_IRI.fullmatch(str(value)) for value in entity_iris):
        raise RealtimeQaToolError("entity_iris 包含无效 IRI")

    structured_query = arguments.get("structured_query")
    if structured_query is not None:
        if not isinstance(structured_query, dict):
            raise RealtimeQaToolError("structured_query 必须是对象")
        name = str(structured_query.get("name") or "")
        parameters = structured_query.get("parameters") or {}
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            raise RealtimeQaToolError("structured_query.name 格式无效")
        if (
            not isinstance(parameters, dict)
            or len(parameters) > 32
            or any(
                not isinstance(key, str)
                or isinstance(value, dict | list)
                or value is None
                or not isinstance(value, str | int | float | bool)
                for key, value in parameters.items()
            )
        ):
            raise RealtimeQaToolError(
                "structured_query.parameters 最多包含32个字符串、数字或布尔参数"
            )
        structured_query = {"name": name, "parameters": parameters}
    reasoning_query = arguments.get("reasoning_query")
    if reasoning_query is not None:
        if not isinstance(reasoning_query, dict):
            raise RealtimeQaToolError("reasoning_query 必须是对象")
        name = str(reasoning_query.get("name") or "")
        parameters = reasoning_query.get("parameters") or {}
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            raise RealtimeQaToolError("reasoning_query.name 格式无效")
        if (
            not isinstance(parameters, dict)
            or len(parameters) > 32
            or any(
                not isinstance(key, str)
                or isinstance(value, dict | list)
                or value is None
                or not isinstance(value, str | int | float | bool)
                for key, value in parameters.items()
            )
        ):
            raise RealtimeQaToolError(
                "reasoning_query.parameters 最多包含32个字符串、数字或布尔参数"
            )
        reasoning_query = {"name": name, "parameters": parameters}
    document_fact_query = arguments.get("document_fact_query")
    if document_fact_query is not None:
        if not isinstance(document_fact_query, dict):
            raise RealtimeQaToolError("document_fact_query 必须是对象")
        name = str(document_fact_query.get("name") or "")
        parameters = document_fact_query.get("parameters") or {}
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            raise RealtimeQaToolError("document_fact_query.name 格式无效")
        if (
            not isinstance(parameters, dict)
            or len(parameters) > 32
            or any(
                not isinstance(key, str)
                or isinstance(value, dict | list)
                or value is None
                or not isinstance(value, str | int | float | bool)
                for key, value in parameters.items()
            )
        ):
            raise RealtimeQaToolError(
                "document_fact_query.parameters 最多包含32个字符串、数字或布尔参数"
            )
        document_fact_query = {"name": name, "parameters": parameters}
    document_query = arguments.get("document_query")
    if document_query is not None:
        if not isinstance(document_query, dict):
            raise RealtimeQaToolError("document_query 必须是对象")
        text = str(document_query.get("text") or "").strip()
        document_ids = document_query.get("document_ids") or []
        limit = document_query.get("limit", 10)
        if len(text) < 2 or len(text) > 200:
            raise RealtimeQaToolError("document_query.text 必须包含2到200个字符")
        if (
            not isinstance(document_ids, list)
            or len(document_ids) > 20
            or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,159}", str(value))
                for value in document_ids
            )
        ):
            raise RealtimeQaToolError("document_query.document_ids 格式无效")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
            raise RealtimeQaToolError("document_query.limit 必须是1到50的整数")
        document_query = {
            "text": text,
            "document_ids": [str(value) for value in document_ids],
            "limit": limit,
        }
    if (
        structured_query is None
        and reasoning_query is None
        and document_fact_query is None
        and document_query is None
        and not entity_iris
    ):
        raise RealtimeQaToolError(
            "至少需要 structured_query、reasoning_query、document_fact_query、document_query 或一个 entity_iri"
        )

    query_id = str(arguments.get("query_id") or f"Q-HARNESS-{uuid4().hex[:16]}")
    return {
        "session_id": session_id,
        "expected_release": {
            "project_id": project_id,
            "release_version": release_version,
            "release_fingerprint": release_fingerprint,
        },
        "answer_request": {
            "question": question,
            "evidence_request": {
                "project_id": project_id,
                "query_id": query_id,
                **({"structured_query": structured_query} if structured_query is not None else {}),
                **({"reasoning_query": reasoning_query} if reasoning_query is not None else {}),
                **({"document_fact_query": document_fact_query} if document_fact_query is not None else {}),
                **({"document_query": document_query} if document_query is not None else {}),
                "entity_iris": [str(value) for value in entity_iris],
                "execution_mode": execution_mode,
            },
        },
    }


RESULT_SUMMARY_LIMITS = {"results": 4, "columns": 24, "categories": 12, "value_length": 96}


def _executed_result_summary(bundle: dict[str, Any], receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Describe complete returned row arrays, never the answer renderer's preview."""
    records = [item for item in (bundle.get("evidence") or [])
               if isinstance(item, dict) and item.get("source_kind") == "structured_db"]
    for item in bundle.get("evidence") or []:
        if not isinstance(item, dict) or item.get("source_kind") not in {"document", "reasoning"}:
            continue
        for cq in (item.get("payload") or {}).get("cq_results") or []:
            if isinstance(cq, dict) and isinstance(cq.get("rows"), list):
                records.append({"evidence_id": item.get("evidence_id"), "source_ref": item.get("source_ref"),
                    "payload": {"rows": cq["rows"], "row_count": cq.get("row_count"),
                                "query_template": cq.get("capability_name"),
                                "cq_provenance": {key: cq[key] for key in ("source_question_id", "query_sha256", "fact_sha256", "ontology_sha256", "release_fingerprint", "source_refs") if key in cq}}})
    summary = {
        "scope": "EXECUTED_RETURNED_ROWS_ONLY",
        "scope_note": "仅统计本次查询已返回行（包括查询本身的筛选、LIMIT或聚合），不等于来源全量；不推断业务含义。",
        "distinct_scope": "非 null 的类型化值；missing 表示行内没有该字段，null 单独计数；布尔、数值、字符串不混同。",
        "bundle_complete": bundle.get("complete") is True,
        "receipt_id": receipt.get("receipt_id") if receipt else None,
        "receipt_sha256": receipt.get("sha256") if receipt else None,
        "stored_full_response": receipt.get("stored_full_response") is True if receipt else False,
        "limits": dict(RESULT_SUMMARY_LIMITS),
        "omitted_result_count": max(0, len(records) - RESULT_SUMMARY_LIMITS["results"]),
        "results": [],
    }
    for record in records[:RESULT_SUMMARY_LIMITS["results"]]:
        data = record.get("payload") or {}
        if not isinstance(data, dict):
            data = {}
        rows = data.get("rows")
        result = {
            key: str(value)[:256] if value is not None else None
            for key, value in {
                "evidence_id": record.get("evidence_id"), "source_ref": record.get("source_ref"),
                "query_template": data.get("query_template"),
                "evidence_role": data.get("evidence_role"),
            }.items()
        }
        if isinstance(data.get("cq_provenance"), dict):
            result["cq_provenance"] = data["cq_provenance"]
        result.update(row_count=len(rows) if isinstance(rows, list) else None,
                      reported_row_count=data.get("row_count") if type(data.get("row_count")) is int else None,
                      status="UNAVAILABLE_ROWS", columns=[])
        summary["results"].append(result)
        if isinstance(rows, list):
            result["rows_preview"] = [{key: (value[:300] + "…" if isinstance(value, str) and len(value) > 300 else value)
                for key, value in list(row.items())[:24] if value is None or isinstance(value, str | int | float | bool)}
                for row in rows[:10] if isinstance(row, dict)]
            result["preview_omitted_rows"] = max(0, len(rows) - 10)
            result["preview_scope"] = "FIRST_10_ROWS_UP_TO_24_SCALAR_COLUMNS_VALUES_UP_TO_300_CHARS; full rows via read_ontology_query_results"
        if not isinstance(rows, list):
            continue
        if any(not isinstance(row, dict) or any(not isinstance(key, str) for key in row) for row in rows):
            result["status"] = "UNAVAILABLE_INVALID_ROWS"
            continue
        result["status"] = ("RETURNED_ROWS_COUNT_MATCHES" if result["reported_row_count"] == len(rows)
                            else "REPORTED_ROW_COUNT_MISSING" if result["reported_row_count"] is None
                            else "REPORTED_ROW_COUNT_MISMATCH")
        names = sorted({key for row in rows for key in row})
        selected = [name for name in names if len(name) <= RESULT_SUMMARY_LIMITS["value_length"]][:RESULT_SUMMARY_LIMITS["columns"]]
        result.update(column_count=len(names), omitted_column_count=len(names) - len(selected))
        for name in selected:
            missing = nulls = unsupported = 0
            counts = {}
            distribution_reason = "COMPLETE"
            for row in rows:
                if name not in row:
                    missing += 1
                    continue
                value = row[name]
                if value is None:
                    nulls += 1
                    continue
                # bool is an int subclass in Python; tag it before numeric handling.
                if isinstance(value, bool):
                    kind, key = "boolean", value
                elif isinstance(value, int | float) and (not isinstance(value, float) or math.isfinite(value)):
                    kind, key = "number", value
                elif isinstance(value, str):
                    kind, key = "string", value
                elif isinstance(value, dict | list):
                    try:
                        kind, key = "json", json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
                    except (ValueError, TypeError):
                        unsupported += 1
                        continue
                    distribution_reason = "OMITTED_NON_SCALAR_VALUES"
                else:
                    unsupported += 1
                    continue
                typed_key = (kind, key)
                if typed_key not in counts:
                    counts[typed_key] = {"type": kind, "value": value, "count": 0}
                counts[typed_key]["count"] += 1
                if len(json.dumps(value, ensure_ascii=False)) > RESULT_SUMMARY_LIMITS["value_length"] and distribution_reason == "COMPLETE":
                    distribution_reason = "OMITTED_LONG_VALUES"
            if unsupported:
                distribution_reason = "OMITTED_UNSUPPORTED_VALUES"
            elif len(counts) > RESULT_SUMMARY_LIMITS["categories"]:
                distribution_reason = "OMITTED_HIGH_CARDINALITY"
            column = {"field": name, "missing_count": missing, "null_count": nulls,
                      "distinct_count": len(counts) if not unsupported else None,
                      "value_counts_status": distribution_reason}
            if unsupported:
                column["unsupported_value_count"] = unsupported
            if distribution_reason == "COMPLETE":
                column["value_counts"] = sorted(counts.values(), key=lambda item: (item["type"], json.dumps(item["value"], ensure_ascii=False)))
            result["columns"].append(column)
    return summary


def _compact_answer(payload: dict[str, Any]) -> str:
    answer = payload.get("answer") or {}
    bundle = answer.get("evidence_bundle") or {}
    release = bundle.get("release") or {}
    runtime = payload.get("runtime_binding") or {}
    status = str(answer.get("answer_status") or "unknown")
    complete = answer.get("complete") is True
    warnings = answer.get("warnings") or []
    citations = answer.get("citations") or []
    citation_lines = [
        f"- [{item.get('citation_id', '?')}] {item.get('source_kind', 'unknown')} · "
        f"{item.get('source_ref', 'unknown')} · "
        f"locator={json.dumps(item.get('locator') or {}, ensure_ascii=False, separators=(',', ':'))}"
        for item in citations
        if isinstance(item, dict)
    ]
    source_status = bundle.get("source_status") or {}
    source_lines = []
    for key in ("structured_db", "document", "reasoning", "document_facts"):
        item = source_status.get(key) or {}
        source_lines.append(
            f"- {key}: status={item.get('status', 'unknown')}; "
            f"source_ref={item.get('source_ref', 'unknown')}; "
            f"queried_at={item.get('queried_at') or 'not_queried'}; "
            f"version={item.get('version') or 'none'}; "
            f"degraded_reason={item.get('degraded_reason') or 'none'}"
        )
    warning_lines = [f"- {value}" for value in warnings]
    citation_text = "\n".join(citation_lines) or "- 无"
    source_text = "\n".join(source_lines)
    snapshot_set_id = bundle.get("snapshot_set_id") or "none"
    source_v2_lines = []
    for item in bundle.get("source_evidence_v2") or []:
        if not isinstance(item, dict):
            continue
        location = ".".join(
            value
            for value in (
                str(item.get("database") or ""),
                str(item.get("schema_name") or ""),
            )
            if value
        )
        source_v2_lines.append(
            f"- {item.get('source_id', 'unknown')} [{item.get('engine', 'unknown')}]: "
            f"location={location or 'unknown'}; "
            f"tables={','.join(item.get('tables') or []) or 'none'}; "
            f"dataset={item.get('dataset_id') or 'none'}@{item.get('snapshot_version') or 'none'}; "
            f"freshness={item.get('freshness') or 'UNKNOWN'}; "
            f"PII={item.get('pii_scope') or 'UNDECLARED'}/{item.get('masking') or 'UNDECLARED'}; "
            f"rule={item.get('rule_id') or 'none'}"
        )
    source_v2_text = "\n".join(source_v2_lines) or "- 当前发布不是多源快照，或未请求多源查询"
    identity_lines = []
    for item in release.get("identity_contracts") or []:
        if not isinstance(item, dict):
            continue
        identity_lines.append(
            f"- {item.get('contract_id', 'unknown')}: "
            f"canonical={item.get('canonical_entity', 'unknown')}; "
            f"normalization={item.get('normalization', 'unknown')}; "
            f"cardinality={item.get('cardinality', 'unknown')}; "
            f"collision={item.get('collision_policy', 'unknown')}"
        )
    identity_text = "\n".join(identity_lines) or "- 无跨源身份契约"
    realtime_lines = [
        f"- {item.get('template_id', 'unknown')}: status={item.get('status', 'unknown')}; "
        f"observed_at={item.get('observed_at') or 'unknown'}"
        for item in bundle.get("realtime_query_receipts") or []
        if isinstance(item, dict)
    ]
    realtime_text = "\n".join(realtime_lines) or "- 未执行实时补查（快照主路径）"
    result_summary = bundle.get("executed_result_summary")
    result_summary_text = ""
    if isinstance(result_summary, dict) and result_summary.get("results"):
        # Escape fence characters in data, preserving a single JSON evidence block.
        rendered = json.dumps(result_summary, ensure_ascii=False, separators=(",", ":")).replace("`", "\\u0060")
        result_summary_text = "### 本次结构化查询完整返回行的确定性统计\n\n" + "```json\n" + rendered + "\n```\n\n"
    artifact_verified = runtime.get("artifact_verified")
    if artifact_verified is None:
        artifact_verified = release.get("integrity_status") == "verified"
    return (
        "## 发布绑定的实时本体回答\n\n"
        f"- 完整性：`{status}`\n"
        f"- 可作为完整结论：`{'是' if complete else '否'}`\n\n"
        "### 发布与运行时绑定\n\n"
        f"- release: `{release.get('project_id', 'unknown')}@{release.get('release_version', 'unknown')}`\n"
        f"- release_fingerprint: `{release.get('release_fingerprint', 'unknown')}`\n"
        f"- artifact_verified: `{'true' if artifact_verified is True else 'false'}`\n"
        f"- runtime_verified: `{'true' if runtime.get('runtime_verified') is True else 'false'}`\n"
        f"- document_runtime_verified: `{'true' if runtime.get('document_runtime_verified') is True else 'false'}`\n"
        f"- reasoning_runtime_verified: `{'true' if runtime.get('reasoning_runtime_verified') is True else 'false'}`\n"
        f"- current_document_count: `{runtime.get('current_document_count', 0)}`\n"
        f"- database_access_mode: `{runtime.get('database_access_mode', release.get('database_access_mode', 'unknown'))}`\n"
        f"- evidence_mode: `{bundle.get('mode', 'unknown')}`\n"
        f"- snapshot_set_id: `{snapshot_set_id}`\n"
        f"- evidence_generated_at: `{bundle.get('generated_at', 'unknown')}`\n\n"
        "### EvidenceBundle 数据源状态\n\n"
        f"{source_text}\n\n"
        "### 多源拓扑与快照证据\n\n"
        f"{source_v2_text}\n\n"
        "### 统一语义与身份契约\n\n"
        f"{identity_text}\n\n"
        "### 实时补查回执\n\n"
        f"{realtime_text}\n\n"
        f"{result_summary_text}"
        f"{answer.get('answer') or '没有生成回答。'}\n\n"
        "### 可定位引用\n\n"
        f"{citation_text}"
        + ("\n\n### 降级说明\n\n" + "\n".join(warning_lines) if warning_lines else "")
    )


def _safe_answer_projection(structured: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Keep raw evidence in its immutable receipt, outside the model projection."""
    source_answer = structured.get("answer") or {}
    source_bundle = source_answer.get("evidence_bundle") or {}
    release = source_bundle.get("release") or {}
    expectation = request["expected_release"]
    query_id = request["answer_request"]["evidence_request"]["query_id"]
    receipt = structured.get("evidence_receipt")
    if receipt is not None:
        expected = {**expectation, "session_id": request["session_id"], "query_id": query_id}
        if (not isinstance(receipt, dict)
                or receipt.get("schema_version") != "orion-evidence-receipt-v1"
                or not re.fullmatch(r"EVD-[a-f0-9]{32}", str(receipt.get("receipt_id") or ""))
                or not FINGERPRINT.fullmatch(str(receipt.get("sha256") or ""))
                or any(receipt.get(key) != value for key, value in expected.items())
                or structured.get("session_id") != request["session_id"]
                or source_bundle.get("query_id") != query_id
                or any(release.get(key) != value for key, value in expectation.items())):
            raise RealtimeQaToolError("证据回执与本次查询、会话或发布版本不一致")
        receipt = {key: receipt.get(key) for key in (
            "schema_version", "receipt_id", "sha256", "byte_count", "session_id", "query_id",
            "project_id", "release_version", "release_fingerprint", "created_at", "stored_full_response",
        )}

    def bounded(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value if len(value) <= 1000 else value[:1000] + "…（摘要截断，完整内容见回执）"
        if depth > 4:
            return "（摘要省略，完整内容见回执）"
        if isinstance(value, list):
            items = [bounded(item, depth + 1) for item in value[:20]]
            if len(value) > 20:
                items.append(f"（摘要省略 {len(value) - 20} 项，完整内容见回执）")
            return items
        if isinstance(value, dict):
            result = {key: bounded(item, depth + 1) for key, item in list(value.items())[:30]}
            if len(value) > 30:
                result["__summary_omitted_fields__"] = len(value) - 30
            return result
        return value

    safe_runtime = {key: structured.get("runtime_binding", {}).get(key) for key in (
        "project_id", "release_version", "release_fingerprint", "artifact_verified", "runtime_verified",
        "document_runtime_verified", "reasoning_runtime_verified", "current_document_count", "database_access_mode",
    )}
    safe_release = {key: release.get(key) for key in (
        "project_id", "release_version", "release_fingerprint", "integrity_status", "database_access_mode",
    )}
    safe_release["identity_contracts"] = bounded(release.get("identity_contracts") or [])
    trace = release.get("source_trace") or {}
    safe_release["source_trace"] = bounded({key: trace.get(key) for key in (
        "scope", "snapshot_set_id", "artifacts",
    )})
    sources = [{key: item.get(key) for key in (
        "source_id", "engine", "database", "schema_name", "tables", "columns", "dataset_id", "dataset_ids", "snapshot_version",
        "freshness", "pii_scope", "masking", "rule_id", "observed_at", "queried_at", "query_mode", "provenance_scope",
    )} for item in (source_bundle.get("source_evidence_v2") or [])[:20] if isinstance(item, dict)]
    return {
        "session_id": structured.get("session_id"), "release_match": structured.get("release_match"),
        "runtime_binding": safe_runtime, "evidence_receipt": receipt,
        "projection": "BOUNDED_SUMMARY_FULL_EVIDENCE_IN_RECEIPT",
        "answer": {
            "answer_status": source_answer.get("answer_status"), "complete": source_answer.get("complete") is True,
            "answer": str(source_answer.get("answer") or "")[:8000],
            "answer_text_truncated": len(str(source_answer.get("answer") or "")) > 8000,
            "citations": bounded(source_answer.get("citations") or []),
            "warnings": bounded(source_answer.get("warnings") or []),
            "evidence_bundle": {
                "query_id": source_bundle.get("query_id"), "release": safe_release,
                "mode": source_bundle.get("mode"), "generated_at": source_bundle.get("generated_at"),
                "snapshot_set_id": source_bundle.get("snapshot_set_id"), "source_evidence_v2": bounded(sources),
                "query_modes": bounded(source_bundle.get("query_modes") or {}),
                "reasoning_conclusion_contracts": [
                    {"source_ref": item.get("source_ref"), "contract": bounded(item["payload"]["conclusion_contract"])}
                    for item in (source_bundle.get("evidence") or [])[:20]
                    if isinstance(item, dict) and item.get("source_kind") == "reasoning"
                    and isinstance(item.get("payload"), dict)
                    and isinstance(item["payload"].get("conclusion_contract"), dict)
                ],
                "source_status": bounded(source_bundle.get("source_status") or {}),
                "degraded_sources": bounded(source_bundle.get("degraded_sources") or []),
                "realtime_query_receipts": bounded(source_bundle.get("realtime_query_receipts") or []),
                "executed_result_summary": _executed_result_summary(source_bundle, receipt),
            },
        },
    }


TOOL = {
    "name": "answer_realtime_ontology_question",
    "description": (
        "只读查询当前会话锁定的 S7 发布版本：按发布白名单调用 Ontop，读取 "
        "Fuseki current 文档证据，或执行发布绑定的 Semantica 规则推理，并返回"
        "统一 EvidenceBundle、完整性状态和定位引用。"
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "description": "当前绑定会话的 session UUID"},
            "project_id": {"type": "string"},
            "release_version": {"type": "string"},
            "release_fingerprint": {
                "type": "string",
                "description": "已绑定 manifest 指纹，格式 sha256: 后接 64 位十六进制值",
            },
            "question": {"type": "string", "description": "2 到 1000 个字符"},
            "query_id": {"type": "string", "description": "1 到 128 个字符"},
            "execution_mode": {
                "type": "string",
                "enum": ["SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID"],
                "default": "SNAPSHOT_ONLY",
            },
            "structured_query": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "发布目录声明的能力名称",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "最多 32 个字符串、数字或布尔参数；由 Python 运行时校验",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
            },
            "reasoning_query": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "发布目录声明的能力名称",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "最多 32 个字符串、数字或布尔参数；由 Python 运行时校验",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
            },
            "document_fact_query": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "发布目录声明的能力名称",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "最多 32 个字符串、数字或布尔参数；由 Python 运行时校验",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
            },
            "entity_iris": {
                "type": "array",
                "description": "最多 20 个绝对 HTTP(S) 或 URN IRI",
                "items": {"type": "string"},
            },
            "document_query": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "description": "2 到 200 个字符"},
                    "document_ids": {
                        "type": "array",
                        "description": "最多 20 个 document ID",
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "description": "1 到 50，默认 10"},
                },
                "required": ["text"],
            },
        },
        "required": [
            "session_id",
            "project_id",
            "release_version",
            "release_fingerprint",
            "question",
        ],
        "description": "至少提供 structured_query、reasoning_query、document_fact_query、document_query 或非空 entity_iris；Python 运行时执行全部约束校验。",
    },
    "annotations": {
        "title": "发布绑定的实时本体问答",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


def _validated_description_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    allowed = {"session_id", "project_id", "release_version", "release_fingerprint", "kind", "name"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise RealtimeQaToolError("能力详情仅接受发布身份、kind与name，不接受文件路径或SQL")
    identity = {key: _required_text(arguments, key) for key in (
        "session_id", "project_id", "release_version", "release_fingerprint",
    )}
    if not SESSION_ID.fullmatch(identity["session_id"]) or not FINGERPRINT.fullmatch(identity["release_fingerprint"]):
        raise RealtimeQaToolError("能力详情的会话或发布指纹格式无效")
    kind, name = arguments.get("kind"), arguments.get("name")
    if kind is not None and kind not in {"structured", "reasoning", "document", "document_fact"}:
        raise RealtimeQaToolError("kind 必须是 structured、reasoning、document 或 document_fact")
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name)):
        raise RealtimeQaToolError("name 格式无效")
    return {
        "session_id": identity.pop("session_id"), "expected_release": identity,
        **({"kind": kind} if kind is not None else {}),
        **({"name": name} if name is not None else {}),
    }


DESCRIBE_TOOL = {
    "name": "describe_ontology_capability",
    "description": "按需只读查看会话绑定发布版本的查询、推理或文档能力。省略name返回轻量目录；指定name返回参数、结果字段、来源及已校验的冻结计算定义。不会执行查询，不返回验证预期答案；定义不足明确UNKNOWN。",
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            **{key: TOOL["inputSchema"]["properties"][key] for key in (
                "session_id", "project_id", "release_version", "release_fingerprint",
            )},
            "kind": {"type": "string", "enum": ["structured", "reasoning", "document", "document_fact"]},
            "name": {"type": "string", "description": "发布目录声明的能力名称，省略时返回目录"},
        },
        "required": ["session_id", "project_id", "release_version", "release_fingerprint"],
    },
    "annotations": {"title": "发布能力详情", "readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}


READ_RESULTS_TOOL = {
    "name": "read_ontology_query_results",
    "description": "只读分页取回本会话已有完整证据回执中的真实查询结果，不重跑查询。传入原receipt的ID、SHA256、query_id及同一会话发布身份。可按CQ/能力筛选，offset/limit翻页；只返回结果行与来源，不返回事实全集。",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {
            **{key: TOOL["inputSchema"]["properties"][key] for key in (
                "session_id", "project_id", "release_version", "release_fingerprint")},
            "receipt_id": {"type": "string", "description": "原真实回执EVD ID"},
            "receipt_sha256": {"type": "string", "description": "原真实回执sha256"},
            "query_id": {"type": "string"},
            "source_question_id": {"type": "string", "description": "可选CQ编号"},
            "capability_name": {"type": "string", "description": "可选已执行能力名称"},
            "offset": {"type": "integer", "description": "起始行，默认0"},
            "limit": {"type": "integer", "description": "每页1至50行，默认20"},
        }, "required": ["session_id", "project_id", "release_version", "release_fingerprint",
                          "receipt_id", "receipt_sha256", "query_id"]},
    "annotations": {"title": "查询结果分页回读", "readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}


def _validated_result_page_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    from services.realtime_qa.result_pages import ResultPageRequest

    allowed = set(READ_RESULTS_TOOL["inputSchema"]["properties"])
    if set(arguments) - allowed:
        raise RealtimeQaToolError("结果回读不接受文件路径或未声明参数")
    payload = {key: value for key, value in arguments.items()
               if key not in {"project_id", "release_version", "release_fingerprint"}}
    payload["expected_release"] = {key: arguments.get(key) for key in (
        "project_id", "release_version", "release_fingerprint")}
    try:
        return ResultPageRequest.model_validate(payload).model_dump(exclude_none=True)
    except ValueError as exc:
        raise RealtimeQaToolError("结果回读需要有效真实回执身份和有界分页参数") from exc


QUERY_SPACE_TOOL = {
    "name": "describe_ontology_query_space",
    "description": "只读发现当前会话发布版本可查询的数据集、术语与来源范围；已有命名能力不匹配时使用。首次看摘要，按dataset/search筛选并用各数据集next_offset分页读取所需术语，不一次加载全部定义；不执行查询，不新增已批准业务规则。",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {**{key: TOOL["inputSchema"]["properties"][key] for key in (
            "session_id", "project_id", "release_version", "release_fingerprint")},
            "dataset": {"type": "string", "minLength": 1, "maxLength": 128},
            "search": {"type": "string", "maxLength": 120},
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        "required": ["session_id", "project_id", "release_version", "release_fingerprint"]},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}
CHECKED_QUERY_TOOL = {
    "name": "execute_checked_ontology_query",
    "description": "在当前发布来源范围内执行服务端检查的只读SPARQL查询。先发现query space并选择dataset；返回真实结果、截断标记及证据回执。结果为GENERATED_CHECKED_NOT_BUSINESS_APPROVED分析，不是新增批准CQ；truncated时不可声称全量。",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {**{key: TOOL["inputSchema"]["properties"][key] for key in QUERY_SPACE_TOOL["inputSchema"]["required"]},
            "question": {"type": "string", "minLength": 1, "maxLength": 1000},
            "sparql": {"type": "string", "minLength": 1, "maxLength": 4096},
            "dataset": {"type": "string", "minLength": 1, "maxLength": 128},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "query_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "required": [*QUERY_SPACE_TOOL["inputSchema"]["required"], "question", "sparql", "dataset"]},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}


def _validated_checked_query_payload(arguments: dict[str, Any], *, execute: bool) -> dict[str, Any]:
    tool = CHECKED_QUERY_TOOL if execute else QUERY_SPACE_TOOL
    if not isinstance(arguments, dict) or set(arguments) - set(tool["inputSchema"]["properties"]):
        raise RealtimeQaToolError("查询仅接受声明参数，不接受文件路径或其它执行入口")
    identity_keys = QUERY_SPACE_TOOL["inputSchema"]["required"]
    if any(not isinstance(arguments.get(key), str) for key in identity_keys):
        raise RealtimeQaToolError("查询发布身份必须为字符串")
    payload = _validated_description_payload({key: arguments.get(key) for key in identity_keys})
    if execute:
        for key, maximum in (("question", 1000), ("sparql", 4096), ("dataset", 128), ("query_id", 128)):
            if key == "query_id" and key not in arguments:
                continue
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise RealtimeQaToolError(f"{key} 必须是1至{maximum}字符的字符串")
            payload[key] = value
        limit = arguments.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RealtimeQaToolError("limit 必须为1至100的整数")
        payload["limit"] = limit
    else:
        for key, maximum in (("dataset", 128), ("search", 120)):
            if key not in arguments:
                continue
            value = arguments[key]
            if not isinstance(value, str) or len(value) > maximum or (key == "dataset" and not value.strip()):
                raise RealtimeQaToolError(f"{key} 必须是不超过{maximum}字符的有效文本")
            payload[key] = value
        for key, minimum, maximum, default in (("offset", 0, 100000, 0), ("limit", 1, 100, 50)):
            value = arguments.get(key, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise RealtimeQaToolError(f"{key} 必须是{minimum}至{maximum}的整数")
            payload[key] = value
    return payload


class RealtimeQaMcpServer:
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        from harness.wren_analysis_tools import ANALYSIS_TOOL, CONTEXT_TOOL, call_wren_tool
        from harness.wren_project_tools import PROJECT_TOOLS, SPECS, call_project_tool

        request_id = message.get("id")
        if request_id is None:
            return None
        method = message.get("method")
        try:
            if method == "initialize":
                params = message.get("params") or {}
                result = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "orion-release-bound-realtime-qa",
                        "version": "1.0.0",
                    },
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [TOOL, DESCRIBE_TOOL, READ_RESULTS_TOOL, QUERY_SPACE_TOOL, CHECKED_QUERY_TOOL, CONTEXT_TOOL, ANALYSIS_TOOL, *PROJECT_TOOLS]}
            elif method == "tools/call":
                params = message.get("params") or {}
                if params.get("name") in SPECS:
                    result = call_project_tool(params["name"], params.get("arguments") or {}, _request_json)
                    return {"jsonrpc": "2.0", "id": request_id, "result": result}
                if params.get("name") in {CONTEXT_TOOL["name"], ANALYSIS_TOOL["name"]}:
                    result = call_wren_tool(params.get("arguments") or {}, params["name"] == ANALYSIS_TOOL["name"], _request_json)
                    return {"jsonrpc": "2.0", "id": request_id, "result": result}
                if params.get("name") in {QUERY_SPACE_TOOL["name"], CHECKED_QUERY_TOOL["name"]}:
                    execute = params["name"] == CHECKED_QUERY_TOOL["name"]
                    payload = _validated_checked_query_payload(params.get("arguments") or {}, execute=execute)
                    path = "/ontology/realtime/session-checked-query" if execute else "/ontology/realtime/session-query-space"
                    structured = _request_json(path, payload)
                    if (structured.get("session_id") != payload["session_id"]
                        or structured.get("expected_release") != payload["expected_release"]
                        or structured.get("release_match") != "verified"):
                        raise RealtimeQaToolError("受检查查询响应与当前会话发布身份不一致")
                    if execute:
                        receipt = structured.get("evidence_receipt")
                        query_id = structured.get("query_id")
                        expected = {**payload["expected_release"], "session_id": payload["session_id"], "query_id": query_id}
                        if (not isinstance(query_id, str) or not query_id
                            or ("query_id" in payload and payload["query_id"] != query_id)
                            or not isinstance(receipt, dict)
                            or receipt.get("schema_version") != "orion-evidence-receipt-v1"
                            or not re.fullmatch(r"EVD-[a-f0-9]{32}", str(receipt.get("receipt_id") or ""))
                            or not FINGERPRINT.fullmatch(str(receipt.get("sha256") or ""))
                            or any(receipt.get(key) != value for key, value in expected.items())
                            or not isinstance(structured.get("rows"), list)
                            or len(structured["rows"]) > payload["limit"]
                            or type(structured.get("truncated")) is not bool
                            or structured.get("semantic_status") != "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"):
                            raise RealtimeQaToolError("受检查查询结果或回执身份、行数与语义标记无效")
                    encoded = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
                    if len(encoded.encode("utf-8")) > 100_000:
                        raise RealtimeQaToolError("查询响应超过有界输出预算；请缩小查询范围或limit，不可视为完整结果")
                    text = encoded
                    if execute:
                        marker = {"session_id": payload["session_id"], "evidence_receipt": receipt,
                            "answer": {"answer_status": "partial" if structured["truncated"] else "complete",
                                "complete": not structured["truncated"], "evidence_bundle": {
                                    "release": payload["expected_release"], "query_id": query_id,
                                    "generated_at": receipt.get("created_at"), "mode": "checked_query", "snapshot_set_id": None}}}
                        text += "\n\nORION_EVIDENCE_RECEIPT_V1 " + json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
                    return {"jsonrpc": "2.0", "id": request_id, "result": {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": structured, "isError": False}}
                if params.get("name") == READ_RESULTS_TOOL["name"]:
                    payload = _validated_result_page_payload(params.get("arguments") or {})
                    structured = _request_json("/ontology/realtime/session-result-page", payload)
                    if (structured.get("session_id") != payload["session_id"]
                        or structured.get("expected_release") != payload["expected_release"]
                        or structured.get("release_match") != "verified"
                        or any(structured.get(key) != payload[key] for key in ("receipt_id", "receipt_sha256", "query_id"))):
                        raise RealtimeQaToolError("结果页与当前会话、发布版本或证据回执身份不一致")
                    return {"jsonrpc": "2.0", "id": request_id, "result": {
                        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":"))}],
                        "structuredContent": structured, "isError": False}}
                if params.get("name") == DESCRIBE_TOOL["name"]:
                    payload = _validated_description_payload(params.get("arguments") or {})
                    structured = _request_json("/ontology/realtime/session-capability", payload)
                    if (
                        structured.get("session_id") != payload["session_id"]
                        or structured.get("release_match") != "verified"
                        or structured.get("expected_release") != payload["expected_release"]
                        or not isinstance(structured.get("description"), dict)
                    ):
                        raise RealtimeQaToolError("能力详情与当前会话发布身份不一致")
                    return {"jsonrpc": "2.0", "id": request_id, "result": {
                        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":"))}],
                        "structuredContent": structured, "isError": False,
                    }}
                if params.get("name") != TOOL["name"]:
                    raise RealtimeQaToolError("不允许的工具")
                payload = _validated_payload(params.get("arguments") or {})
                structured = _request_json(
                    "/ontology/realtime/session-answer",
                    payload,
                )
                projection = _safe_answer_projection(structured, payload)
                text = _compact_answer(projection)
                if projection["answer"]["answer_text_truncated"]:
                    text += "\n\n回答摘要已截断，完整内容见证据回执。"
                if projection["evidence_receipt"]:
                    marker = {
                        "session_id": projection["session_id"],
                        "evidence_receipt": projection["evidence_receipt"],
                        "answer": {key: projection["answer"][key] for key in ("answer_status", "complete")},
                    }
                    marker["answer"]["evidence_bundle"] = {
                        key: projection["answer"]["evidence_bundle"][key]
                        for key in ("release", "query_id", "generated_at", "mode", "snapshot_set_id")
                    }
                    marker["answer"]["evidence_bundle"]["release"] = payload["expected_release"]
                    text += "\n\nORION_EVIDENCE_RECEIPT_V1 " + json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
                else:
                    text += "\n\n本次返回未提供完整证据回执引用；无法从摘要恢复完整推导链。"
                result = {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": projection,
                    "isError": False,
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (RealtimeQaToolError, ValueError, TypeError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }


def main() -> None:
    server = RealtimeQaMcpServer()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = server.handle(message)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
