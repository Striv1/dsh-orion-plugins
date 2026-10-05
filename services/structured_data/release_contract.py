from __future__ import annotations

import re
from typing import Any

from services.structured_data.multi_source import (
    CrossSourceSnapshotSet,
    EntityResolutionContract,
    SourceBinding,
    SourceSnapshotManifest,
)
from services.structured_data.source_query import SourceQueryGateway, SourceQueryTemplate

SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
LIVE_PARAMETER_TYPES = {
    "string": {"string", "code", "enum", "iri", "date", "datetime"},
    "integer": {"integer"},
    "number": {"decimal"},
    "boolean": {"boolean"},
}


class MultiSourceReleaseContractError(ValueError):
    """A reviewed multi-source runtime is not safe to package or load."""


def validate_multi_source_release_contract(
    *,
    project_id: str,
    snapshot_set_payload: Any,
    source_bindings_payload: Any,
    snapshot_manifests_payload: Any,
    identity_contracts_payload: Any,
    query_capabilities: dict[str, dict[str, Any]],
    query_template_hashes_payload: Any,
    require_production: bool,
    query_templates_payload: Any = None,
) -> tuple[
    CrossSourceSnapshotSet,
    dict[str, SourceBinding],
    dict[str, SourceSnapshotManifest],
    list[EntityResolutionContract],
    dict[str, str],
]:
    try:
        snapshot_set = CrossSourceSnapshotSet.model_validate(snapshot_set_payload)
        if not isinstance(source_bindings_payload, dict):
            raise ValueError("source_bindings must be an object")
        bindings = {
            str(source_id): SourceBinding.model_validate(payload)
            for source_id, payload in source_bindings_payload.items()
        }
        if not isinstance(snapshot_manifests_payload, dict):
            raise ValueError("snapshot_manifests must be an object")
        manifests = {
            str(source_id): SourceSnapshotManifest.model_validate(payload)
            for source_id, payload in snapshot_manifests_payload.items()
        }
        if not isinstance(identity_contracts_payload, list):
            raise ValueError("identity_contracts must be a list")
        identity_contracts = [
            EntityResolutionContract.model_validate(payload)
            for payload in identity_contracts_payload
        ]
    except ValueError as exc:
        raise MultiSourceReleaseContractError(
            f"multi-source release contract is invalid: {exc}"
        ) from exc

    if snapshot_set.project_id != project_id:
        raise MultiSourceReleaseContractError(
            "G-S6-SNAPSHOT-BINDING: snapshot set project does not match release"
        )
    embedded = {item.source_id: item for item in snapshot_set.source_snapshots}
    source_ids = set(bindings)
    # Snapshot-bound runtimes also use this contract for one-source projects.
    # Cross-source identity gates below remain conditional on a query actually
    # spanning more than one source.
    if source_ids != set(manifests) or source_ids != set(embedded) or not source_ids:
        raise MultiSourceReleaseContractError(
            "G-S6-SNAPSHOT-BINDING: source bindings and manifests do not close"
        )
    for source_id in sorted(source_ids):
        binding = bindings[source_id]
        manifest = manifests[source_id]
        embedded_manifest = embedded[source_id]
        if (
            binding.project_id != project_id
            or binding.source_id != source_id
            or manifest.project_id != project_id
            or manifest.source_id != source_id
            or manifest.model_dump(mode="json")
            != embedded_manifest.model_dump(mode="json")
        ):
            raise MultiSourceReleaseContractError(
                f"G-S6-SNAPSHOT-BINDING: inconsistent source contract for {source_id}"
            )
        unauthorized_tables = {
            table.table for table in manifest.tables
        } - set(binding.authorized_tables)
        if unauthorized_tables:
            raise MultiSourceReleaseContractError(
                f"G-S1-SOURCE-SCOPE: snapshot contains unauthorized tables for {source_id}"
            )
        if require_production and (
            not manifest.production_evidence
            or manifest.dataset_type != "PRODUCTION"
            or not snapshot_set.production_evidence
        ):
            raise MultiSourceReleaseContractError(
                "G-S7-PRODUCTION-EVIDENCE: multi-source release lacks a complete "
                "production snapshot binding"
            )

    template_hashes = _template_hashes(query_template_hashes_payload)
    live_names = {
        name
        for name, capability in query_capabilities.items()
        if capability.get("query_mode") in {"REALTIME_REQUIRED", "HYBRID"}
    }
    if query_templates_payload is None:
        query_templates_payload = {}
    if (
        not isinstance(query_templates_payload, dict)
        or set(query_templates_payload) != live_names
    ):
        raise MultiSourceReleaseContractError(
            "G-S6-REALTIME-READONLY: every live query requires one reviewed executable plan"
        )
    if set(template_hashes) != live_names:
        raise MultiSourceReleaseContractError(
            "G-S6-REALTIME-READONLY: live query template hashes do not close"
        )
    try:
        templates = {
            name: SourceQueryTemplate.model_validate(payload)
            for name, payload in query_templates_payload.items()
        }
        gateway = SourceQueryGateway(
            bindings=list(bindings.values()), templates=list(templates.values())
        )
    except ValueError as exc:
        raise MultiSourceReleaseContractError(
            f"G-S6-REALTIME-READONLY: invalid live query plan: {exc}"
        ) from exc
    for name, capability in query_capabilities.items():
        query_sources = set(capability.get("source_ids") or [])
        if query_sources and not query_sources.issubset(source_ids):
            raise MultiSourceReleaseContractError(
                f"G-S6-CROSS-SOURCE-QUERY: {name} references an unbound source"
            )
        for field_name in ("source_tables_by_id", "source_columns_by_id"):
            scoped = capability.get(field_name) or {}
            if set(scoped) - query_sources:
                raise MultiSourceReleaseContractError(
                    f"G-S3-SOURCE-MAPPING-COVERAGE: {name} has unbound {field_name}"
                )
        for source_id, tables in (capability.get("source_tables_by_id") or {}).items():
            if set(tables) - set(bindings[source_id].authorized_tables):
                raise MultiSourceReleaseContractError(
                    f"G-S1-SOURCE-SCOPE: {name} references unauthorized tables for {source_id}"
                )
        if name in live_names:
            template = templates[name]
            if query_sources != {template.source_id}:
                raise MultiSourceReleaseContractError(
                    f"G-S6-REALTIME-READONLY: {name} requires one matching bound source"
                )
            if bindings[template.source_id].status != "ACTIVE":
                raise MultiSourceReleaseContractError(
                    f"G-S6-REALTIME-READONLY: {name} source must be ACTIVE"
                )
            used_tables = set(template.plan.tables.values())
            used_columns = {
                ref.split(".", 1)[1] for ref in template.plan.select.values()
            }
            for join in template.plan.joins:
                used_columns.update(
                    (join.left.split(".", 1)[1], join.right.split(".", 1)[1])
                )
            used_columns.update(
                item.field.split(".", 1)[1] for item in template.plan.filters
            )
            declared_tables = set(
                (capability.get("source_tables_by_id") or {}).get(template.source_id) or []
            )
            declared_columns = set(
                (capability.get("source_columns_by_id") or {}).get(template.source_id) or []
            )
            if used_tables != declared_tables or used_columns != declared_columns:
                raise MultiSourceReleaseContractError(
                    f"G-S3-SOURCE-MAPPING-COVERAGE: {name} live plan does not match declared dependencies"
                )
            if set(template.parameters) != set(capability.get("parameters") or {}):
                raise MultiSourceReleaseContractError(
                    f"G-S6-REALTIME-READONLY: {name} parameter schema differs from reviewed capability"
                )
            for parameter_name, schema in template.parameters.items():
                reviewed = capability["parameters"][parameter_name]
                if (
                    reviewed.get("type") not in LIVE_PARAMETER_TYPES[schema.type]
                    or reviewed.get("required") is not True
                    or "default" in reviewed
                    or not schema.required
                ):
                    raise MultiSourceReleaseContractError(
                        f"G-S6-REALTIME-READONLY: {name}.{parameter_name} live parameter type or presence differs"
                    )
            if set(template.plan.select) != set(capability.get("result_fields") or []):
                raise MultiSourceReleaseContractError(
                    f"G-S6-REALTIME-READONLY: {name} result fields differ from reviewed capability"
                )
            if gateway.template_sha256(template.template_id) != template_hashes[name]:
                raise MultiSourceReleaseContractError(
                    f"G-S6-REALTIME-READONLY: {name} live plan or source scope hash differs"
                )
        if len(query_sources) > 1 and not any(
            contract.project_id == project_id
            and query_sources.issubset(contract.source_join_keys)
            for contract in identity_contracts
        ):
            raise MultiSourceReleaseContractError(
                f"G-S3-CROSS-SOURCE-IDENTITY: {name} lacks a covering identity contract"
            )

    for contract in identity_contracts:
        contract_sources = set(contract.source_join_keys)
        if contract.project_id != project_id or not contract_sources.issubset(source_ids):
            raise MultiSourceReleaseContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: identity contract references an unbound project/source"
            )
        for source_id, table in contract.source_tables.items():
            snapshot_tables = {item.table for item in manifests[source_id].tables}
            if (
                table not in bindings[source_id].authorized_tables
                or table not in snapshot_tables
            ):
                raise MultiSourceReleaseContractError(
                    "G-S3-CROSS-SOURCE-IDENTITY: identity table is not bound to the snapshot"
                )

    return snapshot_set, bindings, manifests, identity_contracts, template_hashes


def _template_hashes(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MultiSourceReleaseContractError("query_template_hashes must be an object")
    normalized = {str(name): str(digest) for name, digest in value.items()}
    if any(not SHA256.fullmatch(digest) for digest in normalized.values()):
        raise MultiSourceReleaseContractError(
            "query_template_hashes must contain sha256-prefixed digests"
        )
    return normalized
