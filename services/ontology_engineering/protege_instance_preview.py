"""Bounded, provenance-labelled teaching/readback asset from an actual S6 graph."""
from __future__ import annotations

import hashlib
from itertools import islice
from typing import Any

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

PREVIEW = Namespace("https://orion.local/preview-vocabulary#")


def build_preview(*, ontology_graph: Graph, data_graph: Any, classes: list[dict[str, Any]], project_id: str, model_version: str, snapshot_sha256: str, per_class: int = 3, subject_limit: int = 120, triple_limit: int = 6000) -> tuple[str, dict[str, Any]]:
    if per_class < 1 or subject_limit < 1 or triple_limit < 1:
        raise ValueError("Preview limits must be positive")
    chosen: set[Any] = set()
    coverage = []
    for cls in classes:
        members = list(islice(data_graph.subjects(RDF.type, URIRef(cls["iri"])), per_class + 1))
        selected = []
        for member in members[:per_class]:
            if member in chosen or len(chosen) < subject_limit:
                chosen.add(member)
                selected.append(str(member))
        coverage.append({"class_iri": cls["iri"], "selected_subjects": selected, "sample_limited": len(members) > len(selected), "membership_scope": "DIRECT_TYPES_ONLY"})
    sample = Graph()
    links: set[Any] = set()
    facts_truncated = False
    for subject in sorted(chosen, key=str):
        for triple in data_graph.triples((subject, None, None)):
            if len(sample) >= triple_limit:
                facts_truncated = True
                break
            sample.add(triple)
            if isinstance(triple[2], URIRef | BNode) and triple[1] != RDF.type and triple[2] not in chosen:
                links.add(triple[2])
    context = set(sorted(links, key=str)[:max(0, subject_limit - len(chosen))])
    for subject in sorted(context, key=str):
        for triple in data_graph.triples((subject, None, None)):
            if len(sample) >= triple_limit:
                facts_truncated = True
                break
            sample.add(triple)
    output = Graph()
    for prefix, namespace in ontology_graph.namespaces():
        output.bind(prefix, namespace)
    ontology_headers = set(ontology_graph.subjects(RDF.type, OWL.Ontology))
    for triple in ontology_graph:
        # A standalone preview has one explicit identity, never a formal release identity.
        if triple[0] not in ontology_headers:
            output.add(triple)
    for triple in sample:
        output.add(triple)
    digest = hashlib.sha256((project_id + model_version + snapshot_sha256).encode()).hexdigest()[:24]
    preview_iri = URIRef(f"https://orion.local/instance-preview/{digest}")
    output.add((preview_iri, RDF.type, OWL.Ontology))
    output.add((preview_iri, RDFS.label, Literal("模型与真实实例样本（非全量、非发布本体）", lang="zh")))
    output.add((preview_iri, RDFS.comment, Literal("来自同版本 S6 验收快照的有界样本。未运行推理，未修改原始事实；缺少实例或关系不能用于判断完整数据。", lang="zh")))
    for key, value in (("projectId", project_id), ("modelVersion", model_version), ("snapshotSha256", snapshot_sha256)):
        output.add((preview_iri, PREVIEW[key], Literal(value)))
        output.add((PREVIEW[key], RDF.type, OWL.AnnotationProperty))
    report = {
        "status": "GENERATED", "purpose": "PROTEGE_MODEL_AND_REAL_INSTANCE_SAMPLE", "is_full_dataset": False,
        "project_id": project_id, "model_version": model_version, "snapshot_sha256": snapshot_sha256,
        "source_artifact": "06-quality-validation/materialized.ttl", "model_artifact": "05-ontology-build/ontology.ttl",
        "preview_ontology_iri": str(preview_iri), "original_ontology_iris": sorted(map(str, ontology_headers)),
        "selected_subject_count": len(chosen), "context_subject_count": len(context), "sample_fact_count": len(sample),
        "selection_policy": "FIRST_BOUNDED_DIRECT_MEMBERS_PER_CLASS_PLUS_ONE_HOP_CONTEXT",
        "limits": {"per_class": per_class, "subjects": subject_limit, "sample_triples": triple_limit},
        "context_limited": len(context) < len(links), "facts_truncated": facts_truncated,
        "class_coverage": coverage, "inference_performed": False,
    }
    return str(output.serialize(format="xml")), report
