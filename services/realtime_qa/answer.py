from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from services.realtime_qa.json_values import query_json_scalar
from services.realtime_qa.models import (
    EvidenceBundle,
    EvidenceCitation,
    EvidenceRecord,
    RealtimeAnswer,
    RealtimeAnswerRequest,
)
from services.realtime_qa.reasoning import RULE_CONCLUSION_BOUNDARY_ZH

WHITESPACE = re.compile(r"\s+")


class RealtimeAnswerService:
    """Render source-backed facts without allowing a model to invent conclusions."""

    def __init__(self, *, max_rows_per_source: int = 10) -> None:
        if max_rows_per_source < 1 or max_rows_per_source > 100:
            raise ValueError("max_rows_per_source must be between 1 and 100")
        self.max_rows_per_source = max_rows_per_source

    def answer(
        self,
        request: RealtimeAnswerRequest,
        bundle: EvidenceBundle,
    ) -> RealtimeAnswer:
        if request.evidence_request.query_id != bundle.query_id:
            raise ValueError("answer request and EvidenceBundle query_id do not match")

        citations, truncated = self._citations(bundle)
        if not citations or citations[0].source_kind != "ontology_release":
            raise ValueError("EvidenceBundle has no ontology release citation")
        # Result existence belongs to execution records. A business column named
        # row_count (including an aggregate zero) is not an empty-result marker.
        has_source_evidence = self._has_source_evidence(bundle)
        execution_warnings = self._execution_warnings(bundle)
        degraded = bool(bundle.degraded_sources) or not bundle.complete or bool(execution_warnings)
        if not has_source_evidence:
            answer_status = "no_evidence"
        elif degraded:
            answer_status = "partial"
        else:
            answer_status = "complete"

        warnings = [
            f"{source.source_kind}: {source.reason}"
            for source in bundle.degraded_sources
        ]
        warnings.extend(warning for warning in execution_warnings if warning not in warnings)
        if not bundle.complete and not bundle.degraded_sources and not execution_warnings:
            warnings.append("EvidenceBundle 标记为不完整，未提供具体降级原因。")
        if truncated:
            warnings.append(
                f"证据行超过展示上限，每个来源最多展示 {self.max_rows_per_source} 行。"
            )

        answer = self._render(
            request.question,
            bundle,
            citations,
            answer_status,
            warnings,
        )
        return RealtimeAnswer(
            question=request.question,
            answer_status=answer_status,
            complete=answer_status == "complete",
            answer=answer,
            citations=citations,
            warnings=warnings,
            evidence_bundle=bundle,
        )

    @staticmethod
    def _has_source_evidence(bundle: EvidenceBundle) -> bool:
        def has_rows(value: Any) -> bool:
            return isinstance(value, list) and any(isinstance(row, dict) for row in value)

        for record in bundle.evidence:
            payload = record.payload
            if RealtimeAnswerService._verified_empty_query(bundle, record):
                return True
            if record.source_kind in {"structured_db", "document"} and has_rows(payload.get("rows")):
                return True
            if record.source_kind in {"document", "reasoning"}:
                results = payload.get("cq_results")
                if isinstance(results, list) and any(
                    isinstance(result, dict) and has_rows(result.get("rows")) for result in results
                ):
                    return True
            if record.source_kind == "reasoning" and payload.get("result_facts"):
                return True
        return False

    @staticmethod
    def _verified_empty_query(bundle: EvidenceBundle, record: EvidenceRecord) -> bool:
        """Zero rows is evidence within a verified query's scope, not universal absence."""
        status = bundle.source_status.get("structured_db")
        payload = record.payload
        name = payload.get("query_template")
        return bool(
            record.source_kind == "structured_db"
            and name in bundle.release.ontop_query_names
            and record.source_ref == f"ontop:{name}"
            and payload.get("rows") == []
            and payload.get("row_count") == 0
            and status is not None and status.status == "empty"
            and status.source_ref == record.source_ref
            and status.version == bundle.release.release_version
            and status.queried_at is not None
            and status.details.get("artifact_verification") == "verified"
            and status.details.get("runtime_verification") == "VERIFIED"
        )

    @staticmethod
    def _execution_warnings(bundle: EvidenceBundle) -> list[str]:
        """Honor explicit execution incompleteness, never infer it from preview size.

        Inspect only reserved metadata positions, not row values. Business data may
        legitimately contain columns named truncated, complete or row_count.
        """
        warnings: list[str] = []
        for source in bundle.source_status.values():
            if source.status == "degraded":
                warnings.append(f"{source.source_kind}: {source.degraded_reason or '来源执行已标记为降级。'}")
            if source.details.get("truncated") is True:
                warnings.append(f"{source.source_ref}: 执行结果被截断，当前行数不能作为全量总数。")
        for record in bundle.evidence:
            if record.source_kind == "ontology_release":
                continue
            payload = record.payload
            results = payload.get("cq_results")
            metadata = [payload] + ([item for item in results if isinstance(item, dict)] if isinstance(results, list) else [])
            if any(item.get("truncated") is True for item in metadata):
                warnings.append(f"{record.source_ref}: 执行结果被截断，当前行数不能作为全量总数。")
        return list(dict.fromkeys(warnings))

    def _citations(
        self,
        bundle: EvidenceBundle,
    ) -> tuple[list[EvidenceCitation], bool]:
        citations: list[EvidenceCitation] = []
        truncated = False
        for record in bundle.evidence:
            if record.source_kind == "ontology_release":
                citations.append(
                    self._release_citation(record, len(citations) + 1)
                )
                continue
            if record.source_kind in {"document", "reasoning"}:
                for cq in record.payload.get("cq_results") or []:
                    if not isinstance(cq, dict) or not isinstance(cq.get("rows"), list):
                        continue
                    cq_rows = cq["rows"]
                    truncated = truncated or len(cq_rows) > self.max_rows_per_source
                    citations.append(EvidenceCitation(
                        citation_id=f"C{len(citations) + 1}",
                        source_kind=record.source_kind,
                        source_ref=record.source_ref,
                        observed_at=record.observed_at,
                        locator={
                            **{key: cq[key] for key in (
                                "source_question_id", "capability_name", "query_sha256",
                                "fact_sha256", "ontology_sha256", "source_refs", "parameters",
                            ) if key in cq},
                            "release_fingerprint": bundle.release.release_fingerprint,
                        },
                        facts={
                            "cq_row_count": len(cq_rows), "row_count": len(cq_rows),
                            "rows_preview": [self._bounded_facts(row) for row in cq_rows[:self.max_rows_per_source] if isinstance(row, dict)],
                            "full_results_location": "evidence_bundle.evidence.payload.cq_results",
                        },
                    ))
            if record.source_kind == "reasoning":
                citations.append(
                    EvidenceCitation(
                        citation_id=f"C{len(citations) + 1}",
                        source_kind="reasoning",
                        source_ref=record.source_ref,
                        observed_at=record.observed_at,
                        locator={
                            "capability_name": record.payload.get("capability_name"),
                            "engine": record.payload.get("engine"),
                            "rule_artifact": record.payload.get("rule_artifact"),
                            "rule_sha256": record.payload.get("rule_sha256"),
                            "release_fingerprint": bundle.release.release_fingerprint,
                        },
                        facts={
                            "execution_scope": record.payload.get("execution_scope"),
                            "conclusion_contract": record.payload.get("conclusion_contract"),
                            "input_facts_sha256": record.payload.get(
                                "input_facts_sha256"
                            ),
                            "rules": record.payload.get("rules") or [],
                            "rules_fired": record.payload.get("rules_fired", 0),
                            "result_fact_count": len(
                                record.payload.get("result_facts") or []
                            ),
                            "result_facts": record.payload.get("result_facts") or [],
                            "semantic_result_facts": record.payload.get(
                                "semantic_result_facts"
                            )
                            or [],
                            "trace": record.payload.get("trace") or [],
                            "decision_record": record.payload.get("decision_record"),
                        },
                    )
                )
                continue
            rows = record.payload.get("rows")
            if not isinstance(rows, list):
                continue
            if not rows:
                citation_id = f"C{len(citations) + 1}"
                if record.source_kind == "structured_db":
                    citations.append(
                        EvidenceCitation(
                            citation_id=citation_id,
                            source_kind="structured_db",
                            source_ref=record.source_ref,
                            observed_at=record.observed_at,
                            locator={
                                "query_template": record.payload.get("query_template"),
                                "parameters": record.payload.get("parameters") or {},
                            },
                            facts={"row_count": 0},
                        )
                    )
                elif record.source_kind == "document":
                    citations.append(
                        EvidenceCitation(
                            citation_id=citation_id,
                            source_kind="document",
                            source_ref=record.source_ref,
                            observed_at=record.observed_at,
                            locator={
                                "entity_iri": record.payload.get("entity_iri"),
                            },
                            facts={"row_count": 0},
                        )
                    )
                continue
            if len(rows) > self.max_rows_per_source:
                truncated = True
            for row_index, row in enumerate(rows[: self.max_rows_per_source]):
                if not isinstance(row, dict):
                    continue
                citation_id = f"C{len(citations) + 1}"
                if record.source_kind == "structured_db":
                    locator = {
                        "query_template": record.payload.get("query_template"),
                        "parameters": record.payload.get("parameters") or {},
                        "row_index": row_index,
                    }
                    if record.payload.get("evidence_role"):
                        locator["evidence_role"] = record.payload["evidence_role"]
                    dataset_id = row.get("dataset_id") or row.get("_orion_dataset_id")
                    source_row = row.get("source_row") or row.get("_orion_source_row")
                    if dataset_id is not None:
                        locator["dataset_id"] = dataset_id
                    if source_row is not None:
                        locator["source_row"] = source_row
                    citations.append(
                        EvidenceCitation(
                            citation_id=citation_id,
                            source_kind="structured_db",
                            source_ref=record.source_ref,
                            observed_at=record.observed_at,
                            locator=locator,
                            facts=self._bounded_facts(row),
                        )
                    )
                elif record.source_kind == "document":
                    citations.append(
                        EvidenceCitation(
                            citation_id=citation_id,
                            source_kind="document",
                            source_ref=record.source_ref,
                            observed_at=record.observed_at,
                            locator={
                                key: row.get(key)
                                for key in (
                                    "graph",
                                    "document_id",
                                    "version",
                                    "page",
                                    "locator",
                                    "source_uri",
                                    "sha256",
                                )
                                if row.get(key) is not None
                            },
                            facts=self._document_facts(row),
                        )
                    )
        return citations, truncated

    @staticmethod
    def _release_citation(record: EvidenceRecord, number: int) -> EvidenceCitation:
        payload = record.payload
        return EvidenceCitation(
            citation_id=f"C{number}",
            source_kind="ontology_release",
            source_ref=record.source_ref,
            observed_at=record.observed_at,
            locator={
                "ontology_artifact": payload.get("ontology_artifact"),
                "mapping_artifact": payload.get("mapping_artifact"),
            },
            facts={
                "release_fingerprint": payload.get("release_fingerprint"),
                "artifact_verification": payload.get("artifact_verification"),
                "runtime_verification": payload.get("ontop_runtime_verification"),
            },
        )

    @classmethod
    def _document_facts(cls, row: dict[str, Any]) -> dict[str, Any]:
        excerpt = row.get("page_text") or row.get("text") or ""
        facts = {
            key: row.get(key)
            for key in ("title", "document_type", "processed_at", "confidence")
            if row.get(key) is not None
        }
        if excerpt:
            facts["excerpt"] = cls._bounded_text(excerpt, 500)
        return facts

    @classmethod
    def _bounded_facts(cls, row: dict[str, Any]) -> dict[str, Any]:
        bounded: dict[str, Any] = {}
        for key in sorted(row)[:20]:
            value = row[key]
            if isinstance(value, str):
                bounded[str(key)] = cls._bounded_text(value, 500)
            elif value is None or isinstance(value, bool | int | float):
                bounded[str(key)] = value
            elif isinstance(value, date | Decimal):
                bounded[str(key)] = cls._bounded_text(query_json_scalar(value), 500)
            else:
                bounded[str(key)] = cls._bounded_text(
                    json.dumps(value, ensure_ascii=False, sort_keys=True, default=query_json_scalar, allow_nan=False),
                    500,
                )
        return bounded

    @classmethod
    def _render(
        cls,
        question: str,
        bundle: EvidenceBundle,
        citations: list[EvidenceCitation],
        answer_status: str,
        warnings: list[str],
    ) -> str:
        if answer_status == "complete":
            heading = "已完成全部已请求数据源的证据回读。"
        elif answer_status == "partial":
            heading = "只能给出部分回答：已请求的证据存在执行降级、不完整或截断，不能形成完整结论。"
        else:
            heading = "当前没有可用于回答该问题的数据行或文档证据，不能形成结论。"

        lines = [
            heading,
            f"问题：{cls._bounded_text(question, 1000)}",
            (
                "本体版本："
                f"{bundle.release.project_id}@{bundle.release.release_version} "
                f"({bundle.release.release_fingerprint}) [C1]"
            ),
        ]
        if any(record.source_kind == "reasoning" for record in bundle.evidence):
            # Keep this before large row arrays so bounded model summaries retain it.
            lines.append("推理结论边界：" + RULE_CONCLUSION_BOUNDARY_ZH)
        for name, mode in bundle.query_modes.items():
            if mode == "SNAPSHOT_ONLY":
                lines.append(f"数据时效：{name} 的发布合同为 SNAPSHOT_ONLY（快照查询）；"
                             "本次执行成功或 fresh 状态不代表上游实时，采集后的来源变化未核验。")
            elif mode == "HYBRID":
                lines.append(f"数据时效：{name} 声明 HYBRID；本次 Ontop 执行没有上游实时补查回执，"
                             "不能据此声称来源当前最新。")
        if bundle.query_modes and not bundle.release.snapshot_set_id:
            lines.append("来源限制：发布 runtime 未冻结完整快照来源合同，无法证明采集时间与上游新鲜度。")
            trace = bundle.release.source_trace
            if trace.get("snapshot_set_id"):
                lines.append(f"包内受保护 S1 追溯：{trace['snapshot_set_id']}；仅为追溯身份，不等同运行快照绑定。")
        structured = [
            citation for citation in citations if citation.source_kind == "structured_db"
        ]
        documents = [
            citation for citation in citations if citation.source_kind == "document"
        ]
        reasoning = [
            citation for citation in citations if citation.source_kind == "reasoning"
        ]
        if structured:
            lines.append("数据库查询事实：")
            lines.extend(
                f"- [{citation.citation_id}] {cls._facts_text(citation.facts)}"
                for citation in structured
            )
            if answer_status == "complete" and any(cls._verified_empty_query(bundle, record) for record in bundle.evidence):
                lines.append("空结果说明：已验证查询成功完成，在本次发布版本、来源范围和筛选条件内没有匹配记录；"
                             "不能外推为所有来源均不存在，也不代表上游实时状态。")
        elif bundle.source_status["structured_db"].status != "not_requested":
            lines.append(
                "数据库查询事实："
                f"{bundle.source_status['structured_db'].status}，没有可展示的数据行。"
            )
        if documents:
            lines.append("文档证据：")
            lines.extend(
                f"- [{citation.citation_id}] {cls._facts_text(citation.facts)}"
                for citation in documents
            )
        elif bundle.source_status["document"].status != "not_requested":
            lines.append(
                "文档证据："
                f"{bundle.source_status['document'].status}，没有可展示的 current 版本证据。"
            )
        if reasoning:
            lines.append("规则推理结论：")
            lines.extend(
                f"- [{citation.citation_id}] {cls._facts_text(citation.facts)}"
                for citation in reasoning
            )
        elif (
            bundle.source_status.get("reasoning") is not None
            and bundle.source_status["reasoning"].status != "not_requested"
        ):
            lines.append(
                "规则推理结论："
                f"{bundle.source_status['reasoning'].status}，没有推出目标业务事实。"
            )
        if warnings:
            lines.append("降级说明：")
            lines.extend(f"- {cls._bounded_text(warning, 500)}" for warning in warnings)
        lines.append(
            "结论边界：数据库与文档内容为来源事实；规则推理内容仅来自当前发布版本"
            "保护的规则包，并保留规则、前提、结论和置信度轨迹。"
        )
        return "\n".join(lines)

    @classmethod
    def _facts_text(cls, facts: dict[str, Any]) -> str:
        return cls._bounded_text(
            json.dumps(facts, ensure_ascii=False, sort_keys=True, default=query_json_scalar, allow_nan=False),
            1000,
        )

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        normalized = WHITESPACE.sub(" ", str(value)).strip()
        return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
