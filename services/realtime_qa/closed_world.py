from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from services.ontology_contracts import closed_world_roles

SHA256 = re.compile(r"sha256:[a-f0-9]{64}")
SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:-]+")


class ClosedWorldInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def execute_material_set_difference(
    *,
    required_rows: list[dict[str, Any]],
    submitted_rows: list[dict[str, Any]],
    case_id: str,
    service_item_id: str,
    snapshot_version: str,
    submitted_contract: dict[str, Any],
    required_source: dict[str, Any],
    case_exists: bool,
    snapshot_complete: bool,
    material_aliases: dict[str, str] | None = None,
    alternatives: dict[str, list[str]] | None = None,
    exemptions: set[str] | None = None,
    version_anchor: dict[str, str] | None = None,
    execution_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute RequiredMaterial - SubmittedMaterial under an audited closed world.

    Absence is meaningful only when the caller proves that the case exists and
    the per-case submission snapshot is complete. Otherwise this function
    returns UNKNOWN and never emits a missing-material fact.
    """

    case_id = str(case_id).strip()
    service_item_id = str(service_item_id).strip()
    snapshot_version = str(snapshot_version).strip()
    _validate_submitted_contract(submitted_contract)
    _validate_required_source(required_source)
    dataset_type = str(submitted_contract["dataset_type"])
    production_evidence = bool(submitted_contract["production_evidence"])
    if (
        str(required_source["dataset_type"]) != dataset_type
        or bool(required_source["production_evidence"]) is not production_evidence
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "required and submitted sources belong to different evidence tracks"
        )
    if not case_id or not service_item_id or not snapshot_version:
        return _unknown(
            "INPUT_INCOMPLETE",
            "case/service/snapshot parameter is missing",
            dataset_type,
            production_evidence,
        )
    if not case_exists:
        return _unknown(
            "UNKNOWN_CASE",
            "case was not found in the bound case source",
            dataset_type,
            production_evidence,
        )
    if not snapshot_complete:
        return _unknown(
            "INPUT_INCOMPLETE",
            "submitted-material snapshot is not complete for this case and version",
            dataset_type,
            production_evidence,
        )

    fields = closed_world_roles.resolve_submitted_roles(
        submitted_contract["field_bindings"]
    )
    if fields is None:
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", closed_world_roles.role_binding_help()
        )
    if str(submitted_contract["snapshot_version"]) != snapshot_version:
        return _unknown(
            "INPUT_INCOMPLETE",
            "snapshot version differs from the contract",
            dataset_type,
            production_evidence,
        )
    if int(submitted_contract["row_count"]) != len(submitted_rows):
        return _unknown(
            "INPUT_INCOMPLETE",
            "submitted row count differs from the receipt",
            dataset_type,
            production_evidence,
        )
    if int(required_source["row_count"]) != len(required_rows):
        return _unknown(
            "INPUT_INCOMPLETE",
            "required row count differs from the receipt",
            dataset_type,
            production_evidence,
        )

    aliases = {
        _code(alias): _code(canonical)
        for alias, canonical in (material_aliases or {}).items()
    }

    def canonical(value: Any) -> str:
        token = _code(value)
        visited: set[str] = set()
        while token in aliases and token not in visited:
            visited.add(token)
            token = aliases[token]
        return token

    allowed_statuses = {_code(value) for value in submitted_contract["status_filter"]}
    submitted_codes: set[str] = set()
    excluded_status_count = 0
    for row in submitted_rows:
        if (
            str(row.get(fields["record_id"]) or "").strip() != case_id
            or str(row.get(fields["classifier_id"]) or "").strip()
            != service_item_id
            or str(row.get(fields["snapshot_version"]) or "").strip()
            != snapshot_version
        ):
            return _unknown(
                "INPUT_INCOMPLETE",
                "submitted row falls outside the declared case/service/snapshot scope",
                dataset_type,
                production_evidence,
            )
        if _code(row.get(fields["status_field"])) not in allowed_statuses:
            excluded_status_count += 1
            continue
        material_code = canonical(row.get(fields["subject_id"]))
        source_locator = str(row.get(fields["source_locator"]) or "").strip()
        if not material_code or not source_locator:
            return _unknown(
                "INPUT_INCOMPLETE",
                "submitted row lacks material code or source locator",
                dataset_type,
                production_evidence,
            )
        submitted_codes.add(material_code)

    exempt_codes = {canonical(value) for value in exemptions or set()}
    accepted_alternatives = {
        canonical(required): {canonical(value) for value in values}
        for required, values in (alternatives or {}).items()
    }
    required_fields = closed_world_roles.resolve_required_set_roles(
        required_source["field_bindings"]
    )
    if required_fields is None:
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND",
            closed_world_roles.role_binding_help(
                closed_world_roles.REQUIRED_SET_ROLE_ALIASES
            ),
        )
    required_codes: set[str] = set()
    skipped_optional_count = 0
    for row in required_rows:
        if (
            str(row.get(required_fields["classifier_id"]) or "").strip()
            != service_item_id
        ):
            continue
        if row.get(required_fields["mandatory"], True) is not True:
            skipped_optional_count += 1
            continue
        material_code = canonical(row.get(required_fields["subject_id"]))
        source_locator = str(row.get(required_fields["source_locator"]) or "").strip()
        if not material_code or not source_locator:
            raise ClosedWorldInputError(
                "INPUT_INCOMPLETE",
                "required row lacks material_code or source_locator",
            )
        required_codes.add(material_code)

    missing_codes: list[str] = []
    satisfied_by_alternative: dict[str, str] = {}
    for required_code in sorted(required_codes - exempt_codes):
        if required_code in submitted_codes:
            continue
        alternative = next(
            (
                value
                for value in sorted(accepted_alternatives.get(required_code, set()))
                if value in submitted_codes
            ),
            None,
        )
        if alternative is not None:
            satisfied_by_alternative[required_code] = alternative
            continue
        missing_codes.append(required_code)

    not_submitted_facts = [
        f"NotSubmittedMaterial({_token(case_id)}, {_token(code)})"
        for code in missing_codes
    ]
    missing_facts = [
        f"MissingMaterial({_token(case_id)}, {_token(code)})"
        for code in missing_codes
    ]
    evidence = {
        "operator": "CLOSED_WORLD_SET_DIFFERENCE_V1",
        "outcome": "COMPLETE",
        "dataset_type": dataset_type,
        "production_evidence": production_evidence,
        "case_id": case_id,
        "service_item_id": service_item_id,
        "snapshot_version": snapshot_version,
        "required_set": {
            "source_refs": list(required_source["source_refs"]),
            "dataset_id": required_source["dataset_id"],
            "source_sha256": required_source["source_sha256"],
            "snapshot_sha256": required_source["snapshot_sha256"],
            "input_row_count": len(required_rows),
            "distinct_required_count": len(required_codes),
        },
        "submitted_set": {
            "source_refs": list(submitted_contract["source_refs"]),
            "dataset_id": submitted_contract["dataset_id"],
            "source_sha256": submitted_contract["source_sha256"],
            "snapshot_sha256": submitted_contract["snapshot_sha256"],
            "input_row_count": len(submitted_rows),
            "accepted_distinct_count": len(submitted_codes),
            "excluded_status_count": excluded_status_count,
            "status_filter": list(submitted_contract["status_filter"]),
        },
        "difference": {
            "missing_count": len(missing_codes),
            "missing_material_codes": missing_codes,
            "exempt_count": len(required_codes & exempt_codes),
            "skipped_optional_count": skipped_optional_count,
            "satisfied_by_alternative": satisfied_by_alternative,
            "not_submitted_facts": not_submitted_facts,
            "missing_facts": missing_facts,
        },
        "pii_scope": dict(submitted_contract["pii_scope"]),
        "version_anchor": _validate_version_anchor(version_anchor or {}),
        "execution_context": _validate_execution_context(execution_context or {}),
    }
    evidence["evidence_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return evidence


def _validate_submitted_contract(contract: dict[str, Any]) -> None:
    required = {
        "dataset_id",
        "source_sha256",
        "snapshot_sha256",
        "snapshot_version",
        "row_count",
        "source_refs",
        "field_bindings",
        "status_filter",
        "pii_scope",
        "dataset_type",
        "production_evidence",
    }
    if (
        str(contract.get("completeness") or "").upper()
        != "COMPLETE_FOR_CASE_AND_SNAPSHOT"
        or not required.issubset(contract)
    ):
        raise ClosedWorldInputError(
            "INPUT_INCOMPLETE", "submitted source lacks a complete snapshot contract"
        )
    if not contract.get("dataset_id") or not contract.get("source_refs"):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "submitted source is not bound to a dataset"
        )
    dataset_type = str(contract.get("dataset_type") or "")
    production_evidence = contract.get("production_evidence")
    if (
        dataset_type not in {"TEST_ONLY", "PRODUCTION_EVIDENCE"}
        or not isinstance(production_evidence, bool)
        or production_evidence != (dataset_type == "PRODUCTION_EVIDENCE")
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "submitted source evidence-track markers are invalid"
        )
    if (
        not SHA256.fullmatch(str(contract.get("source_sha256") or ""))
        or not SHA256.fullmatch(str(contract.get("snapshot_sha256") or ""))
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "submitted source hashes are missing"
        )
    if not closed_world_roles.submitted_roles_are_valid(contract.get("field_bindings")):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", closed_world_roles.role_binding_help()
        )


def _validate_required_source(source: dict[str, Any]) -> None:
    if not all(
        source.get(field)
        for field in (
            "dataset_id",
            "source_sha256",
            "snapshot_sha256",
            "snapshot_version",
            "source_refs",
            "field_bindings",
        )
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "required-material source is not fully bound"
        )
    dataset_type = str(source.get("dataset_type") or "")
    production_evidence = source.get("production_evidence")
    field_bindings = source.get("field_bindings")
    if (
        dataset_type not in {"TEST_ONLY", "PRODUCTION_EVIDENCE"}
        or not isinstance(production_evidence, bool)
        or production_evidence != (dataset_type == "PRODUCTION_EVIDENCE")
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "required source evidence-track markers are invalid"
        )
    if (
        not SHA256.fullmatch(str(source.get("source_sha256") or ""))
        or not SHA256.fullmatch(str(source.get("snapshot_sha256") or ""))
        or isinstance(source.get("row_count"), bool)
        or not isinstance(source.get("row_count"), int)
        or int(source["row_count"]) < 0
        or not closed_world_roles.required_set_roles_are_valid(field_bindings)
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "required source contract is incomplete"
        )


def _validate_version_anchor(anchor: dict[str, str]) -> dict[str, str]:
    required = {
        "rule_id",
        "rule_version",
        "mapping_sha256",
        "executor_version",
        "release_fingerprint",
    }
    if set(anchor) != required or any(not str(anchor[key]).strip() for key in required):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "rule, mapping, executor, and release versions are not bound"
        )
    if not SHA256.fullmatch(str(anchor["mapping_sha256"])) or not SHA256.fullmatch(
        str(anchor["release_fingerprint"])
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "mapping or release fingerprint is invalid"
        )
    return {key: str(anchor[key]).strip() for key in sorted(required)}


def _validate_execution_context(context: dict[str, str]) -> dict[str, str]:
    required = {"actor", "authorized_by", "executed_by"}
    if set(context) != required or any(
        not str(context[key]).strip() for key in required
    ):
        raise ClosedWorldInputError(
            "SOURCE_NOT_BOUND", "actor, authorizer, and executor must be recorded separately"
        )
    return {key: str(context[key]).strip() for key in sorted(required)}


def _unknown(
    code: str,
    reason: str,
    dataset_type: str,
    production_evidence: bool,
) -> dict[str, Any]:
    return {
        "operator": "CLOSED_WORLD_SET_DIFFERENCE_V1",
        "outcome": "UNKNOWN",
        "dataset_type": dataset_type,
        "production_evidence": production_evidence,
        "reason_code": code,
        "reason": reason,
        "difference": {
            "missing_count": 0,
            "missing_material_codes": [],
            "not_submitted_facts": [],
            "missing_facts": [],
        },
    }


def _code(value: Any) -> str:
    return str(value or "").strip().upper()


def _token(value: Any) -> str:
    return SAFE_TOKEN.sub("_", str(value).strip()).strip("_")
