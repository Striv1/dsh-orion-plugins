"""IRI term helpers shared by S3/S4/S5 gates and S7 release binding.

Ontology terms referenced by runtime reasoning capabilities must match the
built ontology exactly. The most common authoring slip is reusing the same
local name with a different separator or base (".../X" vs "...#X"), which
before this module only surfaced at S7 release binding. These helpers let
earlier gates explain the mismatch and suggest the declared IRI.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from rdflib import Graph, URIRef

_SPLIT = re.compile(r"[#/]")


def local_name(iri: str) -> str:
    """Return the segment after the last '#' or '/'."""

    return _SPLIT.split(str(iri or "").rstrip("#/"))[-1]


def suggest_declared_iri(iri: str, declared: Iterable[str]) -> str | None:
    """Suggest the unique declared IRI sharing the local name of `iri`."""

    name = local_name(iri)
    if not name:
        return None
    matches = sorted(
        {str(item) for item in declared if local_name(str(item)) == name} - {str(iri)}
    )
    return matches[0] if len(matches) == 1 else None


def describe_undeclared(iris: Iterable[str], declared: Iterable[str]) -> str:
    """Format undeclared IRIs, adding a normalized suggestion when unambiguous."""

    declared_set = {str(item) for item in declared}
    parts: list[str] = []
    for iri in sorted({str(item) for item in iris}):
        suggestion = suggest_declared_iri(iri, declared_set)
        parts.append(f"{iri}（疑似应为 {suggestion}）" if suggestion else iri)
    return ", ".join(parts)


def graph_signature(graph: Graph) -> set[str]:
    """All URIRef terms in any triple position; mirrors S7 release binding."""

    return {str(term) for triple in graph for term in triple if isinstance(term, URIRef)}


def reasoning_terms_absent_from(
    capabilities: Mapping[str, Any] | None, signature: set[str]
) -> dict[str, list[str]]:
    """Map capability name to its ontology_terms values absent from `signature`."""

    missing: dict[str, list[str]] = {}
    for name, capability in sorted((capabilities or {}).items()):
        if not isinstance(capability, Mapping):
            continue
        terms = capability.get("ontology_terms") or {}
        if not isinstance(terms, Mapping):
            continue
        absent = sorted(
            {str(value) for value in terms.values() if str(value or "").strip()} - signature
        )
        if absent:
            missing[str(name)] = absent
    return missing

