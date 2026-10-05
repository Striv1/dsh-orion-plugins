from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.ontology_contracts.cq_answers import normalize_expected_rows
from services.ontology_contracts.nullable_bindings import normalize_nullable_bindings

from .cq_contract import CQBindingError, normalize_cq_bindings

CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
SOURCE_TABLE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_$-]{0,127})?$"
)
SOURCE_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")
PLACEHOLDER = re.compile(r"{{([a-z][a-z0-9_]{0,63})}}")
OPTIONAL_BLOCK = re.compile(
    r"{{#([a-z][a-z0-9_]{0,63})}}(.*?){{/\1}}",
    re.DOTALL,
)
COMPARISON_OPERATORS = {
    "EQ": "=",
    "NE": "!=",
    "GT": ">",
    "GTE": ">=",
    "LT": "<",
    "LTE": "<=",
}
PARAMETER_TYPES = {
    "boolean",
    "code",
    "comparison",
    "date",
    "datetime",
    "decimal",
    "enum",
    "integer",
    "iri",
    "string",
}
DOCUMENT_QUERY_CAPABILITIES = {
    "current_full_text_search",
    "reviewed_entity_evidence",
}


class QueryCapabilityError(ValueError):
    pass


def normalize_document_query_capabilities(
    raw: Any,
    *,
    legacy_default: bool = False,
) -> list[str]:
    """Validate document read capabilities carried by one immutable release."""

    if raw is None:
        return ["reviewed_entity_evidence"] if legacy_default else sorted(
            DOCUMENT_QUERY_CAPABILITIES
        )
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise QueryCapabilityError("document_query_capabilities must be a string list")
    normalized = list(dict.fromkeys(item.strip() for item in raw if item.strip()))
    unknown = set(normalized) - DOCUMENT_QUERY_CAPABILITIES
    if unknown:
        raise QueryCapabilityError(
            "unsupported document query capabilities: " + ", ".join(sorted(unknown))
        )
    return sorted(normalized)


