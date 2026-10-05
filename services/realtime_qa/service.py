from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import psycopg

from services.fuseki_client.client import FusekiClient
from services.ontop_client.client import OntopClient
from services.realtime_qa.document_cq import execute_document_cqs
from services.realtime_qa.incremental_pipeline import DocumentCurrentStore
from services.realtime_qa.models import (
    DegradedSource,
    EvidenceBundle,
    EvidenceRecord,
    OntologyReleaseBinding,
    RealtimeEvidenceRequest,
    SourceEvidenceV2,
    SourceStatus,
)
from services.realtime_qa.reasoning import (
    SemanticaReasoningClient,
    build_decision_record,
    document_evidence_facts,
    enrich_reasoning_trace,
    facts_from_rows,
    reasoning_input_sha256,
    result_facts,
    rule_conclusion_contract,
    semantic_facts,
)
from services.realtime_qa.reasoning_cq import execute_reasoning_cqs

SOURCE_ERRORS = (
    httpx.HTTPError,
    psycopg.Error,
    OSError,
    ValueError,
    KeyError,
    RuntimeError,
)


class HybridRealtimeEvidenceService:
    """Collect source-backed facts; answer generation remains a downstream concern."""

    def __init__(
        self,
        binding: OntologyReleaseBinding,
        ontop: OntopClient,
        fuseki: FusekiClient,
        current_registry: DocumentCurrentStore,
        semantica: SemanticaReasoningClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding = binding
        self.ontop = ontop
        self.fuseki = fuseki
        self.current_registry = current_registry
        self.semantica = semantica
        self.now = now or (lambda: datetime.now(UTC))
        if not binding.ontop_query_names.issubset(ontop.allowed_queries):
            raise ValueError("Ontop client does not allow every release-bound query")
        if ontop.bound_release_fingerprint != binding.release_fingerprint:
            raise ValueError("Ontop client is not bound to this verified release")

    def collect(self, request: RealtimeEvidenceRequest) -> EvidenceBundle:
        if request.project_id is not None and request.project_id != self.binding.project_id:
            raise ValueError("evidence request project_id does not match the runtime release")
        if (
            request.document_query is not None
            and "current_full_text_search" not in self.binding.document_query_capabilities
        ):
            raise ValueError("current full-text document search is not allowed by this release")
        if (
            request.entity_iris
            and "reviewed_entity_evidence" not in self.binding.document_query_capabilities
        ):
            raise ValueError("entity-linked document evidence is not allowed by this release")
        if (
            request.reasoning_query is not None
            and request.reasoning_query.name not in self.binding.reasoning_capabilities
        ):
            raise ValueError(
                f"reasoning capability is not bound to this release: {request.reasoning_query.name}"
            )
        generated_at = self.now()
        query_names = []
        if request.structured_query:
            query_names.append(request.structured_query.name)
        if request.reasoning_query:
            query_names.append(self.binding.reasoning_capabilities[request.reasoning_query.name].get("evidence_query"))
        query_modes = {name: self.binding.ontop_query_capabilities.get(name, {}).get("query_mode", "UNKNOWN")
                       for name in query_names if name in self.binding.ontop_query_names}
        if "REALTIME_REQUIRED" in query_modes.values():
            raise ValueError("REALTIME_REQUIRED capability requires SourceQueryGateway; cannot execute it as a snapshot")
        if request.execution_mode == "REALTIME_REQUIRED":
            raise ValueError(
                "REALTIME_SOURCE_QUERY_V1 requires the controlled SourceQueryGateway; "
                "this Ontop evidence service is snapshot-bound"
            )
        evidence = [self._release_evidence(generated_at)]
        degraded: list[DegradedSource] = []
        source_status = {
            "structured_db": SourceStatus(
                source_kind="structured_db",
                status="not_requested",
                source_ref="ontop",
                queried_at=None,
                version=self.binding.release_version,
                details={
                    "artifact_verification": self.binding.integrity_status,
                    "runtime_verification": self.ontop.runtime_verification_status,
                },
            ),
            "document": SourceStatus(
                source_kind="document",
                status="not_requested",
                source_ref="fuseki:versioned-documents",
                queried_at=None,
                version=None,
            ),
            "reasoning": SourceStatus(
                source_kind="reasoning",
                status="not_requested",
                source_ref="semantica:forward",
                queried_at=None,
                version=self.binding.release_version,
            ),
        }

        if request.structured_query is not None:
            query = request.structured_query
            if query.name not in self.binding.ontop_query_names:
                raise ValueError(f"query is not bound to this release: {query.name}")
            try:
                if self.ontop.runtime_verification_status != "VERIFIED":
                    raise RuntimeError("Ontop deployment is NOT_RUNTIME_VERIFIED for this release")
                select_with_metadata = getattr(self.ontop, "select_with_metadata", None)
                result = (select_with_metadata(query.name, **query.parameters)
                          if callable(select_with_metadata)
                          else {"rows": self.ontop.select(query.name, **query.parameters)})
                rows = result["rows"]
                evidence.append(
                    self._evidence(
                        "structured_db",
                        f"ontop:{query.name}",
                        generated_at,
                        [],
                        {
                            "query_template": query.name,
                            "parameters": query.parameters,
                            "row_count": len(rows),
                            "rows": rows,
                            **{key: result[key] for key in ("variables", "row_terms") if key in result},
                        },
                    )
                )
                source_status["structured_db"] = SourceStatus(
                    source_kind="structured_db",
                    status="fresh" if rows else "empty",
                    source_ref=f"ontop:{query.name}",
                    queried_at=self.now(),
                    version=self.binding.release_version,
                    details={
                        "row_count": len(rows),
                        "query_template": query.name,
                        "artifact_verification": self.binding.integrity_status,
                        "runtime_verification": self.ontop.runtime_verification_status,
                    },
                )
            except SOURCE_ERRORS as exc:
                degraded.append(
                    DegradedSource(
                        source_kind="structured_db",
                        source_ref=f"ontop:{query.name}",
                        reason=str(exc),
                    )
                )
                source_status["structured_db"] = SourceStatus(
                    source_kind="structured_db",
                    status="degraded",
                    source_ref=f"ontop:{query.name}",
                    queried_at=self.now(),
                    version=self.binding.release_version,
                    degraded_reason=str(exc),
                    details={
                        "artifact_verification": self.binding.integrity_status,
                        "runtime_verification": self.ontop.runtime_verification_status,
                    },
                )

        if request.document_fact_query is not None:
            query = request.document_fact_query
            capability = self.binding.document_fact_queries.get(query.name, {})
            if not capability.get("cq_bindings"):
                raise ValueError(f"document fact CQ is not bound to this release: {query.name}")
            source_ref = f"document-fact:{query.name}"
            try:
                cq_results = execute_document_cqs(self.binding, query.name, parameters=query.parameters)
                row_count = sum(len(item.get("rows") or []) for item in cq_results)
                evidence.append(self._evidence("document", source_ref, generated_at, [], {
                    "query_name": query.name, "parameters": query.parameters,
                    "cq_results": cq_results, "row_count": row_count,
                    "release_fingerprint": self.binding.release_fingerprint,
                }))
                source_status["document_facts"] = SourceStatus(
                    source_kind="document", status="fresh" if row_count else "empty",
                    source_ref=source_ref, queried_at=self.now(), version=self.binding.release_version,
                    details={"query_name": query.name, "row_count": row_count,
                             "artifact_verification": self.binding.integrity_status},
                )
            except SOURCE_ERRORS as exc:
                degraded.append(DegradedSource(source_kind="document", source_ref=source_ref, reason=str(exc)))
                source_status["document_facts"] = SourceStatus(
                    source_kind="document", status="degraded", source_ref=source_ref,
                    queried_at=self.now(), version=self.binding.release_version, degraded_reason=str(exc),
                )

        if request.reasoning_query is not None:
            self._collect_reasoning(
                request,
                generated_at=generated_at,
                evidence=evidence,
                degraded=degraded,
                source_status=source_status,
            )

        document_versions: set[str] = set()
        document_row_count = 0
        document_had_error = False
        document_details: list[dict[str, Any]] = []
        for entity_iri in request.entity_iris:
            try:
                rows = self._current_document_rows(
                    self.fuseki.documents_for_entity(entity_iri),
                )
                evidence.append(
                    self._evidence(
                        "document",
                        "fuseki:versioned-documents",
                        generated_at,
                        [entity_iri],
                        {
                            "entity_iri": entity_iri,
                            "row_count": len(rows),
                            "rows": rows,
                        },
                    )
                )
                document_row_count += len(rows)
                document_versions.update(
                    str(row["version"]) for row in rows if row.get("version") is not None
                )
                document_details.append(
                    {
                        "entity_iri": entity_iri,
                        "status": "fresh" if rows else "empty",
                        "row_count": len(rows),
                    }
                )
            except SOURCE_ERRORS as exc:
                document_had_error = True
                degraded.append(
                    DegradedSource(
                        source_kind="document",
                        source_ref=f"fuseki:{entity_iri}",
                        reason=str(exc),
                    )
                )
                document_details.append(
                    {
                        "entity_iri": entity_iri,
                        "status": "degraded",
                        "degraded_reason": str(exc),
                    }
                )
        if request.document_query is not None:
            query = request.document_query
            try:
                rows = self._current_document_rows(
                    self.fuseki.search_documents(
                        self.binding.project_id,
                        query.text,
                        document_ids=query.document_ids,
                        limit=query.limit,
                    )
                )[: query.limit]
                evidence.append(
                    self._evidence(
                        "document",
                        "fuseki:current-document-search",
                        generated_at,
                        [],
                        {
                            "document_query": query.model_dump(),
                            "row_count": len(rows),
                            "rows": rows,
                        },
                    )
                )
                document_row_count += len(rows)
                document_versions.update(
                    str(row["version"]) for row in rows if row.get("version") is not None
                )
                document_details.append(
                    {
                        "query": query.text,
                        "status": "fresh" if rows else "empty",
                        "row_count": len(rows),
                    }
                )
            except SOURCE_ERRORS as exc:
                document_had_error = True
                degraded.append(
                    DegradedSource(
                        source_kind="document",
                        source_ref="fuseki:current-document-search",
                        reason=str(exc),
                    )
                )
                document_details.append(
                    {
                        "query": query.text,
                        "status": "degraded",
                        "degraded_reason": str(exc),
                    }
                )
        if request.entity_iris or request.document_query is not None:
            reasons = [
                str(item["degraded_reason"])
                for item in document_details
                if item["status"] == "degraded"
            ]
            source_status["document"] = SourceStatus(
                source_kind="document",
                status=(
                    "degraded"
                    if document_had_error
                    else ("fresh" if document_row_count else "empty")
                ),
                source_ref="fuseki:versioned-documents",
                queried_at=self.now(),
                version=",".join(sorted(document_versions)) or None,
                degraded_reason="; ".join(reasons) or None,
                details={
                    "row_count": document_row_count,
                    "entities": document_details,
                },
            )

        source_status_by_id, source_evidence_v2 = self._source_evidence_v2(
            request=request,
            source_status=source_status,
            generated_at=generated_at,
            evidence=evidence,
        )
        if query_modes:
            source_status["structured_db"].details.update(
                query_modes=query_modes, freshness_scope="QUERY_EXECUTION_ONLY", upstream_freshness="UNKNOWN",
            )
            for record in evidence:
                name = record.payload.get("query_template")
                if record.source_kind == "structured_db" and name in query_modes:
                    record.payload.update(query_mode=query_modes[name], upstream_freshness="UNKNOWN")
        return EvidenceBundle(
            query_id=request.query_id,
            mode=self._mode(request),
            release=self.binding,
            generated_at=generated_at,
            complete=not degraded,
            evidence=evidence,
            degraded_sources=degraded,
            source_status=source_status,
            snapshot_set_id=self.binding.snapshot_set_id,
            source_status_by_id=source_status_by_id,
            source_evidence_v2=source_evidence_v2,
            query_modes=query_modes,
        )

    def _source_evidence_v2(
        self,
        *,
        request: RealtimeEvidenceRequest,
        source_status: dict[str, SourceStatus],
        generated_at: datetime,
        evidence: list[EvidenceRecord],
    ) -> tuple[dict[str, dict[str, Any]], list[SourceEvidenceV2]]:
        query_name: str | None = None
        if request.structured_query is not None:
            query_name = request.structured_query.name
        elif request.reasoning_query is not None:
            reasoning_capability = self.binding.reasoning_capabilities.get(
                request.reasoning_query.name, {}
            )
            query_name = str(reasoning_capability.get("evidence_query") or "") or None
        if query_name is None:
            return {}, []
        capability = self.binding.ontop_query_capabilities.get(query_name, {})
        source_ids = list(capability.get("source_ids") or [])
        if not source_ids:
            return {}, []
        status = source_status["structured_db"]
        structured_payload = next(
            (
                item.payload
                for item in evidence
                if item.source_kind == "structured_db" and item.source_ref == f"ontop:{query_name}"
            ),
            {},
        )
        reasoning_payload = next(
            (item.payload for item in reversed(evidence) if item.source_kind == "reasoning"),
            {},
        )
        query_parameters = (
            request.structured_query.parameters
            if request.structured_query is not None
            else request.reasoning_query.parameters
            if request.reasoning_query is not None
            else {}
        )
        by_id: dict[str, dict[str, Any]] = {}
        records: list[SourceEvidenceV2] = []
        for source_id in source_ids:
            binding = self.binding.source_bindings.get(source_id) or {}
            snapshot = self.binding.snapshot_manifests.get(source_id) or {}
            trace_source = (self.binding.source_trace.get("sources") or {}).get(source_id) or {}
            complete = bool(snapshot.get("snapshot_complete"))
            source_state = (
                status.status if complete and self.binding.snapshot_set_id else "degraded"
            )
            reason = status.degraded_reason
            if not complete or not self.binding.snapshot_set_id:
                reason = "INPUT_INCOMPLETE: source snapshot is not release-bound"
            by_id[source_id] = {
                "status": source_state,
                "snapshot_complete": complete,
                "dataset_id": snapshot.get("dataset_id"),
                "snapshot_version": snapshot.get("snapshot_version"),
                "source_sha256": snapshot.get("source_sha256"),
                "reason": reason,
            }
            source_tables = list(
                (capability.get("source_tables_by_id") or {}).get(source_id)
                or (capability.get("source_tables") if len(source_ids) == 1 else [])
                or []
            )
            source_columns = list(
                (capability.get("source_columns_by_id") or {}).get(source_id)
                or (capability.get("source_columns") if len(source_ids) == 1 else [])
                or []
            )
            # An authorization allowlist is not evidence of this query's
            # dependencies; aggregate multi-source names cannot assign an owner.
            file_tables = [t for t in trace_source.get("tables", [])
                           if t.get("source_table") in source_tables and t.get("file_import")]
            dataset_ids = ([snapshot["dataset_id"]] if snapshot.get("dataset_id") else
                           sorted({t["dataset_id"] for t in file_tables}) if file_tables else trace_source.get("dataset_ids") or [])
            table_manifests = list(snapshot.get("tables") or [])
            locators = {str(item['source_locator']) for item in table_manifests
                        if item.get('source_locator') and item.get('table') in source_tables}
            locator = (next(iter(locators)) if len(locators) == 1
                       else f"snapshot:{source_id}:{snapshot.get('snapshot_version') or 'UNKNOWN'}")
            derived_facts = list(reasoning_payload.get("semantic_result_facts") or [])
            trace = list(reasoning_payload.get("trace") or [])
            rule_ids = [
                str(item.get("rule_id"))
                for item in reasoning_payload.get("rules") or []
                if item.get("rule_id")
            ]
            records.append(
                SourceEvidenceV2(
                    source_id=source_id,
                    engine=str(binding.get("engine") or ("STRUCTURED_FILE" if file_tables else "UNKNOWN")),
                    database=str(binding.get("database") or trace_source.get("database") or "UNKNOWN"),
                    schema_name=str((binding.get("schemas") or [""])[0]) or None,
                    tables=source_tables,
                    columns=source_columns,
                    dataset_id=dataset_ids[0] if len(dataset_ids) == 1 else None,
                    dataset_ids=dataset_ids,
                    snapshot_version=snapshot.get("snapshot_version"),
                    source_sha256=snapshot.get("source_sha256"),
                    observed_at=snapshot.get("captured_at"),
                    queried_at=status.queried_at,
                    query_mode=capability.get("query_mode", "UNKNOWN"),
                    provenance_scope=("RELEASE_SNAPSHOT_CONTRACT" if complete and self.binding.snapshot_set_id
                                      else "PROTECTED_S1_TRACE_ONLY" if trace_source else "UNAVAILABLE"),
                    # Query success says nothing about changes in the upstream
                    # source since capture. No live source receipt exists here.
                    freshness="UNKNOWN",
                    locator=locator,
                    pii_scope=str(binding.get("pii_scope") or "UNDECLARED"),
                    masking=str(
                        binding.get("masking")
                        or (
                            "STABLE_HASH"
                            if "HASH" in str(binding.get("pii_scope") or "").upper()
                            else "POLICY_DECLARED"
                        )
                    ),
                    facts=list(structured_payload.get("rows") or []),
                    derived_facts=derived_facts,
                    rule_id=",".join(rule_ids) or None,
                    rule_version=(
                        str(reasoning_payload.get("rule_sha256"))
                        if reasoning_payload.get("rule_sha256")
                        else None
                    ),
                    trace=[
                        {
                            "query_template": query_name,
                            "parameters": query_parameters,
                            "status": source_state,
                        },
                        *trace,
                    ],
                    snapshot_set_id=self.binding.snapshot_set_id,
                    release_fingerprint=self.binding.release_fingerprint,
                )
            )
        return by_id, records

    def _release_evidence(self, observed_at: datetime) -> EvidenceRecord:
        return self._evidence(
            "ontology_release",
            f"orion:{self.binding.project_id}@{self.binding.release_version}",
            observed_at,
            [],
            {
                "release_fingerprint": self.binding.release_fingerprint,
                "ontology_iri": self.binding.ontology_iri,
                "ontology_artifact": self.binding.ontology_artifact,
                "mapping_artifact": self.binding.mapping_artifact,
                "artifact_verification": self.binding.integrity_status,
                "ontop_runtime_verification": self.ontop.runtime_verification_status,
                "reasoning_capabilities": sorted(self.binding.reasoning_capabilities),
            },
        )

    @staticmethod
    def _mode(request: RealtimeEvidenceRequest) -> str:
        document_requested = (bool(request.entity_iris) or request.document_query is not None
                              or request.document_fact_query is not None)
        requested = sum(
            (
                request.structured_query is not None,
                request.reasoning_query is not None,
                document_requested,
            )
        )
        if requested > 1:
            return "hybrid"
        if request.reasoning_query is not None:
            return "reasoning"
        if request.structured_query is not None:
            return "structured"
        return "documents"

    def _collect_reasoning(
        self,
        request: RealtimeEvidenceRequest,
        *,
        generated_at: datetime,
        evidence: list[EvidenceRecord],
        degraded: list[DegradedSource],
        source_status: dict[str, SourceStatus],
    ) -> None:
        query = request.reasoning_query
        if query is None:
            return
        capability = self.binding.reasoning_capabilities[query.name]
        evidence_query = str(capability["evidence_query"])
        source_ref = f"semantica:{query.name}"
        document_evidence = evidence_query in self.binding.document_fact_queries
        try:
            if self.semantica is None:
                raise RuntimeError("Semantica reasoning runtime is not configured")
            if document_evidence:
                fact_query = self.binding.document_fact_queries[evidence_query]
                # DOCUMENT_ONLY reasoning: facts come from the materialized,
                # provenance-bound document evidence fact layer — never Ontop.
                symbol_table: dict[str, str] = {}
                facts = document_evidence_facts(
                    list(fact_query["facts"]),
                    list(capability["fact_bindings"]),
                    symbol_table,
                    parameters=query.parameters,
                )
                evidence.append(
                    self._evidence(
                        "document",
                        f"document-fact:{evidence_query}",
                        generated_at,
                        [],
                        {
                            "fact_source": fact_query["fact_source"],
                            "fact_count": len(facts),
                            "evidence_role": "RULE_PREMISES_NOT_CONCLUSIONS",
                            "reasoning_capability": query.name,
                            "evidence_query": evidence_query,
                        },
                    )
                )
                source_status["document_facts"] = SourceStatus(
                    source_kind="document",
                    status="fresh" if facts else "empty",
                    source_ref=f"document-fact:{evidence_query}",
                    queried_at=self.now(),
                    version=self.binding.release_version,
                    details={
                        "fact_count": len(facts),
                        "fact_source": fact_query["fact_source"],
                        "reasoning_capability": query.name,
                        "artifact_verification": self.binding.integrity_status,
                    },
                )
            else:
                if self.ontop.runtime_verification_status != "VERIFIED":
                    raise RuntimeError("Ontop deployment is NOT_RUNTIME_VERIFIED for this release")
                rows = self.ontop.select(evidence_query, **query.parameters)
                evidence.append(
                    self._evidence(
                        "structured_db",
                        f"ontop:{evidence_query}",
                        generated_at,
                        [],
                        {
                            "query_template": evidence_query,
                            "parameters": query.parameters,
                            "reasoning_capability": query.name,
                            "row_count": len(rows),
                            "evidence_role": "RULE_PREMISES_NOT_CONCLUSIONS",
                            "rows": rows,
                        },
                    )
                )
                source_status["structured_db"] = SourceStatus(
                    source_kind="structured_db",
                    status="fresh" if rows else "empty",
                    source_ref=f"ontop:{evidence_query}",
                    queried_at=self.now(),
                    version=self.binding.release_version,
                    details={
                        "row_count": len(rows),
                        "query_template": evidence_query,
                        "reasoning_capability": query.name,
                        "artifact_verification": self.binding.integrity_status,
                        "runtime_verification": self.ontop.runtime_verification_status,
                    },
                )
            if document_evidence:
                # DOCUMENT_ONLY: facts are already bound from the provenance-bound
                # document evidence fact layer above; no Ontop row conversion.
                pass
            else:
                symbol_table: dict[str, str] = {}
                facts = facts_from_rows(
                    rows,
                    query.parameters,
                    list(capability["fact_bindings"]),
                    symbol_table,
                )
            rule_package = self.binding.reasoning_rule_packages[query.name]
            formal_rules = list(rule_package["rules"])
            closed_world_predicates = [
                str(item["predicate"]) for item in capability.get("closed_world_inputs") or []
            ]
            result = self.semantica.run_forward(
                facts=facts,
                rules=[rule["expression"] for rule in formal_rules],
                **(
                    {"closed_world_predicates": closed_world_predicates}
                    if closed_world_predicates
                    else {}
                ),
            )
            inferred_facts = [str(value) for value in result["inferred_facts"]]
            expected_results = result_facts(
                inferred_facts,
                list(capability["result_predicates"]),
            )
            trace = enrich_reasoning_trace(list(result["trace"]), formal_rules)
            semantic_results = semantic_facts(
                expected_results,
                dict(capability["ontology_terms"]),
                symbol_table,
            )
            input_facts_sha256 = reasoning_input_sha256(
                facts=facts,
                rules=formal_rules,
            )
            decision_record = build_decision_record(
                query_id=request.query_id,
                capability_name=query.name,
                project_id=self.binding.project_id,
                release_version=self.binding.release_version,
                release_fingerprint=self.binding.release_fingerprint,
                evidence_query=evidence_query,
                parameters=query.parameters,
                input_facts_sha256=input_facts_sha256,
                rule_sha256=capability["rule_sha256"],
                semantic_result_facts=semantic_results,
                trace=trace,
            )
            closed_world_evidence = None
            if closed_world_predicates:
                required_set_source = capability.get("required_set_source")
                submitted_sources = list(capability.get("closed_world_inputs") or [])
                closed_world_result = dict(result.get("closed_world") or {})
                closed_world_evidence = {
                    **closed_world_result,
                    "capability": "CLOSED_WORLD_SET_DIFFERENCE_V1",
                    "dataset_type": "PRODUCTION_EVIDENCE",
                    "production_evidence": all(
                        item.get("dataset_type") == "PRODUCTION_EVIDENCE"
                        and item.get("production_evidence") is True
                        for item in [required_set_source, *submitted_sources]
                        if isinstance(item, dict)
                    ),
                    "required_set_source": required_set_source,
                    "submitted_set_sources": submitted_sources,
                    "input_fact_count": len(facts),
                    "output_fact_count": len(expected_results),
                    "rule_ids": [rule["rule_id"] for rule in formal_rules],
                    "rule_sha256": capability["rule_sha256"],
                    "mapping_sha256": (
                        self.binding.artifact_checksums.get(self.binding.mapping_artifact or "")
                    ),
                    "release_version": self.binding.release_version,
                    "release_fingerprint": self.binding.release_fingerprint,
                    "executor_version": "ORION_SAFE_ANTI_JOIN_V1",
                    "trace": trace,
                }
            evidence.append(
                self._evidence(
                    "reasoning",
                    source_ref,
                    generated_at,
                    [],
                    {
                        "engine": capability["engine"],
                        "read_only": True,
                        "capability_name": query.name,
                        "description_zh": capability["description_zh"],
                        "execution_scope": capability["execution_scope"],
                        "evidence_query": evidence_query,
                        "rule_artifact": capability["rule_artifact"],
                        "rule_sha256": capability["rule_sha256"],
                        "rules": [
                            {
                                "rule_id": rule["rule_id"],
                                "description_zh": rule["description_zh"],
                                "expression": rule["expression"],
                                "confidence": rule["confidence"],
                            }
                            for rule in formal_rules
                        ],
                        "input_fact_count": len(facts),
                        "input_facts": facts,
                        "input_facts_sha256": input_facts_sha256,
                        "derived_fact_count": len(inferred_facts),
                        "derived_facts": inferred_facts,
                        "result_facts": expected_results,
                        "semantic_result_facts": semantic_results,
                        "conclusion_contract": rule_conclusion_contract(closed_world_predicates),
                        "argument_bindings": symbol_table,
                        "ontology_terms": capability["ontology_terms"],
                        "cq_results": execute_reasoning_cqs(
                            self.binding, query.name, facts, inferred_facts, symbol_table,
                            parameters=query.parameters,
                        ),
                        "document_fact_provenance": [
                            {"fact": item["fact"], "provenance": item["provenance"]}
                            for item in fact_query["facts"]
                        ] if document_evidence and capability.get("cq_bindings") else [],
                        "decision_record": decision_record,
                        "rules_fired": int(result.get("rules_fired") or 0),
                        "trace": trace,
                        "warnings": list(result.get("warnings") or []),
                        "closed_world_set_difference": closed_world_evidence,
                    },
                )
            )
            source_status["reasoning"] = SourceStatus(
                source_kind="reasoning",
                status="fresh" if expected_results else "empty",
                source_ref=source_ref,
                queried_at=self.now(),
                version=self.binding.release_version,
                details={
                    "engine": capability["engine"],
                    "read_only": True,
                    "execution_scope": capability["execution_scope"],
                    "input_fact_count": len(facts),
                    "derived_fact_count": len(inferred_facts),
                    "result_fact_count": len(expected_results),
                    "rules_fired": int(result.get("rules_fired") or 0),
                    "rule_sha256": capability["rule_sha256"],
                },
            )
        except SOURCE_ERRORS as exc:
            degraded.append(
                DegradedSource(
                    source_kind="reasoning",
                    source_ref=source_ref,
                    reason=str(exc),
                )
            )
            source_status["reasoning"] = SourceStatus(
                source_kind="reasoning",
                status="degraded",
                source_ref=source_ref,
                queried_at=self.now(),
                version=self.binding.release_version,
                degraded_reason=str(exc),
                details={
                    "engine": "SEMANTICA_FORWARD",
                    "read_only": True,
                    "execution_scope": capability["execution_scope"],
                },
            )

    def _current_document_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_by_document = {}
        for row in rows:
            document_id = str(row.get("document_id") or "").strip()
            if not document_id:
                raise ValueError("Fuseki document evidence has an empty document_id")
            if document_id not in current_by_document:
                current_by_document[document_id] = self.current_registry.current(
                    self.binding.project_id,
                    document_id,
                )
        selected = []
        for row in rows:
            document_id = str(row["document_id"]).strip()
            current = current_by_document[document_id]
            if current is None:
                continue
            if (
                str(row.get("graph") or "") == current.graph_uri
                and str(row.get("version") or "") == current.version
                and str(row.get("sha256") or "") == current.file_sha256
            ):
                selected.append(row)
        return selected

    @staticmethod
    def _evidence(
        source_kind: str,
        source_ref: str,
        observed_at: datetime,
        entity_iris: list[str],
        payload: dict[str, Any],
    ) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=f"EVD-{uuid4().hex[:12]}",
            source_kind=source_kind,
            source_ref=source_ref,
            observed_at=observed_at,
            entity_iris=entity_iris,
            payload=payload,
        )
