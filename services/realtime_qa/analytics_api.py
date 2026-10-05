"""3081 analytics: release models, database execution and evidenced assets."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from services.realtime_qa.analysis_assets import AnalysisAssetStore, result_digest
from services.realtime_qa.analytics_execution import run_project_query
from services.realtime_qa.analytics_models import (
    AnalyticsAssetRequest,
    AnalyticsCatalogRequest,
    AnalyticsExportRequest,
    AnalyticsQueryRequest,
)
from services.realtime_qa.evidence_receipts import persist_session_answer
from services.realtime_qa.models import (
    EvidenceBundle,
    EvidenceRecord,
    RealtimeAnswer,
    RealtimeSessionAnswer,
    SourceStatus,
)
from services.realtime_qa.result_pages import verify_live_release
from services.realtime_qa.runtime import runtime_binding
from services.realtime_qa.wren_analysis import digest

SNAPSHOT_SCOPE = ("对当前发布版本绑定的数据库快照执行完整范围的筛选、关联与聚合。返回行数限制只限制结果展示，不先截取输入样本。"
         "快照不代表上游实时数据；空值按已发布字段定义与 SQL 规则处理。补充分析及用户保存的经验不替代正式业务审批。")

LIVE_SCOPE = ("在已发布业务模型与授权表列范围内，直接查询当前数据库已提交的数据。"
              "结果对应本次只读事务，历史回执保持原结果；再次查询会产生新回执。分析不替代正式业务审批。")


def analysis_scope(descriptor):
    return LIVE_SCOPE if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE" else SNAPSHOT_SCOPE


def identity(request):
    return {"session_id": request.session_id, **request.expected_release.model_dump()}


def response_identity(request):
    return {"session_id": request.session_id, "expected_release": request.expected_release.model_dump(), "release_match": "verified"}


def persist_project_analysis(store, runtime, request, descriptor, output):
    now = datetime.now(UTC)
    query_id = "Q-WREN-" + uuid4().hex[:16]
    chart = request.chart.model_dump()
    columns = output["variables"]
    if (chart["x"] and chart["x"] not in columns) or any(name not in columns for name in chart["y"]):
        raise ValueError("图表字段必须来自本次查询返回列。")
    if chart["kind"] != "table" and not chart["y"]:
        raise ValueError("图表须明确选择查询结果中的指标列；不自动猜测业务指标。")
    analysis = {key: output[key] for key in ("engine_version", "core_version", "sql", "expanded_sql", "mdl_sha256", "row_types", "source_row_counts", "number_encoding", "requested_sql") if key in output}
    live = descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE"
    scope = "LIVE_SOURCE_DATABASE" if live else "RELEASE_SNAPSHOT_DATABASE"
    freshness = output.get("data_freshness") or {"status": "UNKNOWN" if live else "SNAPSHOT", "upstream_requeried": False,
        "snapshot_id": runtime.binding.snapshot_set_id, "queried_at": now.isoformat(),
        "note_zh": "未取得实时来源新鲜度证据。" if live else "读取发布时的固定快照，不代表上游当前数据。"}
    basis = {"kind": "DYNAMIC_LIVE_SOURCE"} if live else {"kind": "UNKNOWN"}
    snapshot = runtime.binding.cross_source_snapshot_set or {}
    if not live and snapshot.get("manifest_sha256") and runtime.binding.snapshot_set_id:
        basis = {"kind": "PUBLISHED_SNAPSHOT", "dataset_id": runtime.binding.snapshot_set_id,
                 "version": runtime.binding.release_version, "sha256": snapshot["manifest_sha256"]}
    analysis.update(engine="WrenAI", execution_scope=scope, chart=chart,
        queried_at=output.get("queried_at", now.isoformat()), model_version=runtime.binding.release_version,
        data_freshness=freshness, evaluation_basis=basis,
        transaction_snapshot=output.get("transaction_snapshot") or output.get("transaction", {}).get("transaction_snapshot"),
        transaction=output.get("transaction"), parameters_sha256=output.get("parameters_sha256"),
        elapsed_ms=output.get("elapsed_ms"), source_validation=output.get("source_validation"), query_started_at=output.get("query_started_at"),
        query_finished_at=output.get("query_finished_at"),
        request=request.model_dump(exclude={"session_id", "expected_release"}),
        snapshot_set_id=None if live else runtime.binding.snapshot_set_id, model_key=descriptor["key"],
        input_sampled=False, result_limit=request.limit, source_tables=descriptor["source_tables"],
        source_row_count=None if live else sum(t["expected_count"] for t in descriptor["source_tables"]),
        row_count_basis="NOT_COUNTED_LIVE_SOURCE" if live else "PUBLISHED_SNAPSHOT_MANIFEST", source_data_transferred_to_model=False)
    warning = analysis_scope(descriptor) + (" 当前结果超过显示上限，已标明截断；请增加筛选或按更高粒度汇总。" if output["truncated"] else "")
    source_refs = sorted({t["source_id"] for t in descriptor["source_tables"]}) if live else [runtime.binding.snapshot_set_id]
    source_ref = ",".join(source_refs)
    result = {"capability_name": "wren_project_analysis", "source_question_id": query_id,
        "rows": output["rows"], "variables": columns, "row_count": len(output["rows"]), "truncated": output["truncated"],
        "semantic_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED", "query_sha256": digest(output["sql"]),
        "source_refs": source_refs, "answer_scope_zh": warning, "analysis": analysis}
    record = EvidenceRecord(evidence_id="EV-WREN-" + uuid4().hex[:16], source_kind="structured_db",
        source_ref=source_ref, observed_at=now, payload={"cq_results": [result], "question": request.question})
    release = runtime.binding.model_copy(update={"document_fact_queries": {}, "reasoning_capabilities": {}, "ontop_query_capabilities": {}})
    bundle = EvidenceBundle(query_id=query_id, mode="structured", release=release, generated_at=now,
        complete=not output["truncated"], evidence=[record],
        source_status={"structured_db": SourceStatus(source_kind="structured_db", status="degraded" if live and freshness.get("status") not in {"LIVE_QUERY", "FRESH"} else "fresh",
            source_ref=source_ref, queried_at=now, version=release.release_version,
            details={"freshness_scope": scope, **freshness})})
    answer = RealtimeAnswer(question=request.question, answer_status="partial" if output["truncated"] else "complete",
        complete=not output["truncated"], answer=f"数据库完成分析，返回 {len(output['rows'])} 行结果。{warning}",
        citations=[], warnings=[warning], evidence_bundle=bundle)
    saved = persist_session_answer(store, RealtimeSessionAnswer(session_id=request.session_id,
        runtime_binding=runtime_binding(runtime), answer=answer))
    return {**result, **response_identity(request), "query_id": query_id, "evidence_receipt": saved.evidence_receipt,
            "scope_warning_zh": warning, "release_modified": False}


def receipt_result(store, request, reference):
    full = {**identity(request), **reference}
    envelope = json.loads(store.read(full))
    bundle = envelope["response"]["answer"]["evidence_bundle"]
    results = [result for record in bundle.get("evidence", [])
               for result in record.get("payload", {}).get("cq_results", [])
               if result.get("analysis", {}).get("engine") == "WrenAI"]
    if len(results) != 1:
        raise ValueError("所选回执必须对应唯一的已执行 Wren 分析。")
    return full, results[0]



def catalog_model_projections(descriptor):
    """Expose approved field lineage without serializing execution metadata."""
    sources = {source["name"]: source for source in descriptor.get("source_tables", [])}
    models = {model["name"]: model for model in descriptor.get("models", [])}
    result = []
    for projection in descriptor.get("model_projections", []):
        source = sources.get(projection.get("source_name"))
        model = models.get(projection.get("model"))
        if source is None or model is None:
            continue
        source_columns = set(source.get("columns", []))
        model_columns = {column["name"] for column in model.get("columns", [])}
        result.append({"model": projection["model"], "source_name": projection["source_name"],
            "identity_columns": [column for column in projection.get("identity_columns", []) if column in source_columns],
            "columns": [{key: column[key] for key in ("physical", "name", "type") if key in column}
                for column in projection.get("columns", [])
                if column.get("physical") in source_columns and column.get("name") in model_columns]})
    return result


AGGREGATE_LABELS = {"sum": "{label}合计", "avg": "{label}平均值", "min": "{label}最小值",
                    "max": "{label}最大值", "count_known": "{label}已知数"}


def catalog_column_labels(descriptor):
    """Map compiled technical column names to published Chinese labels.

    Labels come from the release mapping label_zh only. A column without a
    published label keeps its technical name; no Chinese wording is invented,
    and the MDL, SQL and receipt column names are never rewritten.
    """
    labels = {}
    models = [model for model in descriptor.get("models", []) if isinstance(model.get("name"), str) and model.get("name")]
    published = {}
    for model in models:
        label = (model.get("properties") or {}).get("label_zh")
        published[model["name"]] = label if isinstance(label, str) and label else model["name"]
    for model in models:
        name = model["name"]
        columns = model.get("columns", [])
        direct = {}
        for column in columns:
            field = column.get("name")
            label = (column.get("properties") or {}).get("label_zh")
            if isinstance(field, str) and isinstance(label, str) and label and label != field:
                direct[field] = label
        relations = {}
        for column in columns:
            handle, target = column.get("name"), column.get("type")
            if not column.get("relationship") or not isinstance(handle, str):
                continue
            related = next((item for item in models if item["name"] == target), None)
            fields = {}
            for item in (related or {}).get("columns", []):
                item_label = (item.get("properties") or {}).get("label_zh")
                if isinstance(item.get("name"), str) and isinstance(item_label, str) and item_label:
                    fields[item["name"]] = item_label
            relations[handle] = {"label": direct.get(handle) or published.get(target, handle), "fields": fields}
        model_labels = dict(direct)
        for column in columns:
            field = column.get("name")
            if not isinstance(field, str) or "__" not in field:
                continue
            handle, remainder = field.split("__", 1)
            relation = relations.get(handle)
            if relation is None:
                continue
            target_label = relation["fields"].get(remainder)
            if target_label and target_label != remainder:
                model_labels[field] = relation["label"] + "·" + target_label
        cube = next((item for item in descriptor.get("cubes", []) if item.get("baseObject") == name), None)
        cube_labels = {}
        if cube is not None:
            for measure in cube.get("measures", []):
                measure_name = measure.get("name")
                if not isinstance(measure_name, str) or not measure_name:
                    continue
                if measure_name == "count_rows":
                    cube_labels[measure_name] = "记录数"
                    continue
                for prefix, template in AGGREGATE_LABELS.items():
                    if not measure_name.startswith(prefix + "_"):
                        continue
                    label = model_labels.get(measure_name[len(prefix) + 1:])
                    if label:
                        cube_labels[measure_name] = template.format(label=label)
                    break
            for dimension in [*cube.get("dimensions", []), *cube.get("timeDimensions", [])]:
                field = dimension.get("name")
                if isinstance(field, str) and model_labels.get(field):
                    cube_labels[field] = model_labels[field]
        merged = {**model_labels, **cube_labels}
        if merged:
            labels[name] = merged
            if cube is not None and isinstance(cube.get("name"), str):
                labels[cube["name"]] = dict(merged)
    return labels

def register_analytics_api(app: FastAPI, store, resolve_runtime, *, assets=None, exports=None):
    from services.realtime_qa.analytics_exports import AnalyticsExportJobs
    exports = exports or AnalyticsExportJobs()
    assets = assets or AnalysisAssetStore(Path(os.environ.get("ORION_ANALYSIS_ASSET_ROOT") or Path(__file__).resolve().parents[2] / ".orion-runtime/wren/analysis-assets"))

    def runtime_for(request):
        runtime = resolve_runtime(request.expected_release.project_id)
        runtime.ensure_available()
        verify_live_release(runtime.binding, request.expected_release)
        return runtime

    def project_for(runtime, data_mode="AUTO"):
        from services.realtime_qa.live_analytics import resolve_analysis_project
        from services.realtime_qa.wren_project import build_project
        return resolve_analysis_project(runtime.binding, build_project(runtime.binding), data_mode)

    def execute(request):
        runtime = runtime_for(request)
        descriptor = project_for(runtime, request.data_mode)
        output = run_project_query(runtime.binding, descriptor, request)
        verify_live_release(runtime.binding, request.expected_release)
        return persist_project_analysis(store, runtime, request, descriptor, output)

    @app.post("/ontology/realtime/session-analytics-catalog")
    def catalog(request: AnalyticsCatalogRequest) -> dict:
        try:
            runtime = runtime_for(request)
            descriptor = project_for(runtime, request.data_mode)
            projection = {key: descriptor[key] for key in ("key", "adapter_version", "mdl_sha256", "models", "relationships", "views", "cubes", "knowledge", "omitted") if key in descriptor}
            live = descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE"
            projection.update(mdl_sha256=descriptor.get("mdl_postgres_sha256", descriptor["mdl_sha256"]),
                model_projections=catalog_model_projections(descriptor),
                column_labels=catalog_column_labels(descriptor),
                snapshot_set_id=None if live else runtime.binding.snapshot_set_id,
                execution_scope="LIVE_SOURCE_DATABASE" if live else "RELEASE_SNAPSHOT_DATABASE",
                sources=[{**{key: t.get(key) for key in ("name", "source_id", "source_table", "dataset_id", "expected_count", "columns")},
                    "database": descriptor.get("_registrations", {}).get(t.get("source_id"), {}).get("database"),
                    "registration_status": descriptor.get("_registrations", {}).get(t.get("source_id"), {}).get("state", "SNAPSHOT_ONLY")} for t in descriptor["source_tables"]],
                source_services=[{k: r.get(k) for k in ("source_id", "database", "provider", "state", "query_node", "chat2db_binding_status", "dataset_type")} for r in descriptor.get("_registrations", {}).values()],
                scope_warning_zh=analysis_scope(descriptor), engine_version="0.15.0", execution_mode="DATABASE_PUSHDOWN",
                input_row_limit=None, result_row_limit=500,
                scale_note_zh="输入数据不先拉取或截断；查询性能受数据库与索引影响。复杂查询受执行超时和并发限制。",
                memories=assets.search_memories(identity(request), request.search) if request.search else [],
                knowledge_matches=assets.search_knowledge(identity(request), request.search) if request.search else [])
            return {**response_identity(request), **projection}
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/ontology/realtime/session-analytics-query")
    def query(request: AnalyticsQueryRequest) -> dict:
        try:
            return execute(request)
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/ontology/realtime/session-analytics-assets")
    def asset_action(request: AnalyticsAssetRequest) -> dict:
        try:
            runtime_for(request)
            scope = identity(request)
            refs, results = [], []
            for reference in request.receipts:
                full, result = receipt_result(store, request, reference.model_dump())
                refs.append(full)
                results.append(result)
            if request.action == "list":
                result = {"assets": assets.list_assets(scope)}
            elif request.action == "search":
                result = {"memories": assets.search_memories(scope, request.question), "knowledge": assets.search_knowledge(scope, request.question)}
            elif request.action == "get":
                asset = assets.get(scope, request.asset_id)
                report_results = []
                asset_refs = asset["payload"].get("receipt_refs", [])
                if asset["payload"].get("receipt_ref"):
                    asset_refs = [*asset_refs, asset["payload"]["receipt_ref"]]
                for ref in asset_refs:
                    _, record = receipt_result(store, request, ref)
                    report_results.append({**record, "evidence_receipt": ref, "query_id": ref["query_id"], "session_id": request.session_id})
                result = {"asset": asset, "results": report_results}
            elif request.action in {"remember", "evaluation"}:
                record = results[0]
                plan = record["analysis"].get("request")
                if not plan or record.get("truncated") is not False:
                    raise ValueError("仅可保存完整的数据库分析作为可重放查询或回归基线。")
                if request.action == "remember":
                    result = {"asset": assets.save_memory(scope, request.question or plan["question"], plan, refs[0], confirmed=request.confirmed)}
                else:
                    result = {"asset": assets.save_evaluation(scope, request.title or plan["question"], plan["question"], plan,
                        expected_result_sha256=result_digest(record["rows"]), expected_row_count=len(record["rows"]), receipt_ref=refs[0], basis=record["analysis"].get("evaluation_basis"))}
            elif request.action == "knowledge":
                result = {"asset": assets.save_knowledge(scope, request.title, request.definition, refs, confirmed=request.confirmed)}
            elif request.action == "report":
                result = {"asset": assets.save_report(scope, request.title or "分析报告", refs, request.layout or {"kind": "stack"})}
            else:
                asset = assets.get(scope, request.asset_id)
                if asset["kind"] != "evaluation":
                    raise ValueError("所选资产不是回归基线。")
                saved_query = AnalyticsQueryRequest.model_validate({**asset["payload"]["query"],
                    "session_id": request.session_id, "expected_release": request.expected_release})
                executed = execute(saved_query)
                if executed["truncated"]:
                    raise ValueError("回归执行结果截断，不能签发通过结论。")
                result = {"evaluation": assets.evaluate(scope, request.asset_id, executed["rows"], actual_basis=executed["analysis"].get("evaluation_basis")), "execution": executed}
            return {**response_identity(request), **result, "release_modified": False}
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/ontology/realtime/session-analytics-export")
    def export_action(request: AnalyticsExportRequest):
        try:
            runtime = runtime_for(request)
            scope = identity(request)
            if request.action == "create":
                reference, record = receipt_result(store, request, request.receipt.model_dump())
                plan = record.get("analysis", {}).get("request")
                if not plan:
                    raise ValueError("该回执没有可重新执行的数据库分析计划。")
                saved = AnalyticsQueryRequest.model_validate({**plan, "session_id": request.session_id,
                    "expected_release": request.expected_release,
                    "data_mode": "LIVE" if record["analysis"].get("execution_scope") == "LIVE_SOURCE_DATABASE" else "SNAPSHOT"})
                descriptor = project_for(runtime, saved.data_mode)
                job = exports.create(scope, runtime.binding, descriptor, saved, reference, request.max_rows,
                                     lambda: runtime_for(request))
            elif request.action == "cancel":
                job = exports.cancel(scope, request.job_id)
            elif request.action == "status":
                job = exports.status(scope, request.job_id)
            else:
                stream, job = exports.download(scope, request.job_id)
                def chunks():
                    try:
                        yield from iter(lambda: stream.read(65536), b"")
                    finally:
                        stream.close()
                return StreamingResponse(chunks(), media_type="text/csv; charset=utf-8", headers={
                    "Content-Disposition": f'attachment; filename="{request.job_id}.csv"',
                    "Cache-Control": "no-store", "X-Content-SHA256": job["sha256"],
                    "X-Orion-Session": request.session_id,
                    "X-Orion-Release": request.expected_release.release_fingerprint})
            return {**response_identity(request), "job": job}
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
