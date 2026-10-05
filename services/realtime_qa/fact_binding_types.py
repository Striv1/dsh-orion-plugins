"""Reject provable RDF resource/literal mismatches before freezing a rule design.

This is a structural check of declared query terms, not a semantic guess from
variable names. Unknown expressions remain subject to the S6/S7 value checks.
"""
from __future__ import annotations

from typing import Any

from rdflib import RDF, Literal, URIRef, Variable
from rdflib.plugins.sparql import prepareQuery


def _field_kinds(node: Any, kinds: dict[str, str]) -> dict[str, set[str]]:
    if not isinstance(node, dict):
        return {}
    name = getattr(node, "name", None)
    if name == "BGP":
        fields: dict[str, set[str]] = {}

        def add(term: Any, kind: str) -> None:
            if isinstance(term, Variable):
                fields.setdefault(str(term), set()).add(kind)

        for subject, predicate, obj in node["triples"]:
            add(subject, "RESOURCE")
            add(predicate, "RESOURCE")
            role = kinds.get(str(predicate))
            add(obj, "RESOURCE" if predicate == RDF.type or role == "OBJECT_PROPERTY"
                else "LITERAL" if role == "DATA_PROPERTY" else "UNKNOWN")
        return fields
    if name in {"Join", "LeftJoin", "Union"}:
        left, right = _field_kinds(node["p1"], kinds), _field_kinds(node["p2"], kinds)
        return {field: left.get(field, set()) | right.get(field, set()) for field in left.keys() | right.keys()}
    if name in {"SelectQuery", "Project", "Distinct", "Reduced", "OrderBy", "Slice", "Filter", "Extend", "ToMultiSet"}:
        fields = _field_kinds(node["p"], kinds)
        if name == "Project":
            return {str(v): fields.get(str(v), {"UNKNOWN"}) for v in node["PV"]}
        if name == "Extend":
            expr = node["expr"]
            fields[str(node["var"])] = (
                fields.get(str(expr), {"UNKNOWN"}) if isinstance(expr, Variable)
                else {"RESOURCE"} if isinstance(expr, URIRef)
                else {"LITERAL"} if isinstance(expr, Literal) or getattr(expr, "name", None) == "Builtin_STR"
                else {"UNKNOWN"}
            )
        return fields
    return {}


def fact_binding_type_issues(runtime: dict[str, Any], term_kinds: dict[str, str]) -> list[dict[str, Any]]:
    issues = []
    queries = runtime.get("ontop_queries") or {}
    capabilities = runtime.get("reasoning_capabilities") or {}
    if not isinstance(queries, dict) or not isinstance(capabilities, dict):
        return issues
    for name, capability in capabilities.items():
        if not isinstance(capability, dict):
            continue
        evidence = capability.get("evidence_query")
        if not isinstance(evidence, str):
            continue
        query = queries.get(evidence)
        if not isinstance(query, str):
            continue
        try:
            fields = _field_kinds(prepareQuery(query).algebra, term_kinds)
        except Exception:
            continue  # Query syntax and template parameters have separate gates.
        terms = capability.get("ontology_terms") or {}
        bindings = capability.get("fact_bindings") or []
        if not isinstance(terms, dict) or not isinstance(bindings, list):
            continue
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                continue
            predicate = binding.get("predicate")
            if not isinstance(predicate, str):
                continue
            iri = terms.get(predicate)
            args = binding.get("arguments") or []
            if (not isinstance(iri, str) or term_kinds.get(iri) != "DATA_PROPERTY"
                or not isinstance(args, list) or len(args) != 2 or not isinstance(args[1], dict)):
                continue
            field = args[1].get("field")
            if not isinstance(field, str) or fields.get(field) != {"RESOURCE"}:
                continue
            issues.append({
                "gate": "G-S3-FACT-TYPE", "reason_code": "RESOURCE_AS_DATA_PROPERTY_VALUE",
                "owner": "ENGINEERING_AGENT", "reasoning_capability": name,
                "predicate": predicate, "ontology_iri": iri, "argument_index": 2,
                "evidence_query": capability.get("evidence_query"), "field": field,
                "path": f"realtime_runtime.reasoning_capabilities.{name}.fact_bindings[{index}].arguments[1]",
                "message": f"推理能力 {name} 的事实 {predicate} 映射到数据属性 {iri}，"
                f"但第 2 个参数 ?{field} 在证据查询中是实体。数据属性需要字面值；"
                "实体关系或条件断言须声明符合业务含义的关系/类，不能借用已有属性 IRI。",
            })
    return issues


def validate_fact_binding_types(runtime: dict[str, Any], term_kinds: dict[str, str]) -> None:
    """Frozen/runtime boundary, using the same diagnostics as S3 preflight."""
    issues = fact_binding_type_issues(runtime, term_kinds)
    if issues:
        from services.ontology_engineering.formal_facts import FormalFactContractError

        first = issues[0]
        raise FormalFactContractError(
            "\n".join(issue["message"] for issue in issues),
            predicate=first["predicate"], ontology_iri=first["ontology_iri"],
            argument_index=first["argument_index"], capability_name=first["reasoning_capability"],
        )
