from __future__ import annotations

import json
import re
from typing import Any

from services.ontology_contracts import closed_world_roles
from services.ontology_contracts.rule_syntax import parse_atom, validate_rule_expression
from services.realtime_qa.row_fact_conditions import normalize_row_condition

CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PREDICATE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:-]{0,127}$")
RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
CONSEQUENT = re.compile(
    r"\bTHEN\s+([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(",
    re.IGNORECASE,
)
FACT_EXPRESSION = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_:-]{0,127}\([^(),]+(?:,[^(),]+)*\)$"
)
FACT_PREDICATE = re.compile(r"([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(")
NEGATED_PREDICATE = re.compile(
    r"\bNOT\s+([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(", re.IGNORECASE
)
ABSOLUTE_IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")
EXECUTION_SCOPES = {"FULL_QUERY_RESULT", "REPRESENTATIVE_INSTANCE_PROBE"}
# A document fact query is a named, reviewable view over materialized document
# evidence facts (each carrying source_locator / evidence_id / version / sha256).
# It is the DOCUMENT_ONLY counterpart of an Ontop SQL evidence query.
DOCUMENT_FACT_QUERY_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class ReasoningCapabilityError(ValueError):
    def __init__(self, message: str, *, path: str | None = None, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.reason_code = reason_code


def normalize_reasoning_capabilities(
    payload: Any,
    query_names: set[str],
    *,
    require_rules: bool,
    document_fact_query_names: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ReasoningCapabilityError("reasoning_capabilities must be an object")

    allowed_evidence_queries = set(query_names) | set(document_fact_query_names or ())
    capabilities: dict[str, dict[str, Any]] = {}
    errors: list[ReasoningCapabilityError] = []
    for raw_name, raw_capability in payload.items():
        try:
            name, capability = _normalize_reasoning_capability(
                raw_name, raw_capability,
                allowed_evidence_queries=allowed_evidence_queries,
                require_rules=require_rules,
                document_fact_query_names=document_fact_query_names,
            )
        except ReasoningCapabilityError as exc:
            errors.append(exc)
            continue
        capabilities[name] = capability
    if len(errors) == 1:
        raise errors[0]
    if errors:
        # One preflight round lists every capability gap instead of forcing a
        # model to rediscover the next failing capability after each repair.
        raise ReasoningCapabilityError(
            f"{len(errors)} 个推理能力存在问题（已一次列出全部）：" + " | ".join(str(exc) for exc in errors),
            path=errors[0].path, reason_code=errors[0].reason_code,
        )
    return capabilities


def _normalize_reasoning_capability(
    raw_name: Any,
    raw_capability: Any,
    *,
    allowed_evidence_queries: set[str],
    require_rules: bool,
    document_fact_query_names: set[str] | None,
) -> tuple[str, dict[str, Any]]:
    name = str(raw_name or "").strip()
    if not CAPABILITY_NAME.fullmatch(name) or not isinstance(raw_capability, dict):
        raise ReasoningCapabilityError(f"reasoning capability is invalid: {name}")
    description = str(raw_capability.get("description_zh") or "").strip()
    evidence_query = str(raw_capability.get("evidence_query") or "").strip()
    execution_scope = str(
        raw_capability.get("execution_scope") or "FULL_QUERY_RESULT"
    ).strip().upper()
    if not description:
        raise ReasoningCapabilityError(
            f"reasoning capability requires description_zh: {name}"
        )
    if evidence_query not in allowed_evidence_queries:
        raise ReasoningCapabilityError(
            f"reasoning capability references an unknown evidence query: {name}"
        )
    if execution_scope not in EXECUTION_SCOPES:
        raise ReasoningCapabilityError(
            f"reasoning capability execution_scope is invalid: {name}"
        )

    fact_bindings = _normalize_fact_bindings(name, raw_capability.get("fact_bindings"))
    result_predicates = _string_list(
        raw_capability.get("result_predicates"),
        field=f"reasoning capability result_predicates: {name}",
    )
    if not result_predicates or any(
        not PREDICATE.fullmatch(value) for value in result_predicates
    ):
        raise ReasoningCapabilityError(
            f"reasoning capability result_predicates are invalid: {name}"
        )
    if require_rules:
        seeded_conclusions = sorted(
            {binding["predicate"] for binding in fact_bindings} & set(result_predicates)
        )
        if seeded_conclusions:
            raise ReasoningCapabilityError(
                f"reasoning capability {name} supplies rule conclusions as input facts: "
                + ", ".join(seeded_conclusions)
                + "; fact_bindings must contain source-backed premises, while result_predicates must be produced by rules",
                path=f"realtime_runtime.reasoning_capabilities.{name}.fact_bindings",
                reason_code="RULE_CONCLUSION_AS_INPUT",
            )
    question_examples = _string_list(
        raw_capability.get("question_examples"),
        field=f"reasoning capability question_examples: {name}",
    )
    if not question_examples:
        raise ReasoningCapabilityError(
            f"reasoning capability requires question_examples: {name}"
        )
    source_rule_ids = _string_list(
        raw_capability.get("source_rule_ids"),
        field=f"reasoning capability source_rule_ids: {name}",
    )
    if not source_rule_ids:
        raise ReasoningCapabilityError(
            f"reasoning capability requires source_rule_ids: {name}"
        )

    ontology_terms = _normalize_ontology_terms(
        name,
        raw_capability.get("ontology_terms"),
    )
    runtime_validation = _normalize_runtime_validation(
        name,
        raw_capability.get("runtime_validation"),
    )
    if evidence_query in (document_fact_query_names or set()):
        _validate_document_fact_bindings(name, fact_bindings, runtime_validation["parameters"])
    elif (require_rules and runtime_validation["expected_live_outcome"] == "NEGATIVE"
          and not any("when" in binding for binding in fact_bindings)):
        raise ReasoningCapabilityError(
            f"reasoning capability {name} expects a NEGATIVE live outcome but no fact_binding has a when condition; "
            "S6 builds the counterfactual positive case by lifting when conditions on real evidence rows, so this rule "
            "can never be proven executable. Return all candidate records from the evidence query (move business FILTERs "
            "out of SPARQL) and express the judging premise as fact_bindings[].when, e.g. "
            '{"any":[{"field":"x","missing":true},{"field":"x","not_in":["A","B"]}]}.',
            path=f"realtime_runtime.reasoning_capabilities.{name}.fact_bindings",
            reason_code="NEGATIVE_WITHOUT_CONDITIONAL_PREMISE",
        )
    closed_world_inputs = _normalize_closed_world_inputs(
        name,
        raw_capability.get("closed_world_inputs"),
    )
    required_set_source = _normalize_required_set_source(
        name,
        raw_capability.get("required_set_source"),
        required=bool(closed_world_inputs),
    )

    capability = {
        "description_zh": description,
        "engine": (
            "SEMANTICA_FORWARD_WITH_ORION_SAFE_ANTI_JOIN_V1"
            if closed_world_inputs
            else "SEMANTICA_FORWARD"
        ),
        "evidence_query": evidence_query,
        "execution_scope": execution_scope,
        "fact_bindings": fact_bindings,
        "result_predicates": result_predicates,
        "question_examples": question_examples,
        "source_rule_ids": source_rule_ids,
        "ontology_terms": ontology_terms,
        "runtime_validation": runtime_validation,
        "closed_world_inputs": closed_world_inputs,
        "required_set_source": required_set_source,
    }
    if require_rules:
        rules = _normalize_rules(name, raw_capability.get("rules"))
        rule_ids = {str(rule["rule_id"]) for rule in rules}
        if rule_ids != set(source_rule_ids):
            raise ReasoningCapabilityError(
                f"reasoning capability rules and source_rule_ids differ: {name}"
            )
        consequents = {
            match.group(1)
            for rule in rules
            if (match := CONSEQUENT.search(rule["expression"]))
        }
        missing = set(result_predicates) - consequents
        if missing:
            raise ReasoningCapabilityError(
                f"reasoning result predicates have no producing rule in {name}: "
                + ", ".join(sorted(missing))
            )
        negated_predicates = {
            match.group(1)
            for rule in rules
            for match in NEGATED_PREDICATE.finditer(rule["expression"])
        }
        declared_closed_world = {
            str(item["predicate"]) for item in closed_world_inputs
        }
        if negated_predicates != declared_closed_world:
            raise ReasoningCapabilityError(
                f"reasoning capability NOT predicates and closed_world_inputs differ: {name}"
            )
        positive_rule_predicates = {
            match.group(1)
            for rule in rules
            for match in FACT_PREDICATE.finditer(
                re.sub(
                    r"\bNOT\s+[A-Za-z_][A-Za-z0-9_:-]{0,127}\s*\([^()]*\)",
                    "",
                    re.split(
                        r"\s+THEN\s+",
                        rule["expression"],
                        maxsplit=1,
                        flags=re.IGNORECASE,
                    )[0],
                    flags=re.IGNORECASE,
                )
            )
        }
        if required_set_source and (
            str(required_set_source["predicate"]) not in positive_rule_predicates
        ):
            raise ReasoningCapabilityError(
                f"reasoning capability required_set_source predicate is not a positive premise: {name}"
            )
        _require_complete_ontology_term_binding(
            name,
            ontology_terms,
            fact_bindings=fact_bindings,
            rules=rules,
            result_predicates=result_predicates,
        )
        _require_executable_rule_graph(
            name,
            fact_bindings=fact_bindings,
            rules=rules,
        )
        capability["rules"] = rules
    else:
        artifact = str(raw_capability.get("rule_artifact") or "").strip()
        checksum = str(raw_capability.get("rule_sha256") or "").strip()
        if not artifact or not re.fullmatch(r"sha256:[a-f0-9]{64}", checksum):
            raise ReasoningCapabilityError(
                f"packaged reasoning capability rule artifact is invalid: {name}"
            )
        capability["rule_artifact"] = artifact
        capability["rule_sha256"] = checksum
    if raw_capability.get("cq_bindings"):
        capability.update(_normalize_reasoning_cq_contract(name, raw_capability, capability))
    return name, capability



def _validate_document_fact_bindings(
    name: str, bindings: list[dict[str, Any]], parameters: dict[str, Any]
) -> None:
    for binding_index, binding in enumerate(bindings):
        path = f"reasoning_capabilities.{name}.fact_bindings[{binding_index}]"
        if binding.get("when") is not None:
            raise ReasoningCapabilityError(f"{path}.when is unsupported for document facts")
        for index, source in enumerate(binding["arguments"]):
            argument_path = f"{path}.arguments[{index}]"
            if "field" in source:
                field = source["field"]
                if isinstance(field, bool) or not isinstance(field, int) or field < 0:
                    raise ReasoningCapabilityError(
                        f"{argument_path}.field must be a nonnegative integer for document facts"
                    )
            if "parameter" in source:
                key = source["parameter"]
                value = parameters.get(key) if isinstance(key, str) else None
                if (not isinstance(key, str) or not key.strip()
                    or not isinstance(value, str | int | float | bool)
                    or (isinstance(value, str) and not value.strip())):
                    raise ReasoningCapabilityError(
                        f"{argument_path}.parameter requires a nonempty scalar value in "
                        f"reasoning_capabilities.{name}.runtime_validation.parameters"
                    )


def _normalize_closed_world_inputs(name: str, payload: Any) -> list[dict[str, Any]]:
    if payload in (None, []):
        return []
    if not isinstance(payload, list):
        raise ReasoningCapabilityError(
            f"reasoning capability closed_world_inputs is invalid: {name}"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise ReasoningCapabilityError(
                f"reasoning capability closed-world input is invalid: {name}"
            )
        predicate = str(raw.get("predicate") or "").strip()
        source_refs = _string_list(
            raw.get("source_refs"),
            field=f"reasoning capability closed-world source_refs: {name}",
        )
        key_fields = _string_list(
            raw.get("key_fields"),
            field=f"reasoning capability closed-world key_fields: {name}",
        )
        snapshot_sha256 = str(raw.get("snapshot_sha256") or "").strip()
        source_sha256 = str(raw.get("source_sha256") or "").strip()
        dataset_id = str(raw.get("dataset_id") or "").strip()
        snapshot_version = str(raw.get("snapshot_version") or "").strip()
        field_bindings = raw.get("field_bindings")
        status_filter = _string_list(
            raw.get("status_filter"),
            field=f"reasoning capability closed-world status_filter: {name}",
        )
        pii_scope = raw.get("pii_scope")
        dataset_type = str(raw.get("dataset_type") or "").strip().upper()
        production_evidence = raw.get("production_evidence")
        row_count = raw.get("row_count")
        bound_roles = closed_world_roles.resolve_submitted_roles(field_bindings)
        if (
            not PREDICATE.fullmatch(predicate)
            or predicate in seen
            or str(raw.get("completeness") or "").strip().upper()
            != "COMPLETE_FOR_CASE_AND_SNAPSHOT"
            or not source_refs
            or not key_fields
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", snapshot_sha256)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", source_sha256)
            or not dataset_id
            or not snapshot_version
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
            or bound_roles is None
            or not closed_world_roles.key_role_columns(bound_roles).issubset(
                {str(value).strip() for value in key_fields}
            )
            or not status_filter
            or not isinstance(pii_scope, dict)
            or str(pii_scope.get("mode") or "").strip().upper()
            != "MINIMUM_NECESSARY"
            or not isinstance(pii_scope.get("allowed_fields"), list)
            or not pii_scope.get("allowed_fields")
            or pii_scope.get("direct_identifiers_included") is not False
            or dataset_type != "PRODUCTION_EVIDENCE"
            or production_evidence is not True
        ):
            raise ReasoningCapabilityError(
                f"reasoning capability closed-world snapshot is incomplete: {name}.{predicate or 'UNKNOWN'}"
            )
        seen.add(predicate)
        normalized.append(
            {
                "predicate": predicate,
                "completeness": "COMPLETE_FOR_CASE_AND_SNAPSHOT",
                "source_refs": source_refs,
                "snapshot_sha256": snapshot_sha256,
                "snapshot_version": snapshot_version,
                "dataset_id": dataset_id,
                "source_sha256": source_sha256,
                "row_count": row_count,
                "key_fields": key_fields,
                # Store the neutral role spelling so the executor and evidence
                # receipts stay identical whichever spelling the project used.
                "field_bindings": {
                    role: bound_roles[role] for role in sorted(bound_roles)
                },
                "status_filter": status_filter,
                "pii_scope": {
                    "mode": "MINIMUM_NECESSARY",
                    "allowed_fields": _string_list(
                        pii_scope.get("allowed_fields"),
                        field=f"reasoning capability closed-world pii allowed_fields: {name}",
                    ),
                    "direct_identifiers_included": False,
                },
                "dataset_type": "PRODUCTION_EVIDENCE",
                "production_evidence": True,
            }
        )
    return normalized


def _normalize_required_set_source(
    name: str,
    payload: Any,
    *,
    required: bool,
) -> dict[str, Any] | None:
    if payload is None and not required:
        return None
    if not isinstance(payload, dict):
        raise ReasoningCapabilityError(
            f"reasoning capability required_set_source is invalid: {name}"
        )
    predicate = str(payload.get("predicate") or "").strip()
    source_refs = _string_list(
        payload.get("source_refs"),
        field=f"reasoning capability required-set source_refs: {name}",
    )
    field_bindings = payload.get("field_bindings")
    row_count = payload.get("row_count")
    bound_roles = closed_world_roles.resolve_required_set_roles(field_bindings)
    if (
        not PREDICATE.fullmatch(predicate)
        or not source_refs
        or not str(payload.get("dataset_id") or "").strip()
        or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", str(payload.get("source_sha256") or "")
        )
        or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", str(payload.get("snapshot_sha256") or "")
        )
        or not str(payload.get("snapshot_version") or "").strip()
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or bound_roles is None
        or str(payload.get("dataset_type") or "").strip().upper()
        != "PRODUCTION_EVIDENCE"
        or payload.get("production_evidence") is not True
    ):
        raise ReasoningCapabilityError(
            f"reasoning capability required-set source is incomplete: {name}"
        )
    return {
        "predicate": predicate,
        "dataset_type": "PRODUCTION_EVIDENCE",
        "production_evidence": True,
        "dataset_id": str(payload["dataset_id"]).strip(),
        "source_sha256": str(payload["source_sha256"]).strip(),
        "snapshot_sha256": str(payload["snapshot_sha256"]).strip(),
        "snapshot_version": str(payload["snapshot_version"]).strip(),
        "row_count": row_count,
        "source_refs": source_refs,
        "field_bindings": {role: bound_roles[role] for role in sorted(bound_roles)},
    }


