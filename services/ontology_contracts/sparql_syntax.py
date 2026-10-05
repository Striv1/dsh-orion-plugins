"""SPARQL grammar adapter: parsing only, with no query execution or I/O."""
from __future__ import annotations

from typing import Any

from pyparsing import ParseBaseException
from rdflib.plugins.sparql.parser import parseQuery

from .errors import CQBindingError


def parse_cq_query(sparql: str) -> Any:
    """Parse one complete query, shared by runtime compilation and stage gates.

    Comments, literals, prologues and projection aliases are SPARQL grammar,
    not text prefixes. Callers still enforce their own allowed query forms
    and dataset policy. Never include query text in syntax diagnostics.
    """
    if not str(sparql or "").strip():
        raise CQBindingError("CQ query is missing", reason_code="MISSING_CQ_QUERY", field="query")
    try:
        return parseQuery(sparql)[1]
    except ParseBaseException as exc:
        raise CQBindingError(
            f"CQ query has invalid SPARQL syntax at line {exc.lineno}, column {exc.col}",
            reason_code="INVALID_CQ_QUERY_SYNTAX", field="query",
        ) from exc


def sparql_query_type(sparql: str) -> str | None:
    """Return the operation only when the entire SPARQL query parses."""
    try:
        return query_type(parse_cq_query(sparql))
    except CQBindingError:
        return None


def query_type(parsed: Any) -> str | None:
    return {
        "SelectQuery": "SELECT", "AskQuery": "ASK",
        "ConstructQuery": "CONSTRUCT", "DescribeQuery": "DESCRIBE",
    }.get(parsed.name)


