from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
QUALIFIED_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_$-]{0,127})?$"
)
UNSAFE_IDENTITY_KEY = re.compile(
    r"(?:^|_)(?:name|full_name|display_name|姓名|名称)(?:$|_)", re.IGNORECASE
)


class MultiSourceContractError(ValueError):
    """Raised when a source or snapshot cannot safely enter the Snapshot Hub."""


class SourceBinding(BaseModel):
    project_id: str = Field(min_length=3, max_length=160)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    engine: Literal["POSTGRESQL", "MYSQL"]
    connection_ref: str = Field(
        pattern=r"^(?:(?:env|secret)://[A-Za-z0-9_.:/-]+|chat2db://community/[1-9][0-9]{0,18})$"
    )
    database: str = Field(min_length=1, max_length=128)
    catalog: str | None = Field(default=None, max_length=128)
    schemas: list[str] = Field(min_length=1, max_length=32)
    authorized_tables: list[str] = Field(min_length=1, max_length=500)
    authorized_columns: dict[str, list[str]] = Field(default_factory=dict)
    access_mode: Literal["READ_ONLY"] = "READ_ONLY"
    readonly_attested: bool
    pii_scope: str = Field(min_length=1, max_length=256)
    owner: str = Field(min_length=1, max_length=160)
    status: Literal["ACTIVE", "DISABLED", "DEGRADED"] = "ACTIVE"

    @model_validator(mode="after")
    def validate_scope(self) -> SourceBinding:
        if self.connection_ref.startswith("chat2db://") and self.engine != "POSTGRESQL":
            raise MultiSourceContractError("UNSUPPORTED_OPERATOR: Chat2DB snapshot adapter supports POSTGRESQL only")
        if not self.readonly_attested:
            raise MultiSourceContractError("G-S1-SOURCE-READONLY: 缺少只读身份回执")
        if any(not SAFE_IDENTIFIER.fullmatch(item) for item in self.schemas):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: schema/table 名称非法")
        if any(not QUALIFIED_IDENTIFIER.fullmatch(item) for item in self.authorized_tables):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: schema/table 名称非法")
        if len(self.authorized_tables) != len(set(self.authorized_tables)):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: authorized_tables 重复")
        unknown_tables = set(self.authorized_columns) - set(self.authorized_tables)
        if unknown_tables:
            raise MultiSourceContractError(
                "G-S1-SOURCE-SCOPE: column allowlist references unauthorized tables"
            )
        for columns in self.authorized_columns.values():
            if not columns or len(columns) != len(set(columns)):
                raise MultiSourceContractError(
                    "G-S1-SOURCE-SCOPE: authorized_columns must be non-empty and unique"
                )
            if any(not SAFE_IDENTIFIER.fullmatch(item) for item in columns):
                raise MultiSourceContractError(
                    "G-S1-SOURCE-SCOPE: column name is invalid"
                )
        return self


