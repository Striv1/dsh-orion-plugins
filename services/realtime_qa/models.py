from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ENTITY_IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class OntologyReleaseBinding(BaseModel):
    project_id: str = Field(min_length=1, max_length=160)
    release_version: str = Field(min_length=1, max_length=64)
    release_fingerprint: str = Field(min_length=8, max_length=128)
    ontology_iri: str
    ontology_artifact: str = Field(min_length=1)
    runtime_mode: Literal["STRUCTURED", "DOCUMENT_ONLY", "HYBRID"] = "HYBRID"
    structured_query_enabled: bool = True
    mapping_artifact: str | None = None
    source_mapping_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    database_access_mode: Literal["READ_ONLY"]
    ontop_identity_query_artifact: str | None = None
    ontop_query_names: frozenset[str] = Field(default_factory=frozenset)
    ontop_query_artifacts: dict[str, str] = Field(default_factory=dict)
    ontop_query_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reasoning_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reasoning_rule_packages: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        exclude=True,
    )
    document_fact_queries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    document_query_capabilities: list[str] = Field(default_factory=list)
    document_query_examples: list[str] = Field(default_factory=list)
    artifact_checksums: dict[str, str]
    package_path: str = Field(min_length=1)
    integrity_status: Literal["verified"]
    ontop_deployment_id: str | None = Field(default=None, min_length=1, max_length=160)
    published_at: datetime
    snapshot_set_id: str | None = Field(default=None, pattern=r"^SS-[A-F0-9]{24}$")
    cross_source_snapshot_set: dict[str, Any] | None = None
    source_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    snapshot_manifests: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source_trace: dict[str, Any] = Field(default_factory=dict)
    identity_contracts: list[dict[str, Any]] = Field(default_factory=list)
    query_template_hashes: dict[str, str] = Field(default_factory=dict)
    source_query_templates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    contract_migrations: list[str] = Field(default_factory=list)

    @field_validator("ontology_iri")
    @classmethod
    def validate_ontology_iri(cls, value: str) -> str:
        if not ENTITY_IRI.fullmatch(value):
            raise ValueError("ontology_iri must be an absolute HTTP(S) or URN IRI")
        return value

    @model_validator(mode="after")
    def validate_runtime_mode(self) -> OntologyReleaseBinding:
        if self.structured_query_enabled:
            required = (
                self.mapping_artifact,
                self.source_mapping_sha256,
                self.ontop_identity_query_artifact,
                self.ontop_deployment_id,
            )
            if not all(required) or not self.ontop_query_names:
                raise ValueError("structured runtime binding is incomplete")
        elif self.runtime_mode != "DOCUMENT_ONLY" or self.ontop_query_names:
            raise ValueError("document-only runtime binding is inconsistent")
        return self


class OntopDeploymentIdentity(BaseModel):
    deployment_id: str
    endpoint: str
    release_fingerprint: str
    mapping_sha256: str
    access_mode: Literal["READ_ONLY"]
    status: Literal["READY"]
    verified_at: datetime


class StructuredQueryRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ReasoningQueryRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class DocumentQueryRequest(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, values: list[str]) -> list[str]:
        pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$")
        if any(not pattern.fullmatch(value) for value in values):
            raise ValueError("document_ids contain an invalid document id")
        return list(dict.fromkeys(values))


class RealtimeEvidenceRequest(BaseModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=160)
    query_id: str = Field(min_length=1, max_length=128)
    structured_query: StructuredQueryRequest | None = None
    reasoning_query: ReasoningQueryRequest | None = None
    document_fact_query: StructuredQueryRequest | None = None
    document_query: DocumentQueryRequest | None = None
    entity_iris: list[str] = Field(default_factory=list, max_length=20)
    execution_mode: Literal["SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID"] = (
        "SNAPSHOT_ONLY"
    )

    @field_validator("entity_iris")
    @classmethod
    def validate_entity_iris(cls, values: list[str]) -> list[str]:
        for value in values:
            if not ENTITY_IRI.fullmatch(value):
                raise ValueError(f"invalid entity IRI: {value}")
        return values

    @model_validator(mode="after")
    def require_a_source(self) -> RealtimeEvidenceRequest:
        if (
            self.structured_query is None
            and self.reasoning_query is None
            and self.document_fact_query is None
            and self.document_query is None
            and not self.entity_iris
        ):
            raise ValueError(
                "at least one structured query, reasoning query, document fact query, document query, "
                "or entity IRI is required"
            )
        return self


