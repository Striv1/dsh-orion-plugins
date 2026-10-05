"""Static check: boolean BIND over a variable that may be unbound.

Ontop cannot rewrite BIND(<comparison or logical expr> AS ?b) when the
compared variable comes from an OPTIONAL or a single UNION branch: the SQL
term becomes IS_TRUE(NULL) and the whole SPARQL request fails with HTTP 500
during S6.  Wrapping the expression in COALESCE(expr, false), or guarding the
variable with BOUND(), keeps the query evaluable.  This module is read-only
and reports the pattern at S3 submission.
"""

from __future__ import annotations

import re
from typing import Any

from rdflib import Literal, Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from .cq_binding_guarantee import _mentioned, _nodes
from .sparql_execution import _guaranteed_bound

_BOOLEAN = {"RelationalExpression", "ConditionalOrExpression", "ConditionalAndExpression", "UnaryNot"}
_GUARDS = {"Builtin_COALESCE", "Builtin_BOUND", "Builtin_IF"}


def _unguarded_variables(expr: Any) -> set[str]:
    if isinstance(expr, CompValue):
        if expr.name in _GUARDS:
            return set()
        return set().union(set(), *(_unguarded_variables(v) for k, v in expr.items() if k != "_vars"))
    if isinstance(expr, list | tuple):
        return set().union(set(), *(_unguarded_variables(v) for v in expr))
    return _mentioned(expr)


def nullable_boolean_binds(sparql: str) -> list[dict[str, Any]]:
    """[{target, variables}] for boolean BINDs over possibly-unbound variables."""
    try:
        algebra = translateQuery(parseQuery(sparql)).algebra
    except Exception:
        return []  # grammar gates report syntax separately
    found = []
    for node in _nodes(algebra):
        if node.name != "Extend" or not isinstance(node.get("expr"), CompValue):
            continue
        expr = node["expr"]
        if expr.name not in _BOOLEAN:
            continue
        try:
            guaranteed = {str(v) for v in _guaranteed_bound(node.p)}
        except Exception:
            continue
        loose = sorted(_unguarded_variables(expr) - guaranteed)
        if loose:
            found.append({"target": str(node.get("var")), "variables": loose})
    return found


def _query_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("sparql") or value.get("query")
    return value if isinstance(value, str) and value.strip() else None


def nullable_boolean_bind_issues(runtime: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime, dict):
        return []
    candidates: list[tuple[str, str]] = []
    queries = runtime.get("ontop_queries")
    for name, value in (queries or {}).items() if isinstance(queries, dict) else []:
        text = _query_text(value)
        if text:
            candidates.append((f"realtime_runtime.ontop_queries.{name}", text))
    for kind in ("reasoning_capabilities", "query_capabilities"):
        caps = runtime.get(kind)
        for name, cap in (caps or {}).items() if isinstance(caps, dict) else []:
            bindings = cap.get("cq_bindings") if isinstance(cap, dict) else None
            for qid, binding in (bindings or {}).items() if isinstance(bindings, dict) else []:
                text = _query_text(binding.get("cq_sparql") if isinstance(binding, dict) else None)
                if text:
                    candidates.append((f"realtime_runtime.{kind}.{name}.cq_bindings.{qid}.cq_sparql", text))
    issues = temporal_type_mismatch_issues(runtime, candidates)
    for path, text in candidates:
        for hit in nullable_boolean_binds(text):
            names = ", ".join("?" + v for v in hit["variables"])
            issues.append({
                "gate": "G-S3-ONTOP-NULLABLE-BOOLEAN-BIND",
                "reason_code": "ONTOP_NULLABLE_BOOLEAN_BIND",
                "owner": "ENGINEERING_AGENT",
                "path": path,
                "bind_target": "?" + hit["target"],
                "unbound_variables": hit["variables"],
                "message": (
                    f"BIND 生成布尔值 ?{hit['target']} 时比较了可能未绑定的变量 {names}"
                    "（来自 OPTIONAL 或 UNION 单侧），存在 Ontop 空值改写失败风险。"
                    "缺失应保留未知时，将来源属性模式与 BIND 放在同一个 OPTIONAL 组内，"
                    "让参与比较的变量在该组内必然绑定；组外缺失属性及条件仍未绑定。"
                    f"只有业务明确把缺失视为不成立时，才使用 BIND(COALESCE(<原表达式>, false) AS ?{hit['target']})。"
                    "受管生成内容应修改业务计划后重新编译，不得手改生成查询；"
                    "若最新编译仍产生此诊断，保留现场并报告平台生成问题，不重复修改生成物。"
                ),
            })
    return issues


