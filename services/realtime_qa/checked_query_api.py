"""Session/release guarded query extensions with durable execution receipts."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from services.realtime_qa.evidence_receipts import EvidenceReceiptStore, persist_session_answer
from services.realtime_qa.models import (
    EvidenceBundle,
    EvidenceRecord,
    RealtimeAnswer,
    RealtimeReleaseExpectation,
    RealtimeSessionAnswer,
    SourceStatus,
)
from services.realtime_qa.result_pages import verify_live_release
from services.realtime_qa.runtime import runtime_binding


class QueryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^session-[a-f0-9-]{36}$")
    expected_release: RealtimeReleaseExpectation


class QuerySpaceRequest(QueryIdentity):
    dataset: str | None = Field(default=None, min_length=1, max_length=128)
    search: str | None = Field(default=None, max_length=120)
    offset: int = Field(default=0, ge=0, le=100000, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class CheckedQueryRequest(QueryIdentity):
    question: str = Field(min_length=2, max_length=1000)
    sparql: str = Field(min_length=1, max_length=4096)
    dataset: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=100, ge=1, le=100, strict=True)
    query_id: str = Field(default_factory=lambda: "Q-CHECKED-" + uuid4().hex[:16], min_length=1, max_length=128)


def _identity(request: QueryIdentity) -> dict[str, Any]:
    return {"session_id": request.session_id, "expected_release": request.expected_release.model_dump(),
            "release_match": "verified"}


def _query_space_page(result: dict[str, Any], request: QuerySpaceRequest) -> dict[str, Any]:
    datasets = []
    search = (request.search or "").casefold().strip()
    remaining = request.limit
    for dataset in result.get("datasets", []):
        if request.dataset and dataset["dataset"] != request.dataset:
            continue
        terms = dataset.get("terms") or []
        matches = [term for term in terms if not search or search in str(term).casefold()]
        # Metadata is fetched on demand. Do not repeat the internal executor's
        # whole vocabulary as term_kinds, allowed_iris, and a second dataset.
        item = {key: value for key, value in dataset.items()
                if key not in {"terms", "term_kinds", "allowed_iris", "unavailable_terms"}}
        page = matches[request.offset:request.offset + remaining]
        remaining -= len(page)
        next_offset = request.offset + len(page)
        unavailable = dataset.get("unavailable_terms") or []
        datasets.append({**item, "terms": page, "term_count": len(terms), "matched_term_count": len(matches),
            "offset": request.offset, "returned": len(page), "search": request.search,
            "next_offset": next_offset if next_offset < len(matches) else None,
            "unavailable_term_count": len(unavailable), "unavailable_terms_preview": unavailable[:5]})
    if request.dataset and not datasets:
        raise ValueError("Dataset is not present in this release query space")
    structured = result.get("structured") or {}
    return {**{key: value for key, value in result.items() if key not in {"datasets", "structured"}},
            "datasets": datasets, "structured": {key: structured[key] for key in ("status", "dataset", "reason") if key in structured},
            "metadata_pagination": "limit bounds total returned terms. Use dataset with search or its next_offset to fetch needed definitions; omitted terms are not unavailable."}


def _persist_result(store: EvidenceReceiptStore, runtime: Any, request: CheckedQueryRequest,
                    result: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    source_kind = "structured_db" if request.dataset == "structured" else "document"
    complete = not result["truncated"]
    warning = ("结果只代表所选已发布数据范围；查询已通过平台执行检查，尚未成为业务批准的 CQ。"
               "空结果不能证明业务事实不存在，也不能推断未建模的业务分类。")
    if not complete:
        warning += " 返回达到上限，不能将当前行数作为全量总数；请缩小范围或使用聚合查询。"
    # Use the same immutable receipt and pagination contract as named queries.
    # The new query remains separate from the published ontology/package.
    source = result.get("source") or {}
    query_result = {**result, "capability_name": "checked_query", "source_question_id": request.query_id,
                    "answer_scope_zh": warning, "row_count": len(result["rows"]),
                    "fact_sha256": source.get("fact_sha256"), "ontology_sha256": source.get("ontology_sha256"),
                    "source_refs": [source[key] for key in ("fact_artifact", "ontology_artifact", "deployment_id") if source.get(key)]}
    record = EvidenceRecord(evidence_id="EV-CHECKED-" + uuid4().hex[:16], source_kind=source_kind,
        source_ref=request.dataset, observed_at=now,
        payload={"cq_results": [query_result], "question": request.question, "submitted_sparql": request.sparql,
                 "semantic_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"})
    # Source snapshots already have immutable manifest hashes. Avoid copying
    # every source fact and test oracle into each new analysis receipt.
    receipt_release = runtime.binding.model_copy(update={"document_fact_queries": {}, "reasoning_capabilities": {},
                                                        "ontop_query_capabilities": {}})
    bundle = EvidenceBundle(query_id=request.query_id, mode="structured" if source_kind == "structured_db" else "documents",
        release=receipt_release, generated_at=now, complete=complete, evidence=[record],
        source_status={source_kind: SourceStatus(source_kind=source_kind,
            status="fresh" if result["rows"] else "empty", source_ref=request.dataset, queried_at=now,
            version=runtime.binding.release_version, details={"truncated": result["truncated"],
                "semantic_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"})})
    answer = RealtimeAnswer(question=request.question, answer_status="complete" if complete else "partial",
        complete=complete, answer=f"已执行受检查查询，返回 {len(result['rows'])} 行。{warning}",
        citations=[], warnings=[warning], evidence_bundle=bundle)
    response = persist_session_answer(store, RealtimeSessionAnswer(session_id=request.session_id,
        runtime_binding=runtime_binding(runtime), answer=answer))
    return {**result, **_identity(request), "query_id": request.query_id,
            "evidence_receipt": response.evidence_receipt, "scope_warning_zh": warning,
            "semantic_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED", "release_modified": False}


def register_checked_query_api(app: FastAPI, store: EvidenceReceiptStore, resolve_runtime: Any) -> None:
    @app.post("/ontology/realtime/session-query-space")
    def query_space(request: QuerySpaceRequest) -> dict[str, Any]:
        from services.realtime_qa.checked_query import discover_checked_query

        try:
            runtime = resolve_runtime(request.expected_release.project_id)
            verify_live_release(runtime.binding, request.expected_release)
            result = discover_checked_query(runtime)
            verify_live_release(runtime.binding, request.expected_release)
            return {**_query_space_page(result, request), **_identity(request)}
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/ontology/realtime/session-checked-query")
    def checked_query(request: CheckedQueryRequest) -> dict[str, Any]:
        from services.realtime_qa.checked_query import execute_checked_query

        try:
            runtime = resolve_runtime(request.expected_release.project_id)
            verify_live_release(runtime.binding, request.expected_release)
            result = execute_checked_query(runtime, request.sparql, request.dataset, limit=request.limit)
            # Do not issue a result/receipt for a release revoked during work.
            verify_live_release(runtime.binding, request.expected_release)
            return _persist_result(store, runtime, request, result)
        except TimeoutError as exc:
            raise HTTPException(408, "受检查查询超过执行预算；请缩小范围或简化查询。") from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
