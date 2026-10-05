"""Static checks for SPARQL shapes Ontop 5.3 accepts but cannot serialize to SQL.

Findings are reported at S3 preflight so they are fixed while authoring the
runtime design, instead of surfacing later as an S6 HTTP 500.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from pyparsing import ParseBaseException
from rdflib import Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

GATE = "G-S3-RUNTIME-QUERY-COMPAT"
_COMPARISON_OPS = {"=", "!=", "<", ">", "<=", ">="}


def _walk(value: Any) -> Iterator[CompValue]:
    if isinstance(value, CompValue):
        yield value
        for key, item in value.items():
            if key != "_vars":
                yield from _walk(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item)


def _compares_str(expr: Any) -> bool:
    for node in _walk(expr):
        if (
            node.name == "RelationalExpression"
            and str(node.get("op")) in _COMPARISON_OPS
            and any(inner.name == "Builtin_STR" for inner in _walk([node.get("expr"), node.get("other")]))
        ):
            return True
    return False


def _group_keys(group: CompValue) -> set[Variable]:
    keys: set[Variable] = set()
    for expr in group.get("expr") or []:
        if isinstance(expr, Variable):
            keys.add(expr)
    return keys


def lint_ontop_select(query: str) -> list[str]:
    """Return human-readable incompatibility findings for one SELECT query."""
    try:
        algebra = prepareQuery(query).algebra
    except (ParseBaseException, ValueError, TypeError, KeyError, AttributeError):
        # Syntax/shape errors are reported by the dedicated query gates.
        return []
    findings: list[str] = []
    for group in _walk(algebra):
        if group.name != "Group":
            continue
        keys = _group_keys(group)
        for node in _walk(group.get("p")):
            if node.name != "Extend" or node.get("var") not in keys:
                continue
            expr = node.get("expr")
            if isinstance(expr, CompValue) and expr.name == "Builtin_IF" and _compares_str(expr.get("arg1")):
                findings.append(
                    f"分组键 ?{node['var']} 由 BIND(IF(...)) 生成且条件含 STR() 比较；"
                    "Ontop 5.3 在 GROUP BY 下会返回 HTTP 500"
                    "（SQLSerializationException: Only DBFunctionSymbols must be provided）。"
                    "请把空值/空串判断移到 OPTIONAL 内，例如 "
                    'OPTIONAL { ?s :p ?raw . FILTER(STR(?raw) != "") } '
                    'BIND(COALESCE(?raw, "未知（空值）") AS ?key)，业务口径不变。'
                )
    return findings


def collect_runtime_query_issues(queries: Mapping[str, Any] | None) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for name, query in sorted(dict(queries or {}).items()):
        if not isinstance(query, str):
            continue
        for message in lint_ontop_select(query):
            issues.append({
                "gate": GATE,
                "message": f"查询 {name}：{message}",
                "path": f"realtime_runtime.ontop_queries.{name}",
            })
    return issues