def normalize_query_capabilities(
    raw: Any,
    query_names: set[str],
    *,
    allow_legacy: bool = True,
) -> dict[str, dict[str, Any]]:
    """Validate the public, release-bound contract for parameterized queries.

    Older releases did not carry capability metadata.  They remain readable via
    a deliberately small legacy contract, while every new metadata-bearing
    release must describe every executable query exactly once.
    """

    if raw is None:
        if not allow_legacy:
            return {
                name: {
                    "description_zh": f"执行已评审的只读查询模板 {name}",
                    "parameters": {},
                    "result_fields": [],
                    "question_examples": [],
                    "source_scope": "structured_db",
                    "legacy": False,
                }
                for name in sorted(query_names)
            }
        return {
            name: {
                "description_zh": name,
                "parameters": {},
                "result_fields": [],
                "question_examples": [],
                "source_scope": "structured_db",
                "legacy": True,
            }
            for name in sorted(query_names)
        }
    if (
        isinstance(raw, dict)
        and set(raw) == query_names
        and all(isinstance(item, dict) and item.get("legacy") is True for item in raw.values())
    ):
        return normalize_query_capabilities(
            None,
            query_names,
            allow_legacy=allow_legacy,
        )
    if not isinstance(raw, dict) or set(raw) != query_names:
        raise QueryCapabilityError(
            "query_capabilities must describe every ontop query and no others"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for name in sorted(query_names):
        if not CAPABILITY_NAME.fullmatch(name) or not isinstance(raw[name], dict):
            raise QueryCapabilityError(f"invalid query capability: {name}")
        item = raw[name]
        description = str(item.get("description_zh") or "").strip()
        if not description:
            raise QueryCapabilityError(
                f"query capability description_zh is required: {name}"
            )
        raw_parameters = item.get("parameters") or {}
        if not isinstance(raw_parameters, dict):
            raise QueryCapabilityError(
                f"query capability parameters must be an object: {name}"
            )
        parameters = {
            parameter_name: _normalize_parameter_spec(
                name,
                parameter_name,
                parameter_spec,
            )
            for parameter_name, parameter_spec in sorted(raw_parameters.items())
        }
        result_fields = _string_list(item.get("result_fields") or [], "result_fields")
        examples = _string_list(item.get("question_examples") or [], "question_examples")
        if len(examples) > 20:
            raise QueryCapabilityError(
                f"query capability has too many question examples: {name}"
            )
        source_ids = _source_ids(item.get("source_ids"))
        source_tables_by_id = _source_scope_by_id(
            item.get("source_tables_by_id"),
            "source_tables_by_id",
            source_ids,
            SOURCE_TABLE,
        )
        source_columns_by_id = _source_scope_by_id(
            item.get("source_columns_by_id"),
            "source_columns_by_id",
            source_ids,
            SOURCE_COLUMN,
        )
        normalized[name] = {
            "description_zh": description,
            "parameters": parameters,
            "result_fields": result_fields,
            "question_examples": examples,
            "business_question_ids": _string_list(
                item.get("business_question_ids") or [],
                "business_question_ids",
            ),
            "validation_cases": _normalize_validation_cases(
                name,
                item.get("validation_cases") or [],
                result_fields,
            ),
            "source_scope": "structured_db",
            "query_mode": _query_mode(item.get("query_mode")),
            "source_ids": source_ids,
            "source_tables": _string_list(
                item.get("source_tables") or [], "source_tables"
            ),
            "source_columns": _string_list(
                item.get("source_columns") or [], "source_columns"
            ),
            "source_tables_by_id": source_tables_by_id,
            "source_columns_by_id": source_columns_by_id,
            "legacy": False,
        }
        if "cq_bindings" in item:
            try:
                normalized[name]["cq_bindings"] = normalize_cq_bindings(
                    item["cq_bindings"],
                    question_ids=normalized[name]["business_question_ids"],
                    cases=normalized[name]["validation_cases"],
                    fields=result_fields,
                )
            except CQBindingError as exc:
                raise QueryCapabilityError(str(exc)) from exc
    return normalized


def _query_mode(value: Any) -> str:
    normalized = str(value or "SNAPSHOT_ONLY").strip().upper()
    if normalized not in {"SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID"}:
        raise QueryCapabilityError(f"unsupported query_mode: {normalized}")
    return normalized


def _source_ids(value: Any) -> list[str]:
    values = _string_list(value or [], "source_ids")
    if any(not SOURCE_ID.fullmatch(item) for item in values):
        raise QueryCapabilityError("source_ids contain an invalid source id")
    if len(values) != len(set(values)):
        raise QueryCapabilityError("source_ids contain duplicates")
    return values


def _source_scope_by_id(
    value: Any,
    field_name: str,
    source_ids: list[str],
    item_pattern: re.Pattern[str],
) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise QueryCapabilityError(f"{field_name} must be an object")
    unknown = set(value) - set(source_ids)
    if unknown:
        raise QueryCapabilityError(
            f"{field_name} references undeclared source_ids: "
            + ", ".join(sorted(unknown))
        )
    normalized: dict[str, list[str]] = {}
    for source_id, raw_items in sorted(value.items()):
        if not SOURCE_ID.fullmatch(str(source_id)):
            raise QueryCapabilityError(f"{field_name} contains an invalid source id")
        items = _string_list(raw_items, field_name)
        if not items or any(not item_pattern.fullmatch(item) for item in items):
            raise QueryCapabilityError(f"{field_name} contains an invalid identifier")
        if len(items) != len(set(items)):
            raise QueryCapabilityError(f"{field_name} contains duplicate identifiers")
        normalized[str(source_id)] = items
    return normalized


def render_query_parameters(
    query: str,
    parameters: dict[str, Any],
    capability: dict[str, Any] | None,
) -> str:
    if capability is None or capability.get("legacy") is True:
        return _render_legacy(query, parameters)
    specs = capability.get("parameters") or {}
    if not isinstance(specs, dict):
        raise QueryCapabilityError("query capability parameter contract is invalid")
    unknown = set(parameters) - set(specs)
    if unknown:
        raise QueryCapabilityError(
            "unknown query parameters: " + ", ".join(sorted(unknown))
        )
    values = dict(parameters)
    for name, spec in specs.items():
        if name not in values and "default" in spec:
            values[name] = spec["default"]
        if spec.get("required") is True and name not in values:
            raise QueryCapabilityError(f"missing required query parameter: {name}")

    def optional(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in specs:
            raise QueryCapabilityError(f"optional block has no parameter spec: {name}")
        value = values.get(name)
        return match.group(2) if value is not None and value != "" else ""

    query = OPTIONAL_BLOCK.sub(optional, query)
    placeholders = set(PLACEHOLDER.findall(query))
    missing_specs = placeholders - set(specs)
    if missing_specs:
        raise QueryCapabilityError(
            "query placeholders have no parameter specs: "
            + ", ".join(sorted(missing_specs))
        )
    missing_values = placeholders - set(values)
    if missing_values:
        raise QueryCapabilityError(
            "missing query parameter values: " + ", ".join(sorted(missing_values))
        )
    rendered = {
        name: _render_value(name, values[name], specs[name]) for name in placeholders
    }
    query = PLACEHOLDER.sub(lambda match: rendered[match.group(1)], query)
    if "{{" in query or "}}" in query:
        raise QueryCapabilityError("unresolved query template directive")
    return query


def validate_query_template_contract(
    query: str,
    capability: dict[str, Any],
) -> None:
    if capability.get("legacy") is True:
        return
    specs = capability.get("parameters") or {}
    placeholders = set(PLACEHOLDER.findall(query))
    optional_controls = {match.group(1) for match in OPTIONAL_BLOCK.finditer(query)}
    if "{{#" in OPTIONAL_BLOCK.sub("", query) or "{{/" in OPTIONAL_BLOCK.sub("", query):
        raise QueryCapabilityError("query has an invalid optional parameter block")
    declared = set(specs)
    used = placeholders | optional_controls
    if used != declared:
        missing = declared - used
        unknown = used - declared
        details = []
        if missing:
            details.append("unused specs=" + ",".join(sorted(missing)))
        if unknown:
            details.append("unknown placeholders=" + ",".join(sorted(unknown)))
        raise QueryCapabilityError(
            "query parameter contract does not match template: " + "; ".join(details)
        )


def _normalize_parameter_spec(
    capability_name: str,
    parameter_name: str,
    raw: Any,
) -> dict[str, Any]:
    if not PARAMETER_NAME.fullmatch(str(parameter_name)) or not isinstance(raw, dict):
        raise QueryCapabilityError(
            f"invalid parameter spec: {capability_name}.{parameter_name}"
        )
    parameter_type = str(raw.get("type") or "").strip()
    if parameter_type not in PARAMETER_TYPES:
        raise QueryCapabilityError(
            f"unsupported parameter type: {capability_name}.{parameter_name}"
        )
    description = str(raw.get("description_zh") or "").strip()
    if not description:
        raise QueryCapabilityError(
            f"parameter description_zh is required: {capability_name}.{parameter_name}"
        )
    normalized: dict[str, Any] = {
        "type": parameter_type,
        "description_zh": description,
        "required": bool(raw.get("required", False)),
    }
    if "default" in raw:
        normalized["default"] = raw["default"]
    if parameter_type == "enum":
        values = _string_list(raw.get("values") or [], "values")
        if not values:
            raise QueryCapabilityError(
                f"enum values are required: {capability_name}.{parameter_name}"
            )
        normalized["values"] = values
    if parameter_type == "comparison":
        values = _string_list(
            raw.get("values") or list(COMPARISON_OPERATORS),
            "values",
        )
        if not values or any(value not in COMPARISON_OPERATORS for value in values):
            raise QueryCapabilityError(
                f"comparison values are invalid: {capability_name}.{parameter_name}"
            )
        normalized["values"] = values
    for key in ("minimum", "maximum", "max_length"):
        if key in raw:
            normalized[key] = raw[key]
    if "default" in normalized:
        _render_value(parameter_name, normalized["default"], normalized)
    return normalized


def _normalize_validation_cases(
    capability_name: str,
    raw: Any,
    result_fields: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 20:
        raise QueryCapabilityError(
            f"validation_cases must be a list of at most 20 items: {capability_name}"
        )
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise QueryCapabilityError(
                f"invalid validation case: {capability_name}.{index}"
            )
        case_id = str(item.get("id") or f"case_{index}").strip()
        question = str(item.get("question") or "").strip()
        parameters = item.get("parameters") or {}
        if (
            not PARAMETER_NAME.fullmatch(case_id)
            or case_id in seen_ids
            or not question
            or not isinstance(parameters, dict)
        ):
            raise QueryCapabilityError(
                f"invalid validation case contract: {capability_name}.{case_id}"
            )
        expected_fields = _string_list(
            item.get("expected_fields") or result_fields,
            "expected_fields",
        )
        unknown_fields = set(expected_fields) - set(result_fields)
        if unknown_fields:
            raise QueryCapabilityError(
                f"validation case has unknown expected fields: {capability_name}.{case_id}"
            )
        min_rows = int(item.get("min_rows", 0))
        if min_rows < 0:
            raise QueryCapabilityError(
                f"validation case min_rows is invalid: {capability_name}.{case_id}"
            )
        expected_first_row = item.get("expected_first_row") or {}
        if not isinstance(expected_first_row, dict) or (
            set(expected_first_row) - set(expected_fields)
        ):
            raise QueryCapabilityError(
                f"validation case expected_first_row is invalid: {capability_name}.{case_id}"
            )
        try:
            exact = normalize_expected_rows(item, fields=result_fields)
            nullable = (normalize_nullable_bindings(item["nullable_bindings"], expected_fields)
                        if "nullable_bindings" in item else None)
        except CQBindingError as exc:
            raise QueryCapabilityError(f"{capability_name}.{case_id}: {exc}") from exc
        if exact and (min_rows > len(exact["expected_rows"])
                      or (not exact["expected_rows"] and expected_first_row)):
            raise QueryCapabilityError(f"expected_rows contradicts min_rows/expected_first_row: {capability_name}.{case_id}")
        seen_ids.add(case_id)
        normalized.append(
            {
                "id": case_id,
                "question": question,
                "parameters": dict(parameters),
                "expected_fields": expected_fields,
                "min_rows": min_rows,
                "expected_first_row": dict(expected_first_row),
                **exact,
                **({"nullable_bindings": nullable} if nullable is not None else {}),
            }
        )
    return normalized


def _render_value(name: str, value: Any, spec: dict[str, Any]) -> str:
    parameter_type = spec["type"]
    if parameter_type == "code":
        text = str(value)
        if not CODE.fullmatch(text):
            raise QueryCapabilityError(f"invalid code parameter: {name}")
        return text
    if parameter_type == "iri":
        text = str(value)
        if not IRI.fullmatch(text):
            raise QueryCapabilityError(f"invalid IRI parameter: {name}")
        return f"<{text}>"
    if parameter_type in {"decimal", "integer"}:
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise QueryCapabilityError(f"invalid numeric parameter: {name}") from exc
        if not decimal.is_finite():
            raise QueryCapabilityError(f"numeric parameter must be finite: {name}")
        if parameter_type == "integer" and decimal != decimal.to_integral_value():
            raise QueryCapabilityError(f"integer parameter is required: {name}")
        _check_numeric_bounds(name, decimal, spec)
        return str(int(decimal)) if parameter_type == "integer" else format(decimal, "f")
    if parameter_type == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        if str(value).lower() in {"true", "false"}:
            return str(value).lower()
        raise QueryCapabilityError(f"invalid boolean parameter: {name}")
    if parameter_type == "comparison":
        token = str(value).upper()
        if token not in spec["values"]:
            raise QueryCapabilityError(f"comparison parameter is not allowed: {name}")
        return COMPARISON_OPERATORS[token]
    if parameter_type in {"date", "datetime"}:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QueryCapabilityError(f"invalid {parameter_type} parameter: {name}") from exc
        if parameter_type == "date":
            if "T" in text or " " in text:
                raise QueryCapabilityError(f"invalid date parameter: {name}")
            return f'"{parsed.date().isoformat()}"^^<http://www.w3.org/2001/XMLSchema#date>'
        return f'"{parsed.isoformat()}"^^<http://www.w3.org/2001/XMLSchema#dateTime>'
    text = str(value)
    max_length = int(spec.get("max_length") or 500)
    # An explicitly empty optional string default means no filter. Keep this
    # opt-in separate from required strings, codes and enumerated values.
    allows_empty = (
        parameter_type == "string"
        and spec.get("required") is False
        and "default" in spec
        and spec["default"] == ""
    )
    if (not text and not allows_empty) or len(text) > max_length:
        raise QueryCapabilityError(f"invalid string parameter length: {name}")
    if parameter_type == "enum" and text not in spec["values"]:
        raise QueryCapabilityError(f"enum parameter is not allowed: {name}")
    return json.dumps(text, ensure_ascii=False)


def _check_numeric_bounds(name: str, value: Decimal, spec: dict[str, Any]) -> None:
    for key, comparison in (
        ("minimum", lambda actual, bound: actual < bound),
        ("maximum", lambda actual, bound: actual > bound),
    ):
        if key not in spec:
            continue
        try:
            bound = Decimal(str(spec[key]))
        except InvalidOperation as exc:
            raise QueryCapabilityError(f"invalid {key} contract: {name}") from exc
        if comparison(value, bound):
            raise QueryCapabilityError(f"numeric parameter violates {key}: {name}")


def _render_legacy(query: str, parameters: dict[str, Any]) -> str:
    for key, value in parameters.items():
        text = str(value)
        if not CODE.fullmatch(text):
            raise QueryCapabilityError(f"invalid code parameter: {key}")
        query = query.replace("{{" + key + "}}", text)
    if "{{" in query or "}}" in query:
        raise QueryCapabilityError("missing template parameter")
    return query


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not str(item).strip() for item in value):
        raise QueryCapabilityError(f"{field} must be a list of non-empty strings")
    return [str(item).strip() for item in value]