_TEMPORAL = {
    "http://www.w3.org/2001/XMLSchema#date": "date",
    "http://www.w3.org/2001/XMLSchema#dateTime": "dateTime",
    "http://www.w3.org/2001/XMLSchema#dateTimeStamp": "dateTime",
    "http://www.w3.org/2001/XMLSchema#time": "time",
}
_OBDA_PAIR = re.compile(r"(<[^>]+>|[A-Za-z0-9_-]*:[A-Za-z0-9_.-]+)\s+\{[^}]+\}\^\^(<[^>]+>|[A-Za-z0-9_-]*:[A-Za-z0-9_.-]+)")


def obda_property_datatypes(mapping_obda: str) -> dict[str, str]:
    """Property IRI -> datatype IRI for OBDA literal targets."""
    prefixes: dict[str, str] = {"xsd": "http://www.w3.org/2001/XMLSchema#"}
    for line in mapping_obda.split("[MappingDeclaration]", 1)[0].splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_-]*):\s+(\S+)\s*$", line)
        if match:
            prefixes[match.group(1)] = match.group(2)

    def expand(term: str) -> str:
        if term.startswith("<"):
            return term[1:-1]
        prefix, _, local = term.partition(":")
        return prefixes.get(prefix, prefix + ":") + local

    found: dict[str, str] = {}
    for line in mapping_obda.splitlines():
        if line.strip().startswith("target"):
            for prop, datatype in _OBDA_PAIR.findall(line):
                found[expand(prop)] = expand(datatype)
    return found


def temporal_type_mismatches(sparql: str, datatypes: dict[str, str]) -> list[dict[str, Any]]:
    """Comparisons between xsd:date and xsd:dateTime operands (a SPARQL type error)."""
    try:
        algebra = translateQuery(parseQuery(sparql)).algebra
    except Exception:
        return []
    kinds: dict[str, str] = {}
    for node in _nodes(algebra):
        if node.name == "BGP":
            for _s, predicate, obj in node.get("triples", []):
                kind = _TEMPORAL.get(datatypes.get(str(predicate), ""))
                if isinstance(obj, Variable) and kind:
                    kinds[str(obj)] = kind

    def kind_of(term: Any) -> str | None:
        if isinstance(term, Variable):
            return kinds.get(str(term))
        if isinstance(term, Literal) and term.datatype is not None:
            return _TEMPORAL.get(str(term.datatype))
        return None

    found = []
    for node in _nodes(algebra):
        if node.name != "RelationalExpression" or not node.get("op") or node.get("other") is None:
            continue
        left, right = kind_of(node.get("expr")), kind_of(node.get("other"))
        if left and right and left != right:
            found.append({"left": str(node.get("expr")), "right": str(node.get("other")),
                          "left_type": left, "right_type": right})
    return found


def temporal_type_mismatch_issues(runtime: dict[str, Any], candidates: list[tuple[str, str]]) -> list[dict[str, Any]]:
    datatypes = obda_property_datatypes(str(runtime.get("mapping_obda") or ""))
    if not datatypes:
        return []
    issues = []
    for path, text in candidates:
        for hit in temporal_type_mismatches(text, datatypes):
            issues.append({
                "gate": "G-S3-SPARQL-TEMPORAL-TYPE-MISMATCH",
                "reason_code": "DATE_DATETIME_COMPARISON",
                "owner": "ENGINEERING_AGENT",
                "path": path,
                "operands": [hit["left"], hit["right"]],
                "message": (
                    f"查询把 {hit['left_type']} 值 ?{hit['left']} 与 {hit['right_type']} 值 ?{hit['right']} 直接比较。"
                    "SPARQL 中 xsd:date 与 xsd:dateTime 不可比较，结果是类型错误：放在 FILTER 里会丢掉全部行，"
                    "被 COALESCE(...,false) 包裹时会静默变成 false，规则永不命中，S6 才以答案不符暴露。"
                    "请统一口径：在 S3 映射中把两者映射为同一日期类型，或在查询中比较同精度的词法值，"
                    "例如 STR(?日期) < SUBSTR(STR(?日期时间), 1, 10)。"
                ),
            })
    return issues