class SourceColumnProfile(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
    data_type: str = Field(min_length=1, max_length=256)
    nullable: bool
    ordinal_position: int = Field(ge=1)


class SourceTableProfile(BaseModel):
    table: str = Field(pattern=QUALIFIED_IDENTIFIER.pattern)
    row_count: int = Field(ge=0)
    columns: list[SourceColumnProfile] = Field(min_length=1)
    schema_sha256: str = Field(pattern=SHA256.pattern)


class SourceProfileReceipt(BaseModel):
    project_id: str
    source_id: str
    engine: Literal["POSTGRESQL", "MYSQL"]
    profiled_at: datetime
    database: str
    access_mode: Literal["READ_ONLY"] = "READ_ONLY"
    readonly_verified: Literal[True] = True
    tables: list[SourceTableProfile] = Field(min_length=1)
    schema_fingerprint: str = Field(pattern=SHA256.pattern)
    receipt_sha256: str = Field(pattern=SHA256.pattern)


class TableSnapshot(BaseModel):
    table: str = Field(pattern=QUALIFIED_IDENTIFIER.pattern)
    target_table: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    source_row_count: int = Field(ge=0)
    snapshot_row_count: int = Field(ge=0)
    schema_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_locator: str = Field(min_length=1, max_length=2048)


class SourceSnapshotManifest(BaseModel):
    project_id: str = Field(min_length=3, max_length=160)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    engine: Literal["POSTGRESQL", "MYSQL"]
    dataset_id: str = Field(pattern=r"^DS-[A-Z0-9-]{8,80}$")
    snapshot_version: str = Field(min_length=1, max_length=128)
    captured_at: datetime
    high_watermark: str | None = Field(default=None, max_length=256)
    snapshot_complete: bool
    source_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    pii_scope: str = Field(min_length=1, max_length=256)
    tables: list[TableSnapshot] = Field(min_length=1)
    dataset_type: Literal["PRODUCTION", "TEST_ONLY"]
    production_evidence: bool

    @model_validator(mode="after")
    def validate_manifest(self) -> SourceSnapshotManifest:
        if not self.snapshot_complete:
            raise MultiSourceContractError(
                "G-S1-SNAPSHOT-COMPLETE: source snapshot is incomplete"
            )
        if any(item.source_row_count != item.snapshot_row_count for item in self.tables):
            raise MultiSourceContractError(
                "G-S1-SNAPSHOT-RECONCILIATION: source/snapshot row counts differ"
            )
        if self.dataset_type == "TEST_ONLY" and self.production_evidence:
            raise MultiSourceContractError(
                "G-S7-PRODUCTION-EVIDENCE: TEST_ONLY cannot be production evidence"
            )
        if self.dataset_type == "PRODUCTION" and not self.production_evidence:
            raise MultiSourceContractError(
                "G-S7-PRODUCTION-EVIDENCE: production snapshot lacks evidence attestation"
            )
        return self


class CrossSourceSnapshotSet(BaseModel):
    snapshot_set_id: str = Field(pattern=r"^SS-[A-F0-9]{24}$")
    project_id: str
    snapshot_version: str
    promoted_at: datetime
    # A snapshot set is also the version-binding primitive for legacy
    # single-source DATABASE_ONLY projects.  Multi-source S1 still enforces
    # two or more SourceBindings in the workflow gate; allowing one manifest
    # here lets both project shapes share the same atomic promotion, evidence,
    # and release-binding contract.
    source_snapshots: list[SourceSnapshotManifest] = Field(min_length=1)
    snapshot_complete: Literal[True] = True
    manifest_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    production_evidence: bool


class EntityResolutionContract(BaseModel):
    contract_id: str = Field(pattern=r"^ER-[A-Z0-9-]{8,80}$")
    project_id: str = Field(min_length=3, max_length=160)
    canonical_entity: str = Field(min_length=1, max_length=256)
    source_tables: dict[str, str] = Field(min_length=2)
    source_join_keys: dict[str, list[str]] = Field(min_length=2)
    normalization: Literal["EXACT", "TRIM_UPPER", "STABLE_HASH"]
    cardinality: Literal["ONE_TO_ONE", "MANY_TO_ONE", "ONE_TO_MANY"]
    collision_policy: Literal["FAIL", "AUTHORITY_WINS"]
    source_authority: list[str] = Field(min_length=1)
    freshness_policy: Literal["SNAPSHOT_SET", "AUTHORITY_THEN_FRESHNESS"]
    pii_policy: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_identity(self) -> EntityResolutionContract:
        source_ids = set(self.source_join_keys)
        if set(self.source_tables) != source_ids:
            raise MultiSourceContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: every source needs one explicit source table"
            )
        if any(
            not QUALIFIED_IDENTIFIER.fullmatch(table)
            for table in self.source_tables.values()
        ):
            raise MultiSourceContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: invalid source table"
            )
        if not set(self.source_authority).issubset(source_ids):
            raise MultiSourceContractError(
                "G-S3-CONFLICT-POLICY: source authority references an unbound source"
            )
        if self.collision_policy == "AUTHORITY_WINS" and not self.source_authority:
            raise MultiSourceContractError(
                "G-S3-CONFLICT-POLICY: AUTHORITY_WINS requires source authority"
            )
        for keys in self.source_join_keys.values():
            if not keys or any(not SAFE_IDENTIFIER.fullmatch(item) for item in keys):
                raise MultiSourceContractError(
                    "G-S3-CROSS-SOURCE-IDENTITY: explicit valid join keys are required"
                )
            if any(UNSAFE_IDENTITY_KEY.search(item) for item in keys):
                raise MultiSourceContractError(
                    "G-S3-CROSS-SOURCE-IDENTITY: fuzzy/name identity is forbidden"
                )
        return self


