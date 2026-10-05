"""Necessary declaration coverage for document-backed required class instances.

This does not execute facts or prove that instances exist. S6 remains authoritative.
Only routes consumed by the existing document materializer count as coverage.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def missing_document_class_bindings(
    mappings: list[dict[str, Any]], runtime: dict[str, Any],
    *, resolve_iri: Callable[[Any], str],
) -> list[dict[str, Any]]:
    queries = runtime.get("document_fact_queries", {})
    reasoning = runtime.get("reasoning_capabilities")
    # Shape errors are owned by their existing validators.
    if not isinstance(queries, dict) or (reasoning is not None and not isinstance(reasoning, dict)):
        return []
    consumers = [q for q in queries.values() if isinstance(q, dict) and q.get("cq_bindings")]
    reasoners = [c for c in (reasoning or {}).values()
                if isinstance(c, dict) and isinstance(c.get("evidence_query"), str)
                and isinstance(queries.get(c["evidence_query"]), dict)]
    consumers.extend(reasoners)
    covered: set[str] = set()
    # S6 may also add explicit types from declared rule results. Keep this
    # necessary-only check conservative; final generation semantics stay in S6.
    for reasoner in reasoners:
        terms, results = reasoner.get("ontology_terms"), reasoner.get("result_predicates")
        if isinstance(terms, dict) and isinstance(results, list):
            covered.update(terms[p] for p in results if isinstance(p, str) and isinstance(terms.get(p), str))
    for consumer in consumers:
        terms, bindings = consumer.get("ontology_terms"), consumer.get("fact_bindings")
        if not isinstance(terms, dict) or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict) or not isinstance(binding.get("predicate"), str):
                continue
            term = terms.get(binding["predicate"])
            if isinstance(term, str):
                covered.add(term)
    missing = []
    for index, mapping in enumerate(mappings):
        contract = mapping.get("instance_contract")
        if (str(mapping.get("mapping_type") or "").strip().upper() != "EVIDENCE_TO_CLASS" or not isinstance(contract, dict)
                or contract.get("generation_mode") != "DOCUMENT_FACTS"
                or contract.get("empty_policy") != "REQUIRE_NONEMPTY" or not mapping.get("target")):
            continue
        iri = resolve_iri(mapping["target"])
        if iri not in covered:
            missing.append({"mapping_index": index, "mapping_id": mapping.get("id"), "class_iri": iri})
    return missing
