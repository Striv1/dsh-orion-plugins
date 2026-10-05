"""One value, one RDF term: canonical lexical forms for exact numeric literals.

rdflib compares literals by lexical form, so "60"^^xsd:decimal from a document
fact and "60.0"^^xsd:decimal from an Ontop CAST(... AS numeric) become two
terms. COUNT(DISTINCT), GROUP BY and joins then report a false conflict. Every
S6 source (Ontop partitions, document facts, reasoning conclusions, checkpoints)
applies the same canonicalization, so S7 releases and realtime QA read one term.
Only exact types (xsd:decimal and integer derivatives) are rewritten; float,
double and all other datatypes keep their lexical form.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rdflib import Graph, Literal, URIRef

_XSD = "http://www.w3.org/2001/XMLSchema#"
_INTEGER_TYPES = frozenset(
    _XSD + local
    for local in (
        "integer", "nonPositiveInteger", "negativeInteger", "long", "int", "short", "byte",
        "nonNegativeInteger", "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte",
        "positiveInteger",
    )
)
_DECIMAL = _XSD + "decimal"
PROFILE = "rdf-exact-numeric-canonical-v1"


def canonical_numeric_lexical(value: str, datatype: str) -> str | None:
    """Canonical lexical form, or None when not an exact numeric type/value."""
    if datatype not in _INTEGER_TYPES and datatype != _DECIMAL:
        return None
    text = value.strip()
    if not text or any(ch in text for ch in "eEnNiI"):  # exact types have no exponent/NaN/INF
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return str(int(number))
    if datatype in _INTEGER_TYPES:
        return None
    return format(number.normalize(), "f")


def canonical_literal(term: Literal) -> Literal:
    datatype = str(term.datatype or "")
    lexical = canonical_numeric_lexical(str(term), datatype)
    if lexical is None or lexical == str(term):
        return term
    return Literal(lexical, datatype=URIRef(datatype), normalize=False)


def canonicalize_numeric_literals(graph: Graph) -> int:
    """Rewrite exact numeric literals in place; returns the number of rewritten triples."""
    changes = []
    for subject, predicate, obj in graph:
        if isinstance(obj, Literal) and obj.datatype is not None:
            canonical = canonical_literal(obj)
            if canonical is not obj:
                changes.append((subject, predicate, obj, canonical))
    for subject, predicate, obj, canonical in changes:
        graph.remove((subject, predicate, obj))
        graph.add((subject, predicate, canonical))
    return len(changes)