class IdentityRowReference(BaseModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    dataset_id: str = Field(pattern=r"^DS-[A-Z0-9-]{8,80}$")
    table: str = Field(pattern=QUALIFIED_IDENTIFIER.pattern)
    row_ordinal: int = Field(gt=0)
    row_sha256: str = Field(pattern=SHA256.pattern)


class CanonicalIdentityLink(BaseModel):
    canonical_key_sha256: str = Field(pattern=SHA256.pattern)
    canonical_iri: str = Field(min_length=3, max_length=2048)
    source_rows: list[IdentityRowReference] = Field(min_length=2)


class IdentityResolutionReceipt(BaseModel):
    contract_id: str = Field(pattern=r"^ER-[A-Z0-9-]{8,80}$")
    project_id: str
    snapshot_set_id: str = Field(pattern=r"^SS-[A-F0-9]{24}$")
    resolved_at: datetime
    matched_identity_count: int = Field(ge=0)
    unmatched_row_count: int = Field(ge=0)
    links: list[CanonicalIdentityLink]
    receipt_sha256: str = Field(pattern=SHA256.pattern)


class SourceMappingTerm(BaseModel):
    term_iri: str = Field(min_length=3, max_length=2048)
    kind: Literal["CLASS", "OBJECT_PROPERTY", "DATA_PROPERTY"]
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    table: str = Field(pattern=QUALIFIED_IDENTIFIER.pattern)
    columns: list[str] = Field(min_length=1)
    domain_iri: str | None = Field(default=None, max_length=2048)
    range_iri: str | None = Field(default=None, max_length=2048)
    source_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_semantics(self) -> SourceMappingTerm:
        if self.kind in {"OBJECT_PROPERTY", "DATA_PROPERTY"} and (
            not self.domain_iri or not self.range_iri
        ):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: property mapping lacks domain/range"
            )
        if any(not SAFE_IDENTIFIER.fullmatch(item) for item in self.columns):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: invalid mapped column"
            )
        return self


class SourceMappingFragment(BaseModel):
    project_id: str
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    snapshot_version: str = Field(min_length=1, max_length=128)
    terms: list[SourceMappingTerm] = Field(min_length=1)
    obda_mappings: list[str] = Field(min_length=1)


class UnifiedMappingContract(BaseModel):
    project_id: str
    snapshot_set_id: str = Field(pattern=r"^SS-[A-F0-9]{24}$")
    source_ids: list[str] = Field(min_length=2)
    terms: list[SourceMappingTerm] = Field(min_length=1)
    obda_mappings: list[str] = Field(min_length=1)
    mapping_sha256: str = Field(pattern=SHA256.pattern)


