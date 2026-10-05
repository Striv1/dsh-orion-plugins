"""Static check: POSITIVE reasoning rules whose premises hold on every evidence row.

A fact_binding without a when condition asserts its predicate for every
evidence row.  If every positive premise of a rule is such an unconditional
binding, the evidence query cannot exclude rows by value (no FILTER, VALUES,
MINUS or constant object), and at least one premise is a judgement that the
query does not assert as a triple, the rule concludes on every candidate record: the
judgement is not derived from source data, and S6 fails a CQ boundary
assertion long after S3.  This module is read-only and reports the gap at S3.
"""

from __future__ import annotations

import re
from typing import Any

from rdflib import RDF, Literal, URIRef
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

_ATOM = re.compile(r"([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(")
_RESTRICTING_NODES = {"Filter", "Minus", "values", "ToMultiSet"}


def query_restricts_rows(sparql: str) -> bool:
    """True when the query can exclude candidate rows by value; unparsable counts as restricted."""
    try:
        algebra = translateQuery(parseQuery(sparql)).algebra
    except Exception:
        return True

    def walk(node: Any) -> bool:
        if isinstance(node, CompValue):
            if node.name in _RESTRICTING_NODES:
                return True
            if node.name == "BGP":
                return any(
                    isinstance(obj, Literal) or (isinstance(obj, URIRef) and predicate != RDF.type)
                    for _subject, predicate, obj in node.get("triples", [])
                )
            return any(walk(value) for key, value in node.items() if key != "_vars")
        if isinstance(node, list | tuple):
            return any(walk(item) for item in node)
        return False

    return walk(algebra)


_GENERIC_WORDS = re.compile(r"(record|entity|instance|item|fact|node|row)", re.IGNORECASE)


def _asserting_triples(sparql: str) -> list[tuple[Any, Any, Any]]:
    """Triples whose match binds the row (MINUS and FILTER expressions excluded).

    An OPTIONAL or UNION-branch triple still grounds a structural fact: when
    it does not match, its variables are unbound and the fact is not asserted.
    """
    try:
        algebra = translateQuery(parseQuery(sparql)).algebra
    except Exception:
        return []
    found: list[tuple[Any, Any, Any]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, CompValue) or node.name == "Minus":
            return
        if node.name == "BGP":
            found.extend(node.get("triples", []))
            return
        for key in ("p", "p1", "p2"):
            if key in node:
                walk(node[key])

    walk(algebra)
    return found


def _local(iri: str) -> str:
    return re.split(r"[#/]", iri.rstrip("#/"))[-1]


def _grounded(binding: dict[str, Any], iri: str, triples: list[tuple[Any, Any, Any]]) -> bool:
    """A premise is structural when the evidence query itself asserts exactly that fact.

    Binary: a mandatory triple (?arg0 term ?arg1).  Unary: a mandatory
    (?arg0 a Class) whose class name is the premise name, optionally with a
    generic suffix such as Record.  Anything else is a judgement that must be
    decided by a when condition or a query restriction.
    """
    arguments = binding.get("arguments") or []
    fields = [str(a.get("field")) for a in arguments if isinstance(a, dict) and isinstance(a.get("field"), str)]
    if not iri or len(fields) != len(arguments):
        return False
    names = {(str(s), str(p), str(o)) for s, p, o in triples}
    if len(fields) == 2:
        return (fields[0], iri, fields[1]) in names
    if len(fields) == 1 and (fields[0], str(RDF.type), iri) in names:
        rest = str(binding.get("predicate") or "").lower().replace(_local(iri).lower(), "", 1)
        return not _GENERIC_WORDS.sub("", rest)
    return False


def _query_text(runtime: dict[str, Any], name: str) -> str | None:
    queries = runtime.get("ontop_queries")
    value = queries.get(name) if isinstance(queries, dict) else None
    if isinstance(value, dict):
        value = value.get("sparql") or value.get("query")
    return value if isinstance(value, str) and value.strip() else None


def tautological_rule_premise_issues(runtime: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime, dict) or not isinstance(runtime.get("reasoning_capabilities"), dict):
        return []
    document_queries = runtime.get("document_fact_queries")
    document_names = set(document_queries) if isinstance(document_queries, dict) else set()
    issues: list[dict[str, Any]] = []
    for name, cap in runtime["reasoning_capabilities"].items():
        if not isinstance(cap, dict) or not isinstance(cap.get("rules"), list):
            continue
        validation = cap.get("runtime_validation") if isinstance(cap.get("runtime_validation"), dict) else {}
        if str(validation.get("expected_live_outcome") or "POSITIVE").upper() != "POSITIVE":
            continue
        evidence = str(cap.get("evidence_query") or "")
        query = _query_text(runtime, evidence)
        if evidence in document_names or query is None or query_restricts_rows(query):
            continue
        bindings = [b for b in cap.get("fact_bindings") or [] if isinstance(b, dict)]
        conditional = {str(b.get("predicate")) for b in bindings if b.get("when") is not None}
        unconditional = {str(b.get("predicate")) for b in bindings if b.get("when") is None} - conditional
        terms = cap.get("ontology_terms") if isinstance(cap.get("ontology_terms"), dict) else {}
        triples = _asserting_triples(query)
        by_predicate = {str(b.get("predicate")): b for b in bindings if b.get("when") is None}
        for rule in cap["rules"]:
            if not isinstance(rule, dict):
                continue
            matched = re.match(r"^\s*IF\s+(.+?)\s+THEN\s+", str(rule.get("expression") or ""), re.IGNORECASE)
            if matched is None or re.search(r"\bNOT\s", matched.group(1), re.IGNORECASE):
                continue
            premises = _ATOM.findall(matched.group(1))
            if not premises or not set(premises) <= unconditional:
                continue
            judgements = sorted({
                p for p in premises
                if not _grounded(by_predicate[p], str(terms.get(p) or ""), triples)
            })
            if not judgements:
                continue
            issues.append({
                "gate": "G-S3-RULE-PREMISE-TAUTOLOGY",
                "reason_code": "UNCONDITIONAL_JUDGEMENT_PREMISE",
                "owner": "ENGINEERING_AGENT",
                "path": f"realtime_runtime.reasoning_capabilities.{name}.fact_bindings",
                "reasoning_capability": name,
                "rule_id": rule.get("rule_id"),
                "unconditional_judgement_predicates": judgements,
                "message": (
                    f"推理能力 {name} 的规则 {rule.get('rule_id')} 恒真：判定前提 {', '.join(judgements)} "
                    "在证据查询中没有对应的三元组（?arg0 本体术语 ?arg1，或 ?arg0 a 同名类），"
                    "在 fact_bindings 中也没有 when 条件，而证据查询 "
                    f"{evidence} 不含 FILTER/VALUES/MINUS/常量约束——每条证据行都会被判定为满足，"
                    "规则结论会覆盖全部候选记录，S6 的 CQ 边界断言必然失败。请让证据查询带出判定所需字段，"
                    "把判定写成 fact_bindings[].when，例如 "
                    '{"predicate":"CustomerLicenseInvalid","arguments":[...],'
                    '"when":{"any":[{"field":"licenseNo","missing":true},{"field":"licenseStatus","in":["EXPIRED"]}]}}'
                    "（支持 equals/in/not_in/missing 与 all/any 组合）；需要比较日期或数值时，先在证据查询中用 "
                    "BIND((?licenseExpiry < ?orderTime) AS ?licenseExpired) 算出布尔字段，再用 "
                    '{"field":"licenseExpired","equals":true} 作为 when；或在证据查询中用 FILTER 限定判定条件。'
                ),
            })
    return issues