class DocumentPageEvidence(BaseModel):
    page_number: int = Field(ge=1)
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_locator: str | None = Field(default=None, min_length=1, max_length=1000)


class S0DocumentVersion(BaseModel):
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    document_type: str = Field(min_length=1, max_length=120)
    source_uri: str = Field(min_length=1, max_length=2048)
    file_sha256: str
    processed_at: datetime
    full_text: str = Field(min_length=1)
    mentions_entities: list[str] = Field(default_factory=list, max_length=100)
    pages: list[DocumentPageEvidence] = Field(default_factory=list)

    @field_validator("file_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256.fullmatch(value):
            raise ValueError("file_sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("mentions_entities")
    @classmethod
    def validate_mentions(cls, values: list[str]) -> list[str]:
        for value in values:
            if not ENTITY_IRI.fullmatch(value):
                raise ValueError(f"invalid entity IRI: {value}")
        return values


class DocumentIndexReceipt(BaseModel):
    project_id: str
    document_id: str
    version: str
    graph_uri: str
    file_sha256: str
    triple_count: int = Field(ge=1)
    indexed_at: datetime


class OriginalSnapshot(BaseModel):
    backend: Literal["minio"]
    bucket: str
    object_key: str
    source_uri: str
    file_sha256: str
    size_bytes: int = Field(ge=0)


class CurrentDocumentPointer(BaseModel):
    project_id: str
    document_id: str
    version: str
    file_sha256: str
    source_uri: str
    graph_uri: str
    indexed_at: datetime
    promoted_at: datetime


class IncrementalIngestionResult(BaseModel):
    status: Literal["promoted", "unchanged"]
    snapshot: OriginalSnapshot
    current: CurrentDocumentPointer


class EvidenceRecord(BaseModel):
    evidence_id: str
    source_kind: Literal["ontology_release", "structured_db", "document", "reasoning"]
    source_ref: str
    observed_at: datetime
    entity_iris: list[str] = Field(default_factory=list)
    payload: dict[str, Any]


class DegradedSource(BaseModel):
    source_kind: Literal["structured_db", "document", "reasoning"]
    source_ref: str
    reason: str
    retryable: bool = True


class SourceStatus(BaseModel):
    source_kind: Literal["structured_db", "document", "reasoning"]
    status: Literal["fresh", "empty", "degraded", "not_requested"]
    source_ref: str
    queried_at: datetime | None
    version: str | None
    degraded_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SourceEvidenceV2(BaseModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    engine: str = Field(min_length=1, max_length=64)
    database: str = Field(min_length=1, max_length=256)
    schema_name: str | None = Field(default=None, max_length=256)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    dataset_id: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    snapshot_version: str | None = None
    source_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    observed_at: datetime | None = None
    queried_at: datetime | None = None
    query_mode: Literal["SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID", "UNKNOWN"] = "UNKNOWN"
    provenance_scope: Literal["RELEASE_SNAPSHOT_CONTRACT", "PROTECTED_S1_TRACE_ONLY", "UNAVAILABLE"] = "UNAVAILABLE"
    freshness: Literal["FRESH", "STALE", "UNKNOWN"]
    locator: str = Field(min_length=1, max_length=2048)
    pii_scope: str = Field(min_length=1, max_length=256)
    masking: str = Field(min_length=1, max_length=256)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    derived_facts: list[dict[str, Any]] = Field(default_factory=list)
    rule_id: str | None = None
    rule_version: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    snapshot_set_id: str | None = Field(default=None, pattern=r"^SS-[A-F0-9]{24}$")
    release_fingerprint: str
    realtime_query_receipt: dict[str, Any] | None = None


class EvidenceBundle(BaseModel):
    query_id: str
    mode: Literal["structured", "documents", "reasoning", "hybrid"]
    release: OntologyReleaseBinding
    generated_at: datetime
    complete: bool
    evidence: list[EvidenceRecord]
    degraded_sources: list[DegradedSource] = Field(default_factory=list)
    source_status: dict[str, SourceStatus]
    snapshot_set_id: str | None = Field(default=None, pattern=r"^SS-[A-F0-9]{24}$")
    source_status_by_id: dict[str, dict[str, Any]] = Field(default_factory=dict)
    realtime_query_receipts: list[dict[str, Any]] = Field(default_factory=list)
    source_evidence_v2: list[SourceEvidenceV2] = Field(default_factory=list)
    query_modes: dict[str, str] = Field(default_factory=dict)


class RealtimeAnswerRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    evidence_request: RealtimeEvidenceRequest


class EvidenceCitation(BaseModel):
    citation_id: str
    source_kind: Literal["ontology_release", "structured_db", "document", "reasoning"]
    source_ref: str
    observed_at: datetime
    locator: dict[str, Any]
    facts: dict[str, Any] = Field(default_factory=dict)


class RealtimeAnswer(BaseModel):
    question: str
    answer_status: Literal["complete", "partial", "no_evidence"]
    complete: bool
    answer: str
    citations: list[EvidenceCitation]
    warnings: list[str] = Field(default_factory=list)
    generated_by: Literal["deterministic_evidence_renderer"] = "deterministic_evidence_renderer"
    evidence_bundle: EvidenceBundle


class RealtimeRuntimeBinding(BaseModel):
    project_id: str
    release_version: str
    release_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    runtime_mode: Literal["STRUCTURED", "DOCUMENT_ONLY", "HYBRID"] = "HYBRID"
    structured_query_enabled: bool = True
    artifact_verified: bool
    runtime_verified: bool
    runtime_degraded_reason: str | None = None
    database_access_mode: Literal["READ_ONLY"]
    ontop_query_names: list[str]
    ontop_query_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reasoning_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    document_fact_query_capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reasoning_runtime_verified: bool = False
    reasoning_runtime_degraded_reason: str | None = None
    document_query_capabilities: list[str] = Field(default_factory=list)
    document_query_examples: list[str] = Field(default_factory=list)
    document_runtime_verified: bool = False
    current_document_count: int = Field(default=0, ge=0)
    document_runtime_degraded_reason: str | None = None


class RealtimeRuntimeRegistration(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$")
    project_dir: str = Field(min_length=1, max_length=4096)
    deployment_binding_path: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool = True


class RealtimeRuntimeRegistryConfig(BaseModel):
    schema_version: Literal[1] = 1
    default_project_id: str | None = Field(default=None, min_length=3, max_length=160)
    runtimes: list[RealtimeRuntimeRegistration] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_registry(self) -> RealtimeRuntimeRegistryConfig:
        enabled_ids = [item.project_id for item in self.runtimes if item.enabled]
        if len(enabled_ids) != len(set(enabled_ids)):
            raise ValueError("runtime registry has duplicate enabled project_id values")
        if self.default_project_id is not None and self.default_project_id not in enabled_ids:
            raise ValueError("default_project_id is not an enabled runtime")
        return self


class RealtimeRuntimeCatalog(BaseModel):
    default_project_id: str | None
    runtimes: list[RealtimeRuntimeBinding]
    unavailable_projects: dict[str, str] = Field(default_factory=dict)


class RealtimeReleaseExpectation(BaseModel):
    project_id: str = Field(min_length=1, max_length=160)
    release_version: str = Field(min_length=1, max_length=64)
    release_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class RealtimeSessionAnswerRequest(BaseModel):
    session_id: str = Field(pattern=r"^session-[a-f0-9-]{36}$")
    expected_release: RealtimeReleaseExpectation
    answer_request: RealtimeAnswerRequest


class RealtimeSessionAnswer(BaseModel):
    session_id: str
    release_match: Literal["verified"] = "verified"
    runtime_binding: RealtimeRuntimeBinding
    answer: RealtimeAnswer
    evidence_receipt: dict[str, Any] | None = None
