"""Rule-CQ graph closure: S7/live QA run reasoning CQs on released ontology + rule facts only.

S6 additionally holds Ontop instances, so a CQ that joins source-only terms passes
S6 locally yet returns zero rows after release. New S3 submissions must make every
queried ontology term producible by the capability's own facts or conclusions.
"""
from __future__ import annotations

from typing import Any

from rdflib import URIRef
from rdflib.paths import Path

BUILTIN_NAMESPACES = (
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2001/XMLSchema#",
)


def sparql_pattern_iris(query: str) -> set[str]:
    """Constant IRIs in graph patterns (incl. subqueries/OPTIONAL), excluding built-ins."""
    from rdflib.plugins.sparql.algebra import translateQuery
    from rdflib.plugins.sparql.parser import parseQuery

    found: set[str] = set()

    def collect(term: Any) -> None:
        if isinstance(term, URIRef):
            found.add(str(term))
        elif isinstance(term, Path):
            for part in vars(term).values():
                collect(part)
        elif isinstance(term, tuple | list):
            for part in term:
                collect(part)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if getattr(node, "name", None) == "BGP":
            for triple in node.get("triples", []):
                collect(triple)
        for value in node.values():
            if isinstance(value, dict):
                walk(value)

    walk(translateQuery(parseQuery(query)).algebra)
    return {iri for iri in found if not iri.startswith(BUILTIN_NAMESPACES)}


def rule_cq_graph_gaps(capability_name: str, capability: dict[str, Any]) -> list[str]:
    from .cq_contract import compile_reviewed_cq

    terms = capability.get("ontology_terms") or {}
    produced = {str(terms.get(binding.get("predicate")) or "") for binding in capability.get("fact_bindings") or []}
    produced |= {str(terms.get(predicate) or "") for predicate in capability.get("result_predicates") or []}
    produced.discard("")
    gaps = []
    for question_id, binding in sorted((capability.get("cq_bindings") or {}).items()):
        compiled = compile_reviewed_cq(
            question_id=question_id, query_name=capability_name,
            query=binding["cq_sparql"], capability=capability,
        )
        missing = sorted(sparql_pattern_iris(compiled["sparql"]) - produced) if compiled else []
        if missing:
            gaps.append(f"{question_id}: " + ", ".join(missing))
    return gaps


def validate_rule_cq_graph_closure(reasoning_capabilities: dict[str, Any]) -> None:
    problems = []
    for name, capability in sorted(reasoning_capabilities.items()):
        for gap in rule_cq_graph_gaps(name, capability):
            problems.append(f"reasoning_capabilities.{name}.cq_bindings.{gap}")
    if problems:
        raise ValueError(
            "规则 CQ 引用了本推理能力不会产出的本体项。S7 部署验证与实时问答只在"
            "“已发布本体 + 本能力 fact_bindings 事实 + 规则结论”上执行 CQ，不含 Ontop 实例，"
            "这些模式发布后必然 0 行：" + "；".join(problems)
            + "。修正方式：为每个缺失项在 fact_bindings 增加事实绑定并在 ontology_terms 登记 IRI"
            "（属性二元，例如 {predicate:'equipmentCode', arguments:[{field:'eq'},{field:'equipmentCode'}]}；"
            "类一元），evidence_query 需投影对应字段；主语优先使用 evidence_query 投影的实体 IRI 变量（如 ?eq），"
            "使规则结论与数据实例同一身份；或从 cq_sparql 删除该模式、改由结构化 FACT_QUERY 回答。"
        )