def normalize_document_fact_queries(
    payload: Any,
    *,
    require_explicit: bool = True,
) -> dict[str, dict[str, Any]]:
    """Validate the DOCUMENT_ONLY fact-source registry.

    Each named document fact query is a reviewable, read-only view over
    materialized document evidence facts.  Every fact is provenance-bound to the
    evidence it came from (evidence_id / document_id / source_locator /
    source_sha256 / document_version).  It is the DOCUMENT_ONLY counterpart of an
    Ontop SQL evidence query and feeds a reasoning capability through
    ``fact_bindings``.
    """

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ReasoningCapabilityError("document_fact_queries must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_query in payload.items():
        name = str(raw_name or "").strip()
        if not DOCUMENT_FACT_QUERY_NAME.fullmatch(name) or not isinstance(raw_query, dict):
            raise ReasoningCapabilityError(f"document fact query is invalid: {name}")
        description_zh = str(raw_query.get("description_zh") or "").strip()
        if not description_zh:
            raise ReasoningCapabilityError(
                f"document fact query requires description_zh: {name}"
            )
        fact_source = str(raw_query.get("fact_source") or "").strip()
        if not fact_source:
            raise ReasoningCapabilityError(
                f"document fact query requires fact_source: {name}"
            )
        fact_bindings = _normalize_fact_bindings(name, raw_query.get("fact_bindings"))
        fact_artifact = str(raw_query.get("fact_artifact") or "").strip()
        fact_sha256 = str(raw_query.get("fact_sha256") or "").strip()
        facts = [] if fact_artifact else _normalize_document_facts(name, raw_query.get("facts"))
        if fact_artifact and not re.fullmatch(r"sha256:[a-f0-9]{64}", fact_sha256):
            raise ReasoningCapabilityError(
                f"document fact query fact_sha256 is invalid: {name}"
            )
        result_fields = _string_list(
            raw_query.get("result_fields"),
            field=f"document fact query result_fields: {name}",
        )
        question_examples = _string_list(
            raw_query.get("question_examples"),
            field=f"document fact query question_examples: {name}",
        )
        if require_explicit and (
            not result_fields or not question_examples
        ):
            raise ReasoningCapabilityError(
                f"document fact query requires result_fields and question_examples: {name}"
            )
        source_scope = str(raw_query.get("source_scope") or "document_evidence").strip().lower()
        if source_scope not in {"document_evidence", "document_evidence_and_inference"}:
            raise ReasoningCapabilityError(
                f"document fact query source_scope is invalid: {name}"
            )
        normalized[name] = {
            "description_zh": description_zh,
            "fact_source": fact_source,
            "fact_bindings": fact_bindings,
            "facts": facts,
            "result_fields": result_fields,
            "question_examples": question_examples,
            "source_scope": source_scope,
            "schema_version": 1,
            **({"fact_artifact": fact_artifact, "fact_sha256": fact_sha256} if fact_artifact else {}),
        }
        if raw_query.get("cq_bindings"):
            from .document_cq import normalize_document_cq_contract

            try:
                normalized[name].update(normalize_document_cq_contract(name, raw_query, normalized[name]))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ReasoningCapabilityError(f"document_fact_queries.{name}.cq_bindings: {exc}") from exc
    return normalized


def _normalize_document_facts(name: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ReasoningCapabilityError(f"document fact query requires facts: {name}")
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ReasoningCapabilityError(f"document fact is invalid: {name}")
        fact = str(item.get("fact") or "").strip()
        if not FACT_EXPRESSION.fullmatch(fact):
            raise ReasoningCapabilityError(f"document fact expression is invalid: {name}")
        predicate = FACT_PREDICATE.match(fact)
        if predicate is None:
            raise ReasoningCapabilityError(f"document fact predicate is invalid: {name}")
        provenance = item.get("provenance")
        if not isinstance(provenance, dict) or not isinstance(provenance.get("evidence_id"), str):
            raise ReasoningCapabilityError(
                f"document fact requires evidence provenance: {name}"
            )
        normalized.append(
            {
                "fact": fact,
                "predicate": predicate.group(1),
                "fact_kind": str(item.get("fact_kind") or "document_evidence_fact")
                .strip()
                .lower(),
                "provenance": {
                    "evidence_id": str(provenance["evidence_id"]),
                    "document_id": str(provenance.get("document_id") or ""),
                    "source_locator": str(provenance.get("source_locator") or ""),
                    "source_sha256": str(provenance.get("source_sha256") or ""),
                    "document_version": str(provenance.get("document_version") or ""),
                    "evidence_sha256": str(provenance.get("evidence_sha256") or ""),
                },
            }
        )
    return normalized


def _normalize_ontology_terms(name: str, payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict) or not payload:
        raise ReasoningCapabilityError(
            f"reasoning capability requires ontology_terms: {name}"
        )
    normalized: dict[str, str] = {}
    for raw_predicate, raw_iri in payload.items():
        predicate = str(raw_predicate or "").strip()
        iri = str(raw_iri or "").strip()
        if not PREDICATE.fullmatch(predicate) or not ABSOLUTE_IRI.fullmatch(iri):
            raise ReasoningCapabilityError(
                f"reasoning capability ontology term is invalid: {name}.{predicate}"
            )
        normalized[predicate] = iri
    return normalized


def _normalize_runtime_validation(name: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReasoningCapabilityError(
            f"reasoning capability requires runtime_validation: {name}"
        )
    parameters = payload.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ReasoningCapabilityError(
            f"reasoning capability runtime_validation.parameters is invalid: {name}"
        )
    min_input_facts = payload.get("min_input_facts", 1)
    min_result_facts = payload.get("min_result_facts", 1)
    require_rules_fired = payload.get("require_rules_fired", True)
    expected_live_outcome = str(
        payload.get("expected_live_outcome") or "POSITIVE"
    ).strip().upper()
    if (
        isinstance(min_input_facts, bool)
        or not isinstance(min_input_facts, int)
        or min_input_facts < 1
        or isinstance(min_result_facts, bool)
        or not isinstance(min_result_facts, int)
        or min_result_facts < 0
        or not isinstance(require_rules_fired, bool)
        or expected_live_outcome not in {"POSITIVE", "NEGATIVE"}
    ):
        raise ReasoningCapabilityError(
            f"reasoning capability runtime_validation is invalid: {name}"
        )
    if expected_live_outcome == "NEGATIVE" and (
        min_result_facts != 0 or require_rules_fired
    ):
        raise ReasoningCapabilityError(
            "negative live reasoning validation must allow zero results and rules: "
            + name
        )
    return {
        "parameters": parameters,
        "min_input_facts": min_input_facts,
        "min_result_facts": min_result_facts,
        "require_rules_fired": require_rules_fired,
        "expected_live_outcome": expected_live_outcome,
    }


def _require_complete_ontology_term_binding(
    name: str,
    ontology_terms: dict[str, str],
    *,
    fact_bindings: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    result_predicates: list[str],
) -> None:
    used = {str(binding["predicate"]) for binding in fact_bindings}
    used.update(result_predicates)
    for rule in rules:
        used.update(FACT_PREDICATE.findall(str(rule["expression"])))
    missing = used - set(ontology_terms)
    unused = set(ontology_terms) - used
    if missing or unused:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unused:
            details.append("unused " + ", ".join(sorted(unused)))
        raise ReasoningCapabilityError(
            f"reasoning capability ontology_terms do not match rule vocabulary: {name}: "
            + "; ".join(details)
        )


def normalize_rule_package(payload: Any, *, capability_name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReasoningCapabilityError("reasoning rule package must be an object")
    if int(payload.get("schema_version") or 0) != 1:
        raise ReasoningCapabilityError("reasoning rule package schema_version must be 1")
    if str(payload.get("capability_name") or "") != capability_name:
        raise ReasoningCapabilityError(
            "reasoning rule package capability_name does not match runtime contract"
        )
    return {
        "schema_version": 1,
        "capability_name": capability_name,
        "rules": _normalize_rules(capability_name, payload.get("rules")),
    }


def validate_ontology_term_binding(
    capability_name: str,
    capability: dict[str, Any],
    rules: list[dict[str, Any]],
) -> None:
    """Prove that every executable predicate is anchored to one ontology IRI."""

    _require_complete_ontology_term_binding(
        capability_name,
        dict(capability.get("ontology_terms") or {}),
        fact_bindings=list(capability.get("fact_bindings") or []),
        rules=rules,
        result_predicates=list(capability.get("result_predicates") or []),
    )

    if capability.get("cq_bindings"):
        produced = {match.group(1) for rule in rules
                    if (match := CONSEQUENT.search(str(rule.get("expression") or "")))}
        requested = {predicate for binding in capability["cq_bindings"].values()
                     for predicate in binding.get("derived_predicates") or []}
        if not requested.issubset(produced):
            raise ReasoningCapabilityError(
                f"reasoning_capabilities.{capability_name}.cq_bindings: packaged CQ predicate has no producing rule"
            )


def _normalize_fact_bindings(name: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ReasoningCapabilityError(
            f"reasoning capability requires fact_bindings: {name}"
        )
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ReasoningCapabilityError(f"reasoning fact binding is invalid: {name}")
        predicate = str(item.get("predicate") or "").strip()
        arguments = item.get("arguments")
        if not PREDICATE.fullmatch(predicate) or not isinstance(arguments, list) or not arguments:
            raise ReasoningCapabilityError(f"reasoning fact binding is invalid: {name}")
        normalized_arguments = []
        for argument in arguments:
            if not isinstance(argument, dict):
                raise ReasoningCapabilityError(
                    f"reasoning fact binding argument is invalid: {name}"
                )
            present = [key for key in ("field", "parameter", "constant") if key in argument]
            if len(present) != 1 or argument[present[0]] is None:
                raise ReasoningCapabilityError(
                    f"reasoning fact binding argument needs one source: {name}"
                )
            value = argument[present[0]]
            if present[0] != "constant" and not str(value).strip():
                raise ReasoningCapabilityError(
                    f"reasoning fact binding argument source is empty: {name}"
                )
            normalized_arguments.append({present[0]: value})
        binding: dict[str, Any] = {
            "predicate": predicate,
            "arguments": normalized_arguments,
        }
        when = item.get("when")
        if when is not None:
            try:
                binding["when"] = normalize_row_condition(when)
            except ValueError as exc:
                raise ReasoningCapabilityError(
                    f"reasoning fact binding condition is invalid: {name}: {exc}"
                ) from exc
        normalized.append(binding)
    return normalized


def _normalize_rules(name: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ReasoningCapabilityError(f"reasoning capability requires rules: {name}")
    normalized = []
    seen_ids: set[str] = set()
    for index, item in enumerate(payload):
        path = f"realtime_runtime.reasoning_capabilities.{name}.rules[{index}]"
        if not isinstance(item, dict):
            raise ReasoningCapabilityError(f"reasoning rule must be an object: {name}", path=path, reason_code="INVALID_RULE_SHAPE")
        rule_id = str(item.get("rule_id") or "").strip()
        expression = " ".join(str(item.get("expression") or "").split())
        description = str(item.get("description_zh") or "").strip()
        confidence = item.get("confidence", 1.0)
        problems = []
        if not RULE_ID.fullmatch(rule_id) or rule_id in seen_ids:
            problems.append(("rule_id", "invalid or duplicate rule_id"))
        if not description:
            problems.append(("description_zh", "description_zh is required"))
        if (isinstance(confidence, bool) or not isinstance(confidence, int | float)
                or not 0 <= float(confidence) <= 1):
            problems.append(("confidence", "confidence must be a number between 0 and 1"))
        try:
            validate_rule_expression(expression)
        except ValueError as exc:
            problems.append(("expression", str(exc)))
        if problems:
            raise ReasoningCapabilityError(
                f"reasoning rule is invalid: {name}: " + "; ".join(message for _, message in problems),
                path=path + "." + problems[0][0], reason_code="INVALID_RULE_CONTRACT",
            )
        seen_ids.add(rule_id)
        normalized.append(
            {
                "rule_id": rule_id,
                "description_zh": description,
                "expression": expression,
                "confidence": float(confidence),
            }
        )
    return normalized


def _require_executable_rule_graph(
    name: str,
    *,
    fact_bindings: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> None:
    """Reject rules whose premises cannot originate from the declared evidence query."""

    available = {str(binding["predicate"]) for binding in fact_bindings}
    pending: list[tuple[str, set[str], str]] = []
    predicate_arities: dict[str, int] = {}
    for rule in rules:
        expression = str(rule["expression"])
        matched = re.match(r"^IF\s+(.+?)\s+THEN\s+(.+)$", expression, re.IGNORECASE)
        if matched is None:  # Already rejected by _normalize_rules; keep this fail-closed.
            raise ReasoningCapabilityError(f"reasoning rule is invalid: {name}")
        atoms = [
            re.sub(r"^NOT\s+", "", atom.strip(), flags=re.IGNORECASE)
            for atom in re.split(r"\s+AND\s+", matched.group(1), flags=re.IGNORECASE)
        ] + [matched.group(2).strip()]
        for atom in atoms:
            predicate_name, arguments = parse_atom(atom)
            previous = predicate_arities.setdefault(predicate_name, len(arguments))
            if previous != len(arguments):
                raise ReasoningCapabilityError(
                    f"reasoning predicate arity differs across rules in {name}: {predicate_name}"
                )
        premises = {
            predicate.group(1)
            for atom in re.split(r"\s+AND\s+", matched.group(1), flags=re.IGNORECASE)
            if (predicate := FACT_PREDICATE.match(atom.strip())) is not None
        }
        conclusion = FACT_PREDICATE.match(matched.group(2).strip())
        if conclusion is None:
            raise ReasoningCapabilityError(f"reasoning rule is invalid: {name}")
        pending.append((str(rule["rule_id"]), premises, conclusion.group(1)))

    for binding in fact_bindings:
        predicate = str(binding["predicate"])
        expected = predicate_arities.get(predicate)
        if expected is not None and len(binding["arguments"]) != expected:
            raise ReasoningCapabilityError(
                f"reasoning fact_bindings arity differs from rule in {name}: "
                f"{predicate} expects {expected}, got {len(binding['arguments'])}"
            )

    while pending:
        ready = [item for item in pending if item[1].issubset(available)]
        if not ready:
            unresolved = sorted(
                f"{rule_id}({','.join(sorted(premises - available))})"
                for rule_id, premises, _conclusion in pending
            )
            raise ReasoningCapabilityError(
                f"reasoning rules require unavailable evidence predicates in {name}: "
                + ", ".join(unresolved)
            )
        ready_ids = {rule_id for rule_id, _premises, _conclusion in ready}
        available.update(conclusion for _rule_id, _premises, conclusion in ready)
        pending = [item for item in pending if item[0] not in ready_ids]


def _string_list(payload: Any, *, field: str) -> list[str]:
    if not isinstance(payload, list):
        raise ReasoningCapabilityError(f"{field} must be a list")
    values = [str(value).strip() for value in payload]
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ReasoningCapabilityError(f"{field} contains empty or duplicate values")
    return values


def _normalize_reasoning_cq_contract(
    name: str, raw: dict[str, Any], capability: dict[str, Any],
) -> dict[str, Any]:
    """Optional reviewed rule-CQ surface; never invent a structured DB query."""
    from rdflib import Graph

    from .cq_contract import CQBindingError, compile_reviewed_cq, normalize_cq_bindings
    from .query_capabilities import (
        QueryCapabilityError,
        _normalize_parameter_spec,
        _normalize_validation_cases,
    )
    from .sparql_terms import sparql_references_iri

    prefix = f"reasoning_capabilities.{name}"
    try:
        required = ("business_question_ids", "parameters", "result_fields", "validation_cases")
        missing = [field for field in required if field not in raw]
        if missing:
            raise ValueError("missing CQ fields: " + ", ".join(missing))
        question_ids = _string_list(raw["business_question_ids"], field=f"{prefix}.business_question_ids")
        fields = _string_list(raw["result_fields"], field=f"{prefix}.result_fields")
        if (not question_ids or not fields or len(set(question_ids)) != len(question_ids)
                or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) for field in fields)):
            raise ValueError("CQ requires unique question ids and valid SELECT result fields")
        if not isinstance(raw["parameters"], dict):
            raise ValueError("parameters must be an object")
        parameters = {key: _normalize_parameter_spec(name, key, value)
                      for key, value in raw["parameters"].items()}
        cases = _normalize_validation_cases(name, raw["validation_cases"], fields)
        if not cases:
            raise ValueError("CQ validation_cases cannot be empty")
        bindings = normalize_cq_bindings(raw["cq_bindings"], question_ids=question_ids, cases=cases, fields=fields)
        if set(bindings) != set(question_ids):
            raise ValueError("cq_bindings must cover every declared business_question_id")
        normalized = {"business_question_ids": question_ids, "parameters": parameters,
                      "result_fields": fields, "validation_cases": cases, "cq_bindings": bindings}
        compiler_capability = {**capability, **normalized}
        for question_id, binding in bindings.items():
            selected_case = next(case for case in cases if case["id"] == binding["validation_case_id"])
            # S7 currently produces one fact graph per capability. A CQ may not
            # validate another parameter scope against that already-built graph.
            # JSON comparison distinguishes true/1 and 1/1.0 as well as missing
            # keys; unknown DB defaults cannot be assumed equivalent here.
            if json.dumps(selected_case.get("parameters") or {}, sort_keys=True, allow_nan=False) != json.dumps(
                capability["runtime_validation"]["parameters"], sort_keys=True, allow_nan=False
            ):
                raise ValueError(
                    f"{question_id}: CQ validation_case.parameters must exactly match "
                    "runtime_validation.parameters; per-case fact graph execution is not supported, "
                    "so align parameters or declare separate reasoning capabilities"
                )
            if binding["answer_mode"] != "RULE_INFERENCE" or binding.get("reasoning_capability") != name:
                raise ValueError(f"{question_id}: rule CQ must use RULE_INFERENCE and its own capability")
            derived = set(binding["derived_predicates"])
            if not derived.issubset(set(capability["result_predicates"])) or any(
                not capability["ontology_terms"].get(predicate) for predicate in derived
            ):
                raise ValueError(f"{question_id}: derived predicates must be declared produced results with ontology terms")
            compiled = compile_reviewed_cq(question_id=question_id, query_name=name,
                                          query=binding["cq_sparql"], capability=compiler_capability)
            if compiled is None:
                raise ValueError(f"{question_id}: CQ cannot be compiled")
            query = compiled["sparql"]
            if any(not sparql_references_iri(query, capability["ontology_terms"][predicate]) for predicate in derived):
                raise ValueError(f"{question_id}: SELECT must reference its actual derived ontology terms")
            # Empty-graph execution proves algebra/expression compilation, not
            # expected business answers. Those still require the real S6 graph.
            result = Graph().query(query)
            projected = {str(variable) for variable in result.vars or []}
            if not set(compiled["answer_contract"]["required_bindings"]).issubset(projected):
                raise ValueError(f"{question_id}: expected fields are absent from SELECT projection")
        return normalized
    except (CQBindingError, QueryCapabilityError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReasoningCapabilityError(f"{prefix}.cq_bindings: {exc}") from exc
