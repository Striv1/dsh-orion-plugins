"""Release-bound Wren analysis and durable parent-receipt lineage."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException

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
from services.realtime_qa.wren_analysis import (
    WREN_PYTHON,
    WREN_VERSION,
    digest,
    load_source,
    run_analysis,
)
from services.realtime_qa.wren_models import AnalysisRequest, AnalysisSource

SCOPE = ("统计范围是所选查询回执中的完整结果行，保留原查询的业务条件和数据时间；不代表源库全部数据或实时更新。"
         "记录数不等于业务对象数；去重计数须使用已核验的对象标识。空值单独分组，数值聚合忽略空值；全空数值不计为零。"
         "本次属于补充分析，不改变已发布模型、规则或批准口径。")


def _identity(request):
    return {"session_id": request.session_id, "expected_release": request.expected_release.model_dump(), "release_match": "verified"}


def persist_analysis(store, runtime, request, source, output):
    now = datetime.now(UTC)
    query_id = "Q-WREN-" + uuid4().hex[:16]
    source_reference = request.model_dump(include=set(AnalysisSource.model_fields))
    analysis = {key: value for key, value in output.items() if key not in {"rows", "variables", "truncated"}}
    analysis.update(source_receipt=source_reference, source_rows_sha256=source["rows_sha256"],
        source_metadata=source["source"], columns=source["columns"],
        type_decoding=source["type_decoding"], analysis_rows_sha256=digest(source["analysis_rows"]),
        plan=request.model_dump(include={"dimensions", "metrics", "filters", "limit"}),
        detail_rows=source["rows"], execution_scope="IMMUTABLE_RECEIPT_ROWS")
    warning = SCOPE + (" 分组结果超过显示上限，当前图表不包含全部分组。" if output["truncated"] else "")
    result = {"capability_name": "wren_analysis", "source_question_id": query_id,
        "rows": output["rows"], "row_count": len(output["rows"]), "variables": output["variables"],
        "truncated": output["truncated"], "semantic_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED",
        "answer_scope_zh": warning, "query_sha256": digest(output["sql"]),
        "source_refs": [request.receipt_id], "analysis": analysis}
    record = EvidenceRecord(evidence_id="EV-WREN-" + uuid4().hex[:16], source_kind="structured_db",
        source_ref=request.receipt_id, observed_at=now, payload={"cq_results": [result], "question": request.question})
    receipt_release = runtime.binding.model_copy(update={"document_fact_queries": {}, "reasoning_capabilities": {}, "ontop_query_capabilities": {}})
    bundle = EvidenceBundle(query_id=query_id, mode="structured", release=receipt_release,
        generated_at=now, complete=not output["truncated"], evidence=[record],
        source_status={"structured_db": SourceStatus(source_kind="structured_db", status="fresh",
            source_ref=request.receipt_id, queried_at=now, version=runtime.binding.release_version,
            details={"freshness_scope": "IMMUTABLE_RECEIPT_READBACK_ONLY", "upstream_requeried": False})})
    answer = RealtimeAnswer(question=request.question, answer_status="partial" if output["truncated"] else "complete",
        complete=not output["truncated"], answer=f"基于 {len(source['rows'])} 条完整结果记录完成统计，返回 {len(output['rows'])} 组。{warning}",
        citations=[], warnings=[warning], evidence_bundle=bundle)
    saved = persist_session_answer(store, RealtimeSessionAnswer(session_id=request.session_id,
        runtime_binding=runtime_binding(runtime), answer=answer))
    return {**result, **_identity(request), "query_id": query_id, "engine_version": WREN_VERSION,
        "evidence_receipt": saved.evidence_receipt, "scope_warning_zh": warning, "release_modified": False}


def register_wren_api(app: FastAPI, store, resolve_runtime):
    @app.post("/ontology/realtime/session-analysis-context")
    def context(request: AnalysisSource) -> dict:
        try:
            runtime = resolve_runtime(request.expected_release.project_id)
            source = load_source(store, runtime, request)
            verify_live_release(runtime.binding, request.expected_release)
            return {**_identity(request), "available": WREN_PYTHON.is_file(), "engine_version": WREN_VERSION,
                "source_row_count": len(source["rows"]), "source_rows_sha256": source["rows_sha256"],
                "columns": source["columns"], "source": source["source"], "type_decoding": source["type_decoding"], "scope_warning_zh": SCOPE,
                "supported_operations": ["count_rows", "count_non_null", "count_distinct", "sum", "avg", "min", "max"]}
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/ontology/realtime/session-analysis")
    def analyze(request: AnalysisRequest) -> dict:
        try:
            runtime = resolve_runtime(request.expected_release.project_id)
            source = load_source(store, runtime, request)
            output = run_analysis(source, request)
            verify_live_release(runtime.binding, request.expected_release)
            return persist_analysis(store, runtime, request, source, output)
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
