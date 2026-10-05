"""Release-bound Wren model/cube and analysis-library MCP tools."""
from __future__ import annotations

import json
import re

from harness.wren_analysis_tools import _tool
from services.realtime_qa.analytics_models import (
    AnalyticsAssetRequest,
    AnalyticsCatalogRequest,
    AnalyticsExportRequest,
    AnalyticsQueryRequest,
)

CATALOG_TOOL = _tool("describe_ontology_analytics",
    "读取本发布版本持久分析模型、字段、关联、视图、Cube指标、知识和已确认查询经验。用于跨表统计、时间趋势和大数据分析；先读目录再选精确模型字段。execution_scope区分LIVE_SOURCE_DATABASE实时服务与RELEASE_SNAPSHOT_DATABASE固定快照。最新问题要求data_mode=LIVE，无服务时不得用旧快照替代。input_row_limit=null表示源端聚合不先截样，不代表无限资源。omitted列明未投影的语义，不得猜测。", AnalyticsCatalogRequest)
QUERY_TOOL = _tool("query_ontology_analytics",
    "使用官方Wren按已发布业务模型对已登记实时源或固定快照执行数据库端分析，输入不受1000行限制；sql或cube_query二选一。仅可访问目录中的模型、视图和字段，禁止物理库表限定/文件读取/DDL/DML；复杂业务推理仍调用正式推理工具。支持模型关联、计算字段、聚合、时间Cube、窗口和多轮追问。limit仅限制输出(最多500)，truncated必须如实说明。可传chart={kind:bar|line|kpi|table,x:结果列,y:[指标列]}，返回可回看的证据和图表。只回答实际执行结果；字段或口径歧义须先澄清。失败后依错误修正计划，最多两次，不绕过边界。", AnalyticsQueryRequest)
ASSETS_TOOL = _tool("manage_ontology_analysis_assets",
    "查询或保存本会话、本发布版本的分析资产。list/search/get为读取；report将真实分析回执保存为报告；remember/knowledge/evaluation仅在用户明确要求确认查询经验、口径说明或回归基线后以confirmed=true调用，不得自动代确认。query从真实回执提取；动态源评测只比较数据变化，不把变化自动判为模型退化；不接受凭据或执行指令。evaluate重跑保存的查询并比较结果，生成真实新回执。用户确认不等于正式业务批准，也不修改本体发布。", AnalyticsAssetRequest)
ASSETS_TOOL["annotations"].update(readOnlyHint=False, idempotentHint=False)
EXPORT_TOOL = _tool("manage_ontology_analysis_exports",
    "按已有数据库分析回执创建、查询或取消完整CSV导出任务；create会重新查询当前来源并记录新事务时间，不是原结果的续页。max_rows默认100000、上限1000000，100MiB/130秒保护，超过预算不生成部分文件。只在用户要求导出时创建，不能后台自动导出；下载在3081分析面板完成，不调用download动作。", AnalyticsExportRequest)
EXPORT_TOOL["annotations"].update(readOnlyHint=False, idempotentHint=False)
PROJECT_TOOLS = [CATALOG_TOOL, QUERY_TOOL, ASSETS_TOOL, EXPORT_TOOL]
SPECS = {
    CATALOG_TOOL["name"]: (AnalyticsCatalogRequest, "catalog"),
    QUERY_TOOL["name"]: (AnalyticsQueryRequest, "query"),
    ASSETS_TOOL["name"]: (AnalyticsAssetRequest, "assets"),
    EXPORT_TOOL["name"]: (AnalyticsExportRequest, "export"),
}


def call_project_tool(name, arguments, request_json):
    model, endpoint = SPECS[name]
    if not isinstance(arguments, dict):
        raise ValueError("分析参数必须是对象。")
    payload = dict(arguments)
    payload["expected_release"] = {key: payload.pop(key, None) for key in ("project_id", "release_version", "release_fingerprint")}
    validated = model.model_validate(payload).model_dump(exclude_none=True)
    if endpoint == "export" and validated["action"] == "download":
        raise ValueError("请在3081分析面板下载文件，工具不把完整导出内容装入模型上下文。")
    result = request_json("/ontology/realtime/session-analytics-" + endpoint, validated)
    if (result.get("session_id") != validated["session_id"] or result.get("expected_release") != validated["expected_release"]
            or result.get("release_match") != "verified"):
        raise ValueError("分析响应与当前会话发布身份不一致。")
    executed = result if endpoint == "query" else result.get("execution") if endpoint == "assets" else None
    marker = None
    if executed:
        receipt = executed.get("evidence_receipt") or {}
        expected = {**validated["expected_release"], "session_id": validated["session_id"], "query_id": executed.get("query_id")}
        if (not expected["query_id"] or not isinstance(executed.get("rows"), list)
                or type(executed.get("truncated")) is not bool
                or executed.get("semantic_status") != "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"
                or receipt.get("schema_version") != "orion-evidence-receipt-v1"
                or not re.fullmatch(r"EVD-[a-f0-9]{32}", str(receipt.get("receipt_id")))
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(receipt.get("sha256")))
                or any(receipt.get(key) != value for key, value in expected.items())):
            raise ValueError("分析执行回执不完整或身份不匹配。")
        marker = {"session_id": validated["session_id"], "evidence_receipt": receipt,
            "answer": {"answer_status": "partial" if executed["truncated"] else "complete", "complete": not executed["truncated"],
                "evidence_bundle": {"release": validated["expected_release"], "query_id": expected["query_id"],
                    "generated_at": receipt.get("created_at"), "mode": "wren_analysis", "snapshot_set_id": executed.get("analysis", {}).get("snapshot_set_id")}}}
        executed["analysis"] = {key: value for key, value in executed.get("analysis", {}).items() if key not in {"expanded_sql", "source_tables"}}
        # Receipt retains the whole returned result. Context previews never become
        # a new query's source population or a false full-result declaration.
        if len(json.dumps(executed, ensure_ascii=False).encode()) > 80000:
            executed["rows"] = []
            executed["model_preview_only"] = True
            executed["preview_note"] = "完整返回结果已保存；请read_ontology_query_results分页回读。"
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode()) > 120000:
        raise ValueError("分析目录或资产超过单次工具预算；请缩小检索范围。")
    if marker:
        encoded += "\n\nORION_EVIDENCE_RECEIPT_V1 " + json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": encoded}], "structuredContent": result, "isError": False}
