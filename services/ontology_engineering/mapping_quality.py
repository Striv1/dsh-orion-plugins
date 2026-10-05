"""Read-only graph diagnostics; counts never stand in for source-row lineage.

The caller supplies a validated design and graph snapshot. Source count receipts
must come from an independently validated profiler for that same source version;
this module validates receipt shape, not their authenticity or freshness.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from rdflib import RDF, BNode, Graph, URIRef


def _sample(values: list[str], value: str, limit: int) -> None:
    if limit and value not in values:
        values.append(value)
        values.sort()
        del values[limit:]


def build_mapping_quality_report(
    graph: Graph,
    design: dict[str, Any],
    mappings: list[dict[str, Any]],
    *,
    source_counts: list[dict[str, Any]] | None = None,
    sample_limit: int = 10,
    finding_limit: int = 100,
) -> dict[str, Any]:
    """Describe the supplied snapshot without inference or mutation.

    Graph counts include only assertions actually present (the supplied graph may
    itself be materialized). Missing descriptions are review items under RDF's
    open-world semantics, never proof of broken foreign keys. Samples are bounded;
    counters include all matching triples, even when sample_limit is zero.
    """
    if type(sample_limit) is not int or not 0 <= sample_limit <= 100:
        raise ValueError("sample_limit must be an integer from 0 to 100")
    if type(finding_limit) is not int or not 0 <= finding_limit <= 1000:
        raise ValueError("finding_limit must be an integer from 0 to 1000")
    mapping_ids = Counter(str(item["id"]) for item in mappings if item.get("id"))
    findings: list[dict[str, Any]] = []
    severity_counts: Counter[str] = Counter()

    def add_finding(finding: dict[str, Any]) -> None:
        severity_counts[finding["severity"]] += 1
        if len(findings) < finding_limit:
            findings.append(finding)

    for key in sorted(mapping_ids):
        if mapping_ids[key] > 1:
            add_finding({"code": "DUPLICATE_MAPPING_ID", "severity": "ERROR", "mapping_id": key, "count": mapping_ids[key]})

    classes = []
    relationships = []
    for kind, items in (("CLASS", design.get("classes", [])), ("OBJECT_PROPERTY", design.get("object_properties", [])), ("DATA_PROPERTY", design.get("data_properties", []))):
        for item in items:
            iri = str(item["iri"])
            contract = item.get("instance_contract") or {}
            refs = set(item.get("source_mapping_ids") or []) | set(contract.get("mapping_refs") or [])
            missing = sorted(ref for ref in refs if ref not in mapping_ids)
            if missing:
                add_finding({"code": "UNKNOWN_MAPPING_REFERENCE", "severity": "ERROR", "iri": iri, "mapping_ids": missing})
            if kind == "CLASS":
                count = sum(1 for _ in graph.triples((None, RDF.type, URIRef(iri))))
                classes.append({"class_iri": iri, "label": item.get("label_zh") or item.get("name") or iri, "direct_instance_count": count, "empty_policy": contract.get("empty_policy"), "mapping_ids": sorted(refs)})
                if count == 0 and contract.get("empty_policy") == "REQUIRE_NONEMPTY":
                    add_finding({"code": "REQUIRED_CLASS_EMPTY", "severity": "ERROR", "iri": iri, "count": 0})
            elif kind == "OBJECT_PROPERTY":
                count = invalid = undescribed = 0
                invalid_samples: list[str] = []
                undescribed_samples: list[str] = []
                for _, _, target in graph.triples((None, URIRef(iri), None)):
                    count += 1
                    if not isinstance(target, URIRef | BNode):
                        invalid += 1
                        _sample(invalid_samples, target.n3(), sample_limit)
                    elif next(graph.triples((target, None, None)), None) is None:
                        undescribed += 1
                        _sample(undescribed_samples, target.n3(), sample_limit)
                relationships.append({"property_iri": iri, "relationship_count": count, "non_resource_target_count": invalid, "undescribed_target_edge_count": undescribed, "mapping_ids": sorted(refs)})
                if invalid:
                    add_finding({"code": "OBJECT_PROPERTY_LITERAL_TARGET", "severity": "ERROR", "iri": iri, "count": invalid, "samples": invalid_samples})
                if undescribed:
                    add_finding({"code": "TARGET_DESCRIPTION_ABSENT", "severity": "REVIEW", "iri": iri, "count": undescribed, "count_unit": "EDGES", "samples": undescribed_samples, "meaning": "Target has no outgoing assertion in this snapshot; external or intentionally undescribed resources may be valid."})

    receipts = []
    for receipt in source_counts or []:
        count = receipt.get("row_count")
        if (type(count) is not int or count < 0
                or not isinstance(receipt.get("source"), str) or not receipt["source"].strip()
                or not isinstance(receipt.get("evidence_ref"), str) or not receipt["evidence_ref"].strip()
                or receipt.get("scope") not in {"FULL_SOURCE", "SAMPLE"}):
            raise ValueError("source count requires source, nonnegative integer row_count, evidence_ref and FULL_SOURCE/SAMPLE scope")
        receipts.append({key: receipt[key] for key in ("source", "row_count", "evidence_ref", "scope")})

    errors = severity_counts["ERROR"]
    reviews = severity_counts["REVIEW"]
    return {
        "version": "mapping-quality-v1",
        "status": "ISSUES_FOUND" if errors else "REVIEW_REQUIRED" if reviews else "NO_DETECTED_ISSUES",
        "scope": "ASSERTIONS_IN_SUPPLIED_GRAPH_SNAPSHOT",
        "acceptance_status": "NOT_DETERMINED",
        "triple_count": len(graph),
        "mapping_count": len(mappings),
        "classes": classes,
        "relationships": relationships,
        "findings": findings,
        "findings_truncated": errors + reviews > len(findings),
        "finding_count": errors + reviews,
        "error_count": errors,
        "review_count": reviews,
        "source_counts": receipts,
        "source_count_status": "CALLER_SUPPLIED_RECEIPTS" if receipts else "UNAVAILABLE",
        "source_to_instance_reconciliation": {"status": "NOT_EVALUATED", "reason": "Row-level mapping lineage and approved cardinality expectations are required; source rows and graph instances need not be equal."},
        "identity_collision_check": {"status": "NOT_EVALUATED", "reason": "RDF merges identical triples; distinct source keys and their generated identities are required to establish collisions."},
        "limitations": ["No inference is performed; subclass, equivalent-class and sameAs closure are not computed.", "No source-row, join-loss, mapping-specific output or duplicate-generation counts are inferred from graph counts.", "No detected issues does not establish business correctness or release acceptance."],
    }
