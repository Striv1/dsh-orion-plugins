"""Detect query-created nulls used as proof that a source value is missing.

An OPTIONAL lookup can observe absence. A UNION branch which never reads the
field cannot: it also yields an unbound value for entities that have that field.
This is a bounded structural diagnostic, not a proof of arbitrary SPARQL or of
source completeness. Unknown algebra stays subject to normal runtime checks.
"""
from __future__ import annotations

from typing import Any

from rdflib import Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue


def _variables(value: Any) -> set[str]:
    if isinstance(value, Variable):
        return {str(value)}
    if isinstance(value, CompValue):
        return set().union(set(), *(_variables(v) for k, v in value.items() if k != "_vars"))
    if isinstance(value, list | tuple):
        return set().union(set(), *(_variables(v) for v in value))
    return set()


def _has_exists(value: Any) -> bool:
    if isinstance(value, CompValue):
        return value.name in {"Builtin_EXISTS", "Builtin_NOTEXISTS"} or any(
            _has_exists(v) for k, v in value.items() if k != "_vars")
    if isinstance(value, list | tuple):
        return any(_has_exists(v) for v in value)
    return False


def _observed_paths(node: CompValue) -> list[set[str]] | None:
    """Bounded alternatives, retaining fields that guard a fact's arguments.

    A fact with an unbound argument is never emitted. Keep UNION alternatives
    separate so a vehicle-only branch cannot invalidate a carrier-only fact.
    OPTIONAL fields count as observed even if the lookup returns no value.
    """
    if node.name == "BGP":
        return [_variables(node.triples)]
    if node.name in {"Join", "LeftJoin", "Union"}:
        left, right = _observed_paths(node.p1), _observed_paths(node.p2)
        if left is None or right is None:
            return None
        if node.name == "Union":
            return left + right if len(left) + len(right) <= 64 else None
        return [a | b for a in left for b in right] if len(left) * len(right) <= 64 else None
    if node.name in {"SelectQuery", "Project", "Distinct", "Reduced", "OrderBy", "Slice", "Filter", "Extend"}:
        if node.name == "Filter" and _has_exists(node.expr):
            return None  # A correlated absence proof needs separate runtime validation.
        paths = _observed_paths(node.p)
        if paths is None:
            return None
        if node.name == "Project":
            return [fields & {str(v) for v in node.PV} for fields in paths]
        if node.name == "Extend":
            return [fields | {str(node.var)} if _variables(node.expr) <= fields else fields for fields in paths]
        # FILTER reads a solution, it does not look up missing source fields.
        return paths
    return None


def _absence_fields(condition: Any) -> set[str]:
    if not isinstance(condition, dict):
        return set()
    if condition.get("missing") is True and isinstance(condition.get("field"), str):
        return {condition["field"]}
    return set().union(set(), *(
        _absence_fields(child) for key in ("all", "any")
        if isinstance(condition.get(key), list) for child in condition[key]
    ))


def evidence_absence_issues(runtime: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime, dict):
        return []
    capabilities, queries = runtime.get("reasoning_capabilities"), runtime.get("ontop_queries")
    if not isinstance(capabilities, dict) or not isinstance(queries, dict):
        return []
    issues = []
    for name, capability in capabilities.items():
        if not isinstance(capability, dict):
            continue
        evidence = capability.get("evidence_query")
        if not isinstance(evidence, str):
            continue
        query = queries.get(evidence)
        if not isinstance(query, str):
            continue  # Document facts and artifact descriptors have no inline query.
        bindings = capability.get("fact_bindings") or []
        if not isinstance(bindings, list):
            continue
        requested = set().union(set(), *(
            _absence_fields(b.get("when")) for b in bindings if isinstance(b, dict)
        ))
        if not requested:
            continue
        try:
            paths = _observed_paths(prepareQuery(query).algebra)
        except Exception:
            continue  # Syntax/parameter validation belongs to the existing query gates.
        if paths is None:
            continue
        missing = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            fields = _absence_fields(binding.get("when"))
            arguments = {str(a["field"]) for a in binding.get("arguments", [])
                         if isinstance(a, dict) and "field" in a}
            for observed in paths:
                if arguments <= observed:
                    missing.update(fields - observed)
        if not missing:
            continue
        missing = sorted(missing)
        issues.append({
            "gate": "G-S3-REASONING-ABSENCE-EVIDENCE",
            "reason_code": "QUERY_UNBOUND_IS_NOT_SOURCE_ABSENCE",
            "owner": "ENGINEERING_AGENT",
            "path": f"realtime_runtime.ontop_queries.{evidence}",
            "reasoning_capability": name,
            "evidence_query": evidence,
            "unobserved_fields": missing,
            "message": (
                f"推理能力 {name} 用 missing:true 判定 {', '.join(missing)} 缺失，"
                f"但证据查询 {evidence} 的某些路径没有读取这些字段。UNION 分支漏取字段产生的未绑定值"
                "不代表来源缺失，会把有值的正常对象也判为缺失。请在共同主体上用 OPTIONAL 读取字段，"
                "或使每个 UNION 分支都执行对应的来源查找；保留原业务规则与验收期望，再预检。"
            ),
        })
    return issues


def validate_evidence_absence(capabilities: dict[str, Any], queries: dict[str, str]) -> None:
    issues = evidence_absence_issues({"reasoning_capabilities": capabilities, "ontop_queries": queries})
    if issues:
        raise ValueError(" | ".join(f"{item['path']}: {item['message']}" for item in issues))
