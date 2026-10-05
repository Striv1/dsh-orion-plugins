"""Bounded, source-row predicates for conditional reasoning fact bindings."""

from __future__ import annotations

from typing import Any

LEAF_OPERATORS = ("equals", "in", "not_in", "missing")


def _leaf_operator(value: dict[str, Any]) -> str | None:
    keys = set(value)
    if "field" not in keys or len(keys) != 2:
        return None
    operator = next(iter(keys - {"field"}))
    return operator if operator in LEAF_OPERATORS else None


def normalize_row_condition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("条件必须是结构化对象")
    if set(value) in ({"all"}, {"any"}):
        operator = next(iter(value))
        children = value[operator]
        if not isinstance(children, list) or not 1 <= len(children) <= 8:
            raise ValueError("条件组必须包含 1 到 8 个叶子条件")
        if any(not isinstance(child, dict) or _leaf_operator(child) is None for child in children):
            raise ValueError("条件组仅支持来源列的 equals、in、not_in 或 missing 判断")
        return {operator: [normalize_row_condition(child) for child in children]}
    operator = _leaf_operator(value)
    if operator is None:
        raise ValueError("条件叶子必须提供 field 与 equals、in、not_in 或 missing 之一")
    field = value["field"]
    if not isinstance(field, str) or not field.strip():
        raise ValueError("条件 field 必须是非空来源列名")
    operand = value[operator]
    if operator in {"in", "not_in"}:
        if not isinstance(operand, list) or not 1 <= len(operand) <= 20:
            raise ValueError(f"条件 {operator} 必须包含 1 到 20 个值")
        if any(not isinstance(item, str | int | float | bool) for item in operand):
            raise ValueError(f"条件 {operator} 只接受标量值")
    elif operator == "missing":
        if not isinstance(operand, bool):
            raise ValueError("条件 missing 只接受 true 或 false")
    elif not isinstance(operand, str | int | float | bool):
        raise ValueError("条件 equals 只接受标量值")
    return {"field": field, operator: operand}


def row_matches_condition(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    if "all" in condition:
        return all(row_matches_condition(row, child) for child in condition["all"])
    if "any" in condition:
        return any(row_matches_condition(row, child) for child in condition["any"])
    actual = row.get(condition["field"])
    if "missing" in condition:
        # Unbound OPTIONAL columns and blank strings both mean "no value".
        absent = actual is None or (isinstance(actual, str) and not actual.strip())
        return absent is condition["missing"]
    if actual is None:
        return False
    if "not_in" in condition:
        return not any(_condition_equal(actual, item) for item in condition["not_in"])
    expected = condition.get("equals")
    values = condition.get("in", [expected])
    return any(_condition_equal(actual, item) for item in values)


def validate_fact_source_fields(
    reasoning_capabilities: dict[str, dict[str, Any]],
    query_capabilities: dict[str, dict[str, Any]],
) -> None:
    """Reject source bindings that cannot be supplied by the declared query."""

    problems: list[str] = []
    for capability_name, capability in reasoning_capabilities.items():
        query = query_capabilities.get(capability["evidence_query"])
        if query is None:
            continue  # Document facts have positional arguments, not row fields.
        available = set(query.get("result_fields") or [])
        if not available:
            continue  # Legacy query contracts did not declare a result schema.
        missing_by_binding: list[str] = []
        for index, binding in enumerate(capability["fact_bindings"]):
            required = {
                source["field"] for source in binding["arguments"]
                if isinstance(source.get("field"), str)
            }
            condition = binding.get("when")
            if condition:
                leaves = condition.get("all", condition.get("any", [condition]))
                required.update(leaf["field"] for leaf in leaves)
            missing = required - available
            if missing:
                missing_by_binding.append(f"fact_bindings[{index}] 缺 " + ", ".join(sorted(missing)))
        if missing_by_binding:
            problems.append(
                f"reasoning_capabilities.{capability_name}（evidence_query={capability['evidence_query']}，"
                f"可用 result_fields: {', '.join(sorted(available))}）：" + "；".join(missing_by_binding)
            )
    if problems:
        raise ValueError(
            "fact_bindings references fields absent from evidence_query result_fields（已一次列出全部缺口）："
            + " | ".join(problems)
            + "。修正方式：在对应 query_capabilities.<evidence_query>.result_fields 与 SPARQL SELECT 中投影缺失字段，"
            "或把绑定改为已投影字段；修完全部缺口后再预检。"
        )


def _condition_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return str(actual).strip().lower() in ({"true", "1"} if expected else {"false", "0"})
    return actual == expected or str(actual) == str(expected)
