#!/usr/bin/env python3
"""Build a deterministic browser graph from one resolved ORION release project."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.exceptions import ParserError
from rdflib.namespace import OWL, SKOS
from rdflib.plugins.parsers.ntriples import W3CNTriplesParser

KINDS = {
    OWL.Class: "class",
    RDFS.Class: "class",
    OWL.ObjectProperty: "objectProperty",
    OWL.DatatypeProperty: "dataProperty",
    OWL.NamedIndividual: "individual",
}

INSTANCE_PREVIEW_LIMIT = 200
INSTANCE_PREVIEW_TRIPLE_LIMIT = 10000


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _local_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("graph asset is outside the current project")
    return path


def _instance_preview(project_root: Path, model: Graph, model_path: Path) -> tuple[Graph, dict[str, Any]]:
    """Read only a bounded view of the current, release-bound validation graph."""
    metadata: dict[str, Any] = {"status": "UNAVAILABLE", "scope": "PREVIEW", "limit": INSTANCE_PREVIEW_LIMIT}
    preview = Graph()
    formal = project_root / "06-quality-validation/materialized.ttl"
    relative = "06-quality-validation/materialized.ttl" if formal.exists() else "05-ontology-build/materialized.ttl"
    if not (project_root / relative).exists():
        return preview, {**metadata, "reason": "NO_MATERIALIZED_GRAPH"}
    try:
        state = load_json(project_root / "workflow-state.json", {})
        if state.get("project_status") != "PUBLISHED" or state.get("stage_statuses", {}).get("S6") != "PASSED":
            raise ValueError("instance evidence is not currently published and validated")
        project_id = state.get("project_id")
        manifest = load_json(project_root / "artifact-manifest.json", {})
        if manifest.get("project_id") != project_id or project_id != project_root.name:
            raise ValueError("instance artifact project identity mismatch")
        entries = {item["path"]: item for item in manifest.get("files", [])}
        entry = entries.get(relative, {})
        lifecycle = state.get("artifact_lifecycle", {}).get(relative, {})
        if entry.get("lifecycle_status") != "CURRENT" or "INVALIDATED" in (lifecycle.get("status"), lifecycle.get("lifecycle_status")):
            raise ValueError("instance artifact is not current")
        path = _local_file(project_root, relative)
        checksum = _sha256(path)
        if checksum != entry.get("sha256"):
            raise ValueError("instance artifact checksum mismatch")
        publication = load_json(project_root / "07-release/publication.json", {})
        if publication.get("project_id") != project_id:
            raise ValueError("published project identity mismatch")
        # New releases bind S6 to an immutable snapshot, even when the large
        # instance graph is intentionally external to the delivery package.
        package_path = publication.get("package_path")
        if package_path:
            snapshot_path = _local_file(project_root, package_path + "/04-发布信息/release-snapshot.json")
            snapshot = load_json(snapshot_path, {})
            if (_sha256(snapshot_path) != publication.get("release_snapshot_sha256")
                    or snapshot.get("project_id") != project_id
                    or snapshot.get("release_version") != publication.get("release_version")):
                raise ValueError("release snapshot identity or checksum mismatch")
            for stage in ("S5", "S6"):
                released = snapshot.get("formal_stage_fingerprints", {}).get(stage, {})
                if (released.get("verification_status") != "VERIFIED"
                        or released.get("verified_sha256") != state.get("stage_fingerprints", {}).get(stage, {}).get("output")):
                    raise ValueError("instance evidence belongs to another design or validation revision")
            model_relative = model_path.relative_to(project_root).as_posix()
            model_entry = entries.get(model_relative, {})
            if (model_entry.get("lifecycle_status") != "CURRENT"
                    or _sha256(_local_file(project_root, model_relative)) != model_entry.get("sha256")):
                raise ValueError("model evidence no longer matches the released instance graph")
        elif publication.get("lifecycle_contract_version") or state.get("stage_contract_version"):
            raise ValueError("versioned release snapshot is missing")
        report = load_json(project_root / "06-quality-validation/semantica-report.json", {})
        quality = load_json(project_root / "06-quality-validation/quality-summary.json", {})
        if quality:
            quality_path = _local_file(project_root, "06-quality-validation/quality-summary.json")
            if (quality.get("status") != "PASSED"
                    or _sha256(quality_path) != entries.get("06-quality-validation/quality-summary.json", {}).get("sha256")):
                raise ValueError("full graph count evidence is unverified")
        classes = set(model.subjects(RDF.type, OWL.Class)) | set(model.subjects(RDF.type, RDFS.Class))
        selected: set[str] = set()
        if report.get("materialized_format") == "nt":
            # N-Triples lets us scan lines without materializing the full RDF
            # graph. Parse selected statements rather than truncating Turtle.
            candidate = Graph()
            class Sink:
                def triple(self, subject: Any, predicate: Any, obj: Any) -> None:
                    candidate.add((subject, predicate, obj))
            parser = W3CNTriplesParser(sink=Sink())
            type_marker = f" <{RDF.type}> "
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if type_marker not in line:
                        continue
                    candidate.remove((None, None, None))
                    parser.parsestring(line)
                    for subject, _, obj in candidate:
                        if isinstance(subject, URIRef) and obj in classes:
                            selected.add(subject.n3())
                    if len(selected) >= INSTANCE_PREVIEW_LIMIT:
                        break
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    parts = line.split(None, 1)
                    if parts and parts[0] in selected:
                        candidate.remove((None, None, None))
                        parser.parsestring(line)
                        preview += candidate
                        if len(preview) >= INSTANCE_PREVIEW_TRIPLE_LIMIT:
                            break
        else:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("large non-N-Triples graph requires a bounded preview asset")
            candidate = Graph().parse(path, format="turtle")
            selected_subjects = sorted({subject for subject, obj in candidate.subject_objects(RDF.type)
                                        if isinstance(subject, URIRef) and obj in classes}, key=str)[:INSTANCE_PREVIEW_LIMIT]
            selected = {subject.n3() for subject in selected_subjects}
            for subject in selected_subjects:
                for triple in candidate.triples((subject, None, None)):
                    if len(preview) >= INSTANCE_PREVIEW_TRIPLE_LIMIT:
                        break
                    preview.add(triple)
        metadata.update({"status": "AVAILABLE", "source_path": relative, "source_sha256": checksum,
                         "release_version": publication.get("release_version"),
                         "selected_subject_count": len(selected), "preview_triple_count": len(preview),
                         "full_graph_triple_count": quality.get("materialized_triple_count"),
                         "full_graph_count_source": "06-quality-validation/quality-summary.json",
                         "complete": False,
                         "detail": "有界实例预览；未展示的实例与关系仍保留在完整 S6 验证图中，预览不影响全量验收。"})
        return preview, metadata
    except (OSError, ValueError, KeyError, ParserError) as error:
        return Graph(), {**metadata, "reason": str(error)}


def local_name(value: Any) -> str:
    text = str(value)
    return text.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or text


def label_for(graph: Graph, subject: URIRef) -> str:
    labels = [(str(item), str(item.language or "")) for item in graph.objects(subject, RDFS.label)]
    for preferred in ("zh", "zh-cn", ""):
        match = next((text for text, language in labels if language.lower() == preferred), None)
        if match:
            return match
    return labels[0][0] if labels else local_name(subject)


def summary_for(graph: Graph, subject: URIRef) -> str:
    """Use authored RDF descriptions, preferring Chinese across both annotations."""
    candidates = []
    for predicate_index, predicate in enumerate((RDFS.comment, SKOS.definition)):
        for item in graph.objects(subject, predicate):
            if not isinstance(item, Literal) or not str(item).strip():
                continue
            language = (item.language or "").lower()
            language_rank = (
                0 if language == "zh" else
                1 if language.startswith("zh-") else
                2 if not language else 3
            )
            candidates.append((language_rank, predicate_index, language, str(item).strip()))
    return min(candidates)[-1] if candidates else ""


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def load_yaml(path: Path, fallback: Any) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or fallback
    except (OSError, yaml.YAMLError):
        return fallback


def build(project_root: Path) -> dict[str, Any]:
    graph = Graph()
    build_root = project_root / "05-ontology-build"
    ontology_path = next(
        (path for path in (
            build_root / "ontology.ttl",
            build_root / "ontology.rdf",
            build_root / "ontology.owl",
        ) if path.is_file()),
        None,
    )
    if ontology_path is None:
        raise FileNotFoundError("05-ontology-build 中不存在 ontology.ttl、ontology.rdf 或 ontology.owl")
    graph.parse(ontology_path)
    instance_graph, instance_preview = _instance_preview(project_root, graph, ontology_path)
    graph += instance_graph

    state = load_json(project_root / "workflow-state.json", {})
    project = load_json(project_root / "project.json", {})
    design = load_yaml(project_root / "04-ontology-design" / "ontology-design.yaml", {})
    rules = load_json(project_root / "02-semantic-recognition" / "business-rule-candidates.json", [])

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(value: URIRef | str, kind: str, **extra: Any) -> str:
        node_id = str(value)
        if node_id not in nodes:
            label = label_for(graph, value) if isinstance(value, URIRef) else extra.pop("label", local_name(value))
            summary = summary_for(graph, value) if isinstance(value, URIRef) else ""
            nodes[node_id] = {
                "id": node_id, "label": label, "kind": kind,
                **({"summary": summary} if summary else {}), **extra,
            }
        else:
            nodes[node_id].update({key: item for key, item in extra.items() if item not in (None, "", [])})
        return node_id

    def add_edge(source: str, target: str, predicate: str, label: str, kind: str, **extra: Any) -> None:
        if source == target:
            return
        summary = summary_for(graph, URIRef(predicate))
        edges[(source, target, predicate)] = {
            "id": f"e-{len(edges) + 1}", "source": source, "target": target,
            "predicate": predicate, "label": label, "kind": kind,
            **({"summary": summary} if summary else {}), **extra,
        }

    def relation_cardinality(prop: URIRef) -> dict[str, str]:
        """Return source-to-target cardinality without inventing ontology constraints."""
        functional = (prop, RDF.type, OWL.FunctionalProperty) in graph
        inverse_functional = (prop, RDF.type, OWL.InverseFunctionalProperty) in graph
        if functional and inverse_functional:
            return {"cardinality": "1:1", "cardinalityBasis": "OWL 约束"}
        if functional:
            return {"cardinality": "N:1", "cardinalityBasis": "OWL 约束"}
        if inverse_functional:
            return {"cardinality": "1:N", "cardinalityBasis": "OWL 约束"}

        pairs = [(subject, obj) for subject, obj in graph.subject_objects(prop)
                 if isinstance(subject, URIRef) and isinstance(obj, URIRef)]
        if not pairs:
            return {"cardinality": "未声明", "cardinalityBasis": "未声明"}
        outgoing: dict[URIRef, set[URIRef]] = {}
        incoming: dict[URIRef, set[URIRef]] = {}
        for subject, obj in pairs:
            outgoing.setdefault(subject, set()).add(obj)
            incoming.setdefault(obj, set()).add(subject)
        source_many = max((len(items) for items in outgoing.values()), default=0) > 1
        target_many = max((len(items) for items in incoming.values()), default=0) > 1
        if source_many and target_many:
            value = "N:N"
        elif source_many:
            value = "1:N"
        elif target_many:
            value = "N:1"
        else:
            value = "1:1"
        return {"cardinality": value, "cardinalityBasis": "数据观察"}

    typed: dict[URIRef, str] = {}
    for subject, object_type in graph.subject_objects(RDF.type):
        if isinstance(subject, URIRef) and object_type in KINDS:
            typed[subject] = KINDS[object_type]
    class_ids = {subject for subject, kind in typed.items() if kind == "class"}
    object_property_ids = {subject for subject, kind in typed.items() if kind == "objectProperty"}
    data_property_ids = {subject for subject, kind in typed.items() if kind == "dataProperty"}
    property_ids = object_property_ids | data_property_ids
    for subject, kind in typed.items():
        if kind in {"class", "objectProperty", "dataProperty"}:
            add_node(subject, kind)

    for subject, parent in graph.subject_objects(RDFS.subClassOf):
        if isinstance(subject, URIRef) and isinstance(parent, URIRef):
            add_node(subject, typed.get(subject, "class"))
            add_node(parent, typed.get(parent, "class"))
            add_edge(str(subject), str(parent), str(RDFS.subClassOf), "是…的子类", "subclass")

    for prop in object_property_ids:
        cardinality = relation_cardinality(prop)
        add_node(prop, "objectProperty")
        domains = [item for item in graph.objects(prop, RDFS.domain) if isinstance(item, URIRef)]
        ranges = [item for item in graph.objects(prop, RDFS.range) if isinstance(item, URIRef)]
        for domain in domains:
            add_node(domain, typed.get(domain, "class"))
            add_edge(str(prop), str(domain), str(RDFS.domain), "domain", "domain")
            for target in ranges:
                add_node(target, typed.get(target, "class"))
                add_edge(str(prop), str(target), str(RDFS.range), "range", "range")
                add_edge(str(domain), str(target), str(prop), label_for(graph, prop), "schema", **cardinality)

    for prop, inverse in graph.subject_objects(OWL.inverseOf):
        if isinstance(prop, URIRef) and isinstance(inverse, URIRef):
            add_node(prop, typed.get(prop, "objectProperty"))
            add_node(inverse, typed.get(inverse, "objectProperty"))
            add_edge(str(prop), str(inverse), str(OWL.inverseOf), "inverseOf", "inverse")

    for prop in data_property_ids:
        add_node(prop, "dataProperty")
        ranges = [item for item in graph.objects(prop, RDFS.range) if isinstance(item, URIRef)]
        value_type = "、".join(label_for(graph, item) for item in ranges) or "未声明数据类型"
        for domain in graph.objects(prop, RDFS.domain):
            if not isinstance(domain, URIRef):
                continue
            add_node(domain, typed.get(domain, "class"))
            add_edge(str(prop), str(domain), str(RDFS.domain), "domain", "domain")
            definitions = nodes[str(domain)].setdefault("schemaAttributes", [])
            definitions.append({"property": label_for(graph, prop), "value": value_type})

    individuals: set[URIRef] = {
        subject for subject, kind in typed.items() if kind == "individual"
    }
    for subject in individuals:
        add_node(subject, "individual")
    for subject, object_type in graph.subject_objects(RDF.type):
        if isinstance(subject, URIRef) and isinstance(object_type, URIRef) and object_type in class_ids:
            individuals.add(subject)
            add_node(subject, "individual")
            add_edge(str(subject), str(object_type), str(RDF.type), "属于", "instance")
    for subject, predicate, obj in graph:
        if subject in individuals and predicate in property_ids:
            if isinstance(obj, URIRef):
                add_node(obj, typed.get(obj, "individual"))
                add_edge(str(subject), str(obj), str(predicate), label_for(graph, predicate), "relationship",
                         **relation_cardinality(predicate))
            elif isinstance(obj, Literal):
                values = nodes[str(subject)].setdefault("attributes", [])
                if len(values) < 30:
                    values.append({"property": label_for(graph, predicate), "value": str(obj)})

    constraints = design.get("constraints", []) if isinstance(design, dict) else []

    kind_counts: dict[str, int] = {}
    for node in nodes.values():
        kind_counts[node["kind"]] = kind_counts.get(node["kind"], 0) + 1
    kind_counts["objectProperty"] = len(object_property_ids)
    kind_counts["dataProperty"] = len(data_property_ids)
    # Business-facing graph follows the Palantir mental model: rules and
    # constraints stay as release metadata instead of becoming globe nodes.
    kind_counts["rule"] = len(rules) if isinstance(rules, list) else 0
    kind_counts["constraint"] = len(constraints) if isinstance(constraints, list) else 0
    if instance_preview["status"] == "AVAILABLE":
        instance_preview["displayed_individual_count"] = kind_counts.get("individual", 0)
        instance_preview["context_individual_count"] = max(
            0, kind_counts.get("individual", 0) - instance_preview["selected_subject_count"],
        )
        total_triples = instance_preview.get("full_graph_triple_count")
        if isinstance(total_triples, int):
            instance_preview["unshown_triple_count"] = max(0, total_triples - instance_preview["preview_triple_count"])
    return {
        "project": {
            "id": state.get("project_id") or project_root.name,
            "name": state.get("project_name") or project.get("project_name") or project_root.name,
            "status": state.get("project_status"),
            "version": design.get("version") if isinstance(design, dict) else None,
            "ontologyIri": design.get("ontology_iri") if isinstance(design, dict) else None,
        },
        "stats": {**kind_counts, "nodes": len(nodes), "edges": len(edges)},
        "instance_preview": instance_preview,
        "nodes": sorted(nodes.values(), key=lambda item: (item["kind"], item["label"], item["id"])),
        "edges": list(edges.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.project_root.resolve()), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