def merge_source_mapping_fragments(
    *,
    snapshot_set: CrossSourceSnapshotSet,
    fragments: list[SourceMappingFragment],
) -> UnifiedMappingContract:
    by_source = {item.source_id: item for item in fragments}
    expected = {item.source_id for item in snapshot_set.source_snapshots}
    if set(by_source) != expected or len(by_source) != len(fragments):
        raise MultiSourceContractError(
            "G-S3-SOURCE-MAPPING-COVERAGE: every snapshot source needs one mapping fragment"
        )
    for snapshot in snapshot_set.source_snapshots:
        fragment = by_source[snapshot.source_id]
        if fragment.project_id != snapshot_set.project_id:
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: cross-project mapping fragment"
            )
        if fragment.snapshot_version != snapshot.snapshot_version:
            raise MultiSourceContractError(
                "G-S6-SNAPSHOT-BINDING: mapping fragment snapshot version mismatch"
            )
        if any(term.source_id != fragment.source_id for term in fragment.terms):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: term source_id mismatch"
            )
    terms = [term for source_id in sorted(by_source) for term in by_source[source_id].terms]
    obda = [item for source_id in sorted(by_source) for item in by_source[source_id].obda_mappings]
    payload = {
        "project_id": snapshot_set.project_id,
        "snapshot_set_id": snapshot_set.snapshot_set_id,
        "terms": [item.model_dump(mode="json") for item in terms],
        "obda_mappings": obda,
    }
    return UnifiedMappingContract(
        project_id=snapshot_set.project_id,
        snapshot_set_id=snapshot_set.snapshot_set_id,
        source_ids=sorted(by_source),
        terms=terms,
        obda_mappings=obda,
        mapping_sha256=_sha256(payload),
    )


def _sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_cross_source_snapshot_set(
    *,
    project_id: str,
    bindings: list[SourceBinding],
    snapshots: list[SourceSnapshotManifest],
    max_skew_seconds: int = 300,
    now: datetime | None = None,
) -> CrossSourceSnapshotSet:
    """Atomically validate and promote one complete project snapshot set.

    This function has no partial-success state: any missing, stale, out-of-scope,
    un-reconciled, or cross-project source fails before a current set is built.
    Persistence callers may therefore write the returned set in one transaction.
    """

    active = {item.source_id: item for item in bindings if item.status == "ACTIVE"}
    if not active:
        raise MultiSourceContractError(
            "G-S1-SOURCE-INVENTORY: at least one active source is required"
        )
    if any(item.project_id != project_id for item in bindings):
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: cross-project binding")
    by_source = {item.source_id: item for item in snapshots}
    if set(by_source) != set(active):
        raise MultiSourceContractError(
            "G-S1-SNAPSHOT-COMPLETE: every active source must have exactly one snapshot"
        )
    if len(by_source) != len(snapshots):
        raise MultiSourceContractError(
            "G-S1-MULTI-SOURCE-INVENTORY: duplicate source snapshot"
        )
    for source_id, snapshot in by_source.items():
        binding = active[source_id]
        if snapshot.project_id != project_id or snapshot.engine != binding.engine:
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: snapshot identity mismatch")
        unauthorized = {item.table for item in snapshot.tables} - set(
            binding.authorized_tables
        )
        if unauthorized:
            raise MultiSourceContractError(
                "G-S1-SOURCE-SCOPE: unauthorized tables: "
                + ", ".join(sorted(unauthorized))
            )
    captured = [item.captured_at.astimezone(UTC) for item in snapshots]
    skew = (max(captured) - min(captured)).total_seconds()
    if skew > max_skew_seconds:
        raise MultiSourceContractError(
            "G-S1-CROSS-SOURCE-TIME-CONSISTENCY: snapshot time windows differ"
        )
    versions = sorted(f"{item.source_id}@{item.snapshot_version}" for item in snapshots)
    canonical = {
        "project_id": project_id,
        "sources": [
            item.model_dump(mode="json")
            for item in sorted(snapshots, key=lambda value: value.source_id)
        ],
        "versions": versions,
    }
    digest = _sha256(canonical)
    return CrossSourceSnapshotSet(
        snapshot_set_id="SS-" + digest.removeprefix("sha256:")[:24].upper(),
        project_id=project_id,
        snapshot_version="+".join(versions),
        promoted_at=(now or datetime.now(UTC)),
        source_snapshots=sorted(snapshots, key=lambda value: value.source_id),
        manifest_sha256=digest,
        production_evidence=all(item.production_evidence for item in snapshots),
    )
