"""Compile S4 acceptance queries from an explicitly reviewed S3 query binding.

This module never queries data, invents expectations, or treats a first row as
an exact result set.  Every extra assertion is part of the reviewed binding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pyparsing import ParseResults

from services.ontology_contracts.cq_answers import normalize_expected_rows
from services.ontology_contracts.errors import CQBindingError as CQBindingError
from services.ontology_contracts.nullable_bindings import (
    normalize_nullable_bindings as normalize_nullable_bindings,
)
from services.ontology_contracts.sparql_syntax import parse_cq_query


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _has_external_dataset(node: Any) -> bool:
    if getattr(node, "name", None) in {"ServiceGraphPattern", "DatasetClause"}:
        return True
    if isinstance(node, dict):
        return any(_has_external_dataset(value) for value in node.values())
    if isinstance(node, list | tuple | ParseResults):
        return any(_has_external_dataset(value) for value in node)
    return False


def normalize_cq_bindings(
    raw: Any, *, question_ids: list[str], cases: list[dict[str, Any]], fields: list[str]
) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - set(question_ids):
        raise CQBindingError("cq_bindings must reference this query's business_question_ids")
    cases_by_id = {case["id"]: case for case in cases}
    normalized = {}
    allowed = {
        "validation_case_id",
        "answer_mode",
        "answer_scope_zh",
        "source_refs",
        "boundary_assertions",
        "required_business_dimensions",
        "reasoning_capability",
        "derived_predicates",
        "cq_sparql",
        "nullable_bindings",
    }
    for question_id, value in raw.items():
        if not isinstance(value, dict) or set(value) - allowed:
            raise CQBindingError(f"invalid cq_bindings fields: {question_id}")
        binding = copy.deepcopy(value)
        case_id = str(binding.get("validation_case_id") or "")
        case = cases_by_id.get(case_id)
        exact = normalize_expected_rows(case, fields=fields) if case is not None else {}
        if case is None or (not case["expected_first_row"] and not exact):
            raise CQBindingError(
                f"CQ requires a real validation case with expectations: {question_id}"
            )
        mode = str(binding.get("answer_mode") or "")
        if mode not in {"FACT_QUERY", "EVIDENCE_QUERY", "RULE_INFERENCE", "OWL_INFERENCE"}:
            raise CQBindingError(f"CQ answer_mode is invalid: {question_id}")
        if not re.search(r"[\u3400-\u9fff]", str(binding.get("answer_scope_zh") or "")):
            raise CQBindingError(f"CQ requires a Chinese answer scope: {question_id}")
        refs = binding.get("source_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(x, str) or not x.strip() for x in refs)
        ):
            raise CQBindingError(f"CQ requires source_refs: {question_id}")
        assertions = binding.get("boundary_assertions", [])
        if not isinstance(assertions, list) or (not assertions and not exact):
            raise CQBindingError(f"CQ requires explicit boundary_assertions: {question_id}")
        binding["boundary_assertions"] = assertions
        for assertion in assertions:
            if not isinstance(assertion, dict) or set(assertion) - {
                "binding",
                "operator",
                "expected",
                "row",
                "tolerance",
            }:
                raise CQBindingError(f"invalid CQ boundary assertion: {question_id}")
            if assertion.get("binding") not in fields or "expected" not in assertion:
                raise CQBindingError(
                    f"CQ boundary must reference an actual result field: {question_id}"
                )
            if assertion.get("operator") not in {
                "EQ",
                "NE",
                "GT",
                "GTE",
                "LT",
                "LTE",
                "APPROX",
                "CONTAINS",
            } or assertion.get("row", "FIRST") not in {"FIRST", "ANY", "ALL"}:
                raise CQBindingError(f"invalid CQ boundary operator/row: {question_id}")
            if assertion.get("operator") == "NE" and assertion["expected"] == "":
                raise CQBindingError(
                    f"CQ boundary cannot be a non-empty placeholder: {question_id}"
                )
            if assertion.get("operator") == "APPROX":
                try:
                    tolerance = Decimal(str(assertion.get("tolerance")))
                    if not tolerance.is_finite() or tolerance < 0:
                        raise InvalidOperation
                except (InvalidOperation, ValueError) as exc:
                    raise CQBindingError(f"invalid CQ boundary tolerance: {question_id}") from exc
        if mode in {"RULE_INFERENCE", "OWL_INFERENCE"}:
            predicates = binding.get("derived_predicates")
            if (
                not binding.get("reasoning_capability")
                or not isinstance(predicates, list)
                or not predicates
                or any(not isinstance(x, str) or not x for x in predicates)
                or not str(binding.get("cq_sparql") or "").strip()
            ):
                raise CQBindingError(
                    f"inference CQ requires capability, predicates and actual conclusion query: {question_id}"
                )
        elif any(
            binding.get(key) for key in ("reasoning_capability", "derived_predicates", "cq_sparql")
        ):
            raise CQBindingError(
                f"fact CQ cannot replace its reviewed runtime query: {question_id}"
            )
        dimensions = binding.get("required_business_dimensions", [])
        if not isinstance(dimensions, list) or any(not isinstance(x, dict) for x in dimensions):
            raise CQBindingError(f"invalid CQ business dimensions: {question_id}")
        case_nullable = case.get("nullable_bindings") or {}
        binding_nullable = normalize_nullable_bindings(binding.get("nullable_bindings"), case["expected_fields"])
        if any(field in binding_nullable and binding_nullable[field] != condition
               for field, condition in case_nullable.items()):
            raise CQBindingError(f"CQ and validation case have conflicting nullable conditions: {question_id}")
        if case_nullable or "nullable_bindings" in binding:
            binding["nullable_bindings"] = normalize_nullable_bindings(
                {**case_nullable, **binding_nullable}, case["expected_fields"]
            )
        normalized[question_id] = binding
    return normalized


def compile_reviewed_cq(
    *, question_id: str, query_name: str, query: str, capability: dict[str, Any]
) -> dict[str, Any] | None:
    # Local import keeps query-capability normalization independent of compilation.
    from .query_capabilities import render_query_parameters

    binding = (capability.get("cq_bindings") or {}).get(question_id)
    if binding is None:
        return None
    case = next(
        (
            x
            for x in capability.get("validation_cases", [])
            if x["id"] == binding["validation_case_id"]
        ),
        None,
    )
    if case is None:
        raise CQBindingError(f"CQ validation case is missing: {question_id}")
    sparql = render_query_parameters(
        binding.get("cq_sparql") or query, case["parameters"], capability
    )
    try:
        parsed = parse_cq_query(sparql)
    except CQBindingError as exc:
        raise CQBindingError(
            f"CQ query must be a complete read-only SELECT: {question_id}; {exc}",
            reason_code=exc.reason_code, field="query",
        ) from exc
    if parsed.name != "SelectQuery" or _has_external_dataset(parsed):
        raise CQBindingError(
            f"CQ query must be a complete read-only SELECT: {question_id}; external datasets and SERVICE are forbidden",
            reason_code="UNSUPPORTED_CQ_QUERY", field="query",
        )
    source = {
        "query_name": query_name,
        "question_id": question_id,
        "validation_case_id": case["id"],
        "binding_sha256": _sha(binding),
        "validation_case_sha256": _sha(case),
        "runtime_query_sha256": _sha(query),
    }
    return {
        "sparql": sparql,
        "answer_contract": {
            "answer_mode": binding["answer_mode"],
            "reasoning_capability": binding.get("reasoning_capability"),
            "derived_predicates": binding.get("derived_predicates", []),
            "required_bindings": case["expected_fields"],
            "min_rows": case["min_rows"],
            "result_assertions": [
                {"binding": field, "operator": "EQ", "expected": value, "row": "FIRST"}
                for field, value in case["expected_first_row"].items()
            ],
            "boundary_assertions": copy.deepcopy(binding["boundary_assertions"]),
            "required_business_dimensions": copy.deepcopy(
                binding.get("required_business_dimensions", [])
            ),
            "reviewed_runtime_binding": source,
            "answer_scope_zh": binding["answer_scope_zh"],
            "source_refs": list(binding["source_refs"]),
            **normalize_expected_rows(case, fields=capability.get("result_fields") or []),
            **(
                {"nullable_bindings": copy.deepcopy(binding["nullable_bindings"])}
                if "nullable_bindings" in binding
                else {}
            ),
        },
    }
