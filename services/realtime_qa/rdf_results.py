"""Typed SPARQL SELECT evidence shared by preview and formal verification."""
from __future__ import annotations

import math
import re

from rdflib import RDF, XSD, Literal, URIRef

from services.ontology_engineering.formal_facts import frozen_datatype_literal
from services.ontology_engineering.ontology_types import normalize_datatype_iri


def rdf_term_value(term):
    if not isinstance(term, dict) or set(term) - {"type", "value", "datatype", "xml:lang"}:
        raise ValueError("malformed RDF binding")
    kind, lexical = term.get("type"), term.get("value")
    if not isinstance(lexical, str):
        raise ValueError("RDF binding requires a lexical value")
    datatype, language = term.get("datatype"), term.get("xml:lang")
    if kind in {"uri", "bnode"}:
        if datatype is not None or language is not None or not lexical:
            raise ValueError("malformed RDF resource")
        if kind == "uri" and not re.fullmatch(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s<>"{}\\]*', lexical):
            raise ValueError("invalid RDF resource IRI")
        return lexical
    if kind not in {"literal", "typed-literal"} or (kind == "typed-literal" and not datatype):
        raise ValueError("unsupported RDF binding kind")
    if language is not None:
        if (not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z0-9]+)*", language)
                or datatype not in (None, str(RDF.langString))):
            raise ValueError("invalid language literal")
        return lexical
    if datatype is None:
        return lexical
    if not isinstance(datatype, str) or normalize_datatype_iri(datatype) != datatype:
        raise ValueError("invalid datatype IRI")
    literal = (frozen_datatype_literal(lexical, datatype) if datatype.startswith(str(XSD))
               else Literal(lexical, datatype=URIRef(datatype)))
    if literal.ill_typed is True:
        raise ValueError("ill-typed RDF literal")
    value = literal.toPython()
    if isinstance(value, Literal):
        return lexical  # Preserve an unknown datatype's evidence, never guess.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite RDF numeric value")
    return value


def typed_result_rows(result):
    """Decode only matching RDF metadata; lexical-looking numbers are not evidence of type."""
    rows, terms, variables = result.get("rows"), result.get("row_terms"), result.get("variables")
    if not isinstance(rows, list) or not isinstance(variables, list):
        raise ValueError("SELECT result requires rows and variables")
    if result.get("truncated") or any(not isinstance(field, str) for field in variables) or len(set(variables)) != len(variables):
        raise ValueError("SELECT result is truncated or has an invalid header")
    if terms is None and not rows:
        terms = []
    if not isinstance(terms, list) or len(terms) != len(rows):
        raise ValueError("complete-row verification requires matching RDF row metadata")
    decoded = []
    for row, metadata in zip(rows, terms, strict=True):
        if (not isinstance(row, dict) or not isinstance(metadata, dict)
                or set(row) != set(metadata) or not set(row) <= set(variables)):
            raise ValueError("SELECT row and RDF metadata disagree")
        values = {}
        for field, term in metadata.items():
            if not isinstance(term, dict) or row[field] != term.get("value"):
                raise ValueError("SELECT lexical value and RDF metadata disagree")
            values[field] = rdf_term_value(term)
        decoded.append(values)
    return decoded
