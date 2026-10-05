"""Resolve terms in actual SPARQL graph patterns, excluding textual mentions."""

from rdflib import URIRef, Variable
from rdflib.paths import Path
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery


def sparql_references_iri(query: str, iri: str) -> bool:
    """Match full/PREFIX/BASE IRIs in triples; literals and comments never count."""
    if not iri or not query:
        return False
    target = URIRef(iri)

    def term_matches(term):
        if isinstance(term, URIRef):
            return term == target
        if isinstance(term, Path):
            return any(term_matches(part) for part in vars(term).values())
        if isinstance(term, tuple | list):
            return any(term_matches(part) for part in term)
        return False

    def walk(node):
        if not isinstance(node, dict):
            return False
        if getattr(node, "name", None) == "BGP":
            return any(term_matches(triple) for triple in node.get("triples", []))
        # Only descend through graph algebra. Constants inside FILTER/BIND,
        # VALUES, literals or the prologue do not constitute a queried term.
        return any(walk(node[key]) for key in ("p", "p1", "p2") if key in node)

    try:
        return walk(translateQuery(parseQuery(query)).algebra)
    except Exception:
        return False


def sparql_has_only_scalar_aggregate_outputs(query: str) -> bool:
    """Prove SELECT outputs are ungrouped metrics, including joined subqueries.

    Unknown shapes fail closed. SAMPLE/GROUP_CONCAT can expose relationship
    members and are deliberately not treated as scalar metric outputs.
    """
    def metrics(node):
        if not isinstance(node, dict):
            return set()
        name = getattr(node, "name", None)
        if name == "AggregateJoin":
            group = node.get("p")
            if getattr(group, "name", None) != "Group" or group.get("expr"):
                return set()
            return {
                aggregate["res"] for aggregate in node["A"]
                if aggregate.name in {
                    "Aggregate_Count", "Aggregate_Sum", "Aggregate_Avg",
                    "Aggregate_Min", "Aggregate_Max",
                }
            }
        if name == "Join":
            left, right = metrics(node["p1"]), metrics(node["p2"])
            return left | right if left and right else set()
        if name == "Extend":
            known = metrics(node["p"])
            if isinstance(node["expr"], Variable) and node["expr"] in known:
                known.add(node["var"])
            return known
        if name == "Project":
            known, projected = metrics(node["p"]), set(node["PV"])
            return projected if projected and projected <= known else set()
        if name in {"SelectQuery", "ToMultiSet", "Filter", "OrderBy", "Distinct", "Slice"}:
            return metrics(node["p"])
        return set()

    try:
        algebra = translateQuery(parseQuery(query)).algebra
        return algebra.name == "SelectQuery" and bool(metrics(algebra))
    except Exception:
        return False
