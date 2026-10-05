"""Wren tools use the same guarded Core API as the existing Q&A tools."""
from __future__ import annotations

import json
import re

from services.realtime_qa.wren_models import AnalysisRequest, AnalysisSource


def _tool(name, description, model):
    schema = model.model_json_schema()
    identity = schema.pop("$defs")["RealtimeReleaseExpectation"]
    definitions = model.model_json_schema().get("$defs", {})
    definitions.pop("RealtimeReleaseExpectation", None)
    schema["properties"].pop("expected_release")
    schema["properties"].update(identity["properties"])
    schema["required"].remove("expected_release")
    schema["required"].extend(identity["required"])
    def harness_schema(node):
        if "$ref" in node:
            return harness_schema(definitions[node["$ref"].split("/")[-1]])
        if "anyOf" in node:
            choices = [harness_schema(item) for item in node["anyOf"]]
            if {item.get("type") for item in choices} >= {"integer", "number"}:
                choices = [item for item in choices if item.get("type") != "integer"]
            return {"oneOf": choices}
        result = {key: node[key] for key in ("type", "required", "additionalProperties", "enum", "const", "description") if key in node}
        if "properties" in node:
            result["properties"] = {key: harness_schema(value) for key, value in node["properties"].items()}
        if "items" in node:
            result["items"] = harness_schema(node["items"])
        constraints = {key: node[key] for key in ("minLength", "maxLength", "minimum", "maximum", "maxItems", "minItems", "pattern", "default") if key in node}
        if constraints:
            result["description"] = (result.get("description", "") + " 服务端约束：" + json.dumps(constraints, ensure_ascii=False)).strip()
        return result

    return {"name": name, "description": description, "inputSchema": harness_schema(schema),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}


CONTEXT_TOOL = _tool("describe_ontology_analysis",
    "发现本会话已有完整查询回执可分析的字段、类型、空值及来源范围。先取得真实查询回执，指定唯一结果；不重新查询源库。最多1000行，截断或不完整结果拒绝分析。", AnalysisSource)
ANALYSIS_TOOL = _tool("analyze_ontology_results",
    "使用官方 Wren 引擎对完整回执做分组、日期趋势和聚合。先 describe_ontology_analysis，再用返回的精确字段制定结构化计划。最多2维度4指标，日期分组仅支持YYYY-MM-DD。count_rows是记录数，不是对象数；count_distinct仅用于已核验身份字段。返回统计和新证据回执，3081证据详情提供图表和原始明细。保持原来源范围；不接受SQL、凭据或路径，不新增已批准业务口径。", AnalysisRequest)


def call_wren_tool(arguments, execute, request_json):
    model = AnalysisRequest if execute else AnalysisSource
    if not isinstance(arguments, dict):
        raise ValueError("分析参数必须是对象。")
    payload = dict(arguments)
    payload["expected_release"] = {key: payload.pop(key, None) for key in (
        "project_id", "release_version", "release_fingerprint")}
    validated = model.model_validate(payload).model_dump(exclude_none=True)
    result = request_json("/ontology/realtime/session-analysis" if execute else "/ontology/realtime/session-analysis-context", validated)
    if (result.get("session_id") != validated["session_id"] or result.get("expected_release") != validated["expected_release"]
            or result.get("release_match") != "verified"):
        raise ValueError("分析响应与当前会话发布身份不一致。")
    projection = dict(result)
    if execute:
        receipt = result.get("evidence_receipt") or {}
        query_id = result.get("query_id")
        expected = {**validated["expected_release"], "session_id": validated["session_id"], "query_id": query_id}
        if (not query_id or receipt.get("schema_version") != "orion-evidence-receipt-v1"
                or not re.fullmatch(r"EVD-[a-f0-9]{32}", str(receipt.get("receipt_id") or ""))
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(receipt.get("sha256") or ""))
                or any(receipt.get(key) != value for key, value in expected.items())
                or result.get("semantic_status") != "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"
                or not isinstance(result.get("rows"), list) or len(result["rows"]) > validated["limit"]
                or type(result.get("truncated")) is not bool):
            raise ValueError("分析回执身份或结果合同不完整。")
        projection["analysis"] = {key: value for key, value in (result.get("analysis") or {}).items()
            if key not in {"detail_rows", "mdl", "groups", "expanded_sql", "details_sql"}}
    text = json.dumps(projection, ensure_ascii=False)
    if execute:
        marker = {"session_id": validated["session_id"], "evidence_receipt": receipt,
            "answer": {"answer_status": "partial" if result["truncated"] else "complete", "complete": not result["truncated"],
                "evidence_bundle": {"release": validated["expected_release"], "query_id": query_id,
                    "generated_at": receipt.get("created_at"), "mode": "wren_analysis", "snapshot_set_id": None}}}
        text += "\n\nORION_EVIDENCE_RECEIPT_V1 " + json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": projection, "isError": False}
