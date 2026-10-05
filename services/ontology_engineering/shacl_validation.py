"""Complete SHACL validation with a proven datatype-focus rewrite and RDFS fallback.

Only explicit datatype-only shapes with exact domain alignment are optimized.
The complete source/document/derived graph remains unchanged; unsupported shapes,
RDFS schema changes, imports or library versions use the original full validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from time import perf_counter
from typing import Any

from pyshacl import validate
from rdflib import OWL, RDF, RDFS, ConjunctiveGraph, Dataset, Graph, Namespace, URIRef

POLICY = "rdfs-domain-datatype-focus-v1"
VERIFIED_LIBRARIES = {"pyshacl": "0.30.1", "owlrl": "7.1.4", "rdflib": "7.1.4"}


def library_versions() -> dict[str, str]:
    return {name: version(name) for name in VERIFIED_LIBRARIES}


SH = Namespace("http://www.w3.org/ns/shacl#")
NODE_ALLOWED = {
    RDF.type,
    RDFS.label,
    RDFS.comment,
    SH.name,
    SH.description,
    SH.targetClass,
    SH.property,
    SH.severity,
    SH.message,
}
PROPERTY_ALLOWED = {
    SH.path,
    SH.datatype,
    SH.severity,
    RDFS.label,
    RDFS.comment,
    SH.name,
    SH.description,
    SH.message,
}
TBOX_ALLOWED = {
    RDF.type,
    RDFS.label,
    RDFS.comment,
    RDFS.domain,
    RDFS.range,
    RDFS.subClassOf,
    OWL.versionIRI,
    OWL.versionInfo,
}
SCHEMA_TYPES = {
    RDF.Property,
    RDFS.Class,
    RDFS.Datatype,
    RDFS.ContainerMembershipProperty,
    OWL.Class,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
    OWL.Restriction,
    OWL.Ontology,
}
DYNAMIC_SCHEMA = {
    RDFS.domain,
    RDFS.range,
    RDFS.subClassOf,
    RDFS.subPropertyOf,
    OWL.imports,
    OWL.equivalentClass,
    OWL.equivalentProperty,
    OWL.inverseOf,
    OWL.propertyChainAxiom,
    OWL.onProperty,
    OWL.unionOf,
    OWL.intersectionOf,
    OWL.oneOf,
    OWL.complementOf,
}


@dataclass
class DatatypeFocusPlan:
    supported: bool
    reason: str
    node_shapes: int = 0
    property_shapes: int = 0
    target_paths: dict[Any, tuple[URIRef, ...]] | None = None


def datatype_focus_plan(data: Graph, shapes: Graph, ontology: Graph) -> DatatypeFocusPlan:
    def reject(reason: str) -> DatatypeFocusPlan:
        return DatatypeFocusPlan(False, reason)

    if isinstance(data, Dataset | ConjunctiveGraph):
        return reject("Named graph datasets require full RDFS validation")
    # Check every schema-producing predicate through graph indexes, not a sample.
    for predicate in DYNAMIC_SCHEMA:
        if next(data.triples((None, predicate, None)), None) is not None:
            return reject(f"Dynamic schema in complete input: {predicate}")
    for schema_type in SCHEMA_TYPES:
        if next(data.triples((None, RDF.type, schema_type)), None) is not None:
            return reject(f"Dynamic schema declaration in complete input: {schema_type}")
    for predicate in DYNAMIC_SCHEMA - {RDFS.domain, RDFS.range, RDFS.subClassOf}:
        if next(ontology.triples((None, predicate, None)), None) is not None:
            return reject(f"Unsupported schema axiom: {predicate}")
    for predicate in (RDFS.domain, RDFS.range):
        for prop, class_ in ontology.subject_objects(predicate):
            if (
                not isinstance(prop, URIRef)
                or not isinstance(class_, URIRef)
                or str(prop).startswith((str(RDF), str(RDFS), str(OWL)))
            ):
                return reject("Metamodel or non-named domain/range")
    annotations = set(ontology.subjects(RDF.type, OWL.AnnotationProperty))
    if any(p not in TBOX_ALLOWED | annotations for _, p, _ in ontology):
        return reject("Unsupported ontology axiom/predicate (includes imports/subproperties)")
    for child, parent in ontology.subject_objects(RDFS.subClassOf):
        if (
            not isinstance(child, URIRef)
            or not isinstance(parent, URIRef)
            or any(str(x).startswith((str(RDF), str(RDFS))) for x in (child, parent))
        ):
            return reject("Non-named or metamodel subclass hierarchy")
    if any(o not in SCHEMA_TYPES for _, o in ontology.subject_objects(RDF.type)):
        return reject("Unsupported ontology instance/schema kind")

    nodes = set(shapes.subjects(RDF.type, SH.NodeShape))
    if not nodes:
        return reject("No explicit NodeShapes")
    visited = set(nodes)
    properties = set()
    target_paths = {}
    for node in nodes:
        if set(shapes.objects(node, RDF.type)) != {SH.NodeShape}:
            return reject("Implicit class/property/custom shape kind")
        if any(p not in NODE_ALLOWED for p, _ in shapes.predicate_objects(node)):
            return reject("Unsupported node constraint or target")
        targets = list(shapes.objects(node, SH.targetClass))
        if len(targets) != 1 or not isinstance(targets[0], URIRef):
            return reject("Expected one named targetClass")
        target = targets[0]
        if str(target).startswith((str(RDF), str(RDFS))):
            return reject("Metamodel targetClass")
        paths = []
        for prop in shapes.objects(node, SH.property):
            if prop in properties:
                return reject("Shared or nested property shape")
            properties.add(prop)
            visited.add(prop)
            if any(p not in PROPERTY_ALLOWED for p, _ in shapes.predicate_objects(prop)):
                return reject("Unsupported property constraint or nested shape")
            ps = list(shapes.objects(prop, SH.path))
            ds = list(shapes.objects(prop, SH.datatype))
            if (
                len(ps) != 1
                or not isinstance(ps[0], URIRef)
                or len(ds) != 1
                or not isinstance(ds[0], URIRef)
            ):
                return reject("Expected one simple IRI path and one datatype")
            path, datatype = ps[0], ds[0]
            if str(path).startswith((str(RDF), str(RDFS))):
                return reject("RDF/RDFS structural path can be generated by inference")
            if (path, RDFS.domain, target) not in ontology:
                return reject("Exact path domain does not imply shape target")
            if set(ontology.objects(path, RDFS.range)) != {datatype}:
                return reject("Expected exactly matching datatype range")
            if next(ontology.triples((None, path, None)), None) is not None:
                return reject("Ontology contains values for a constrained path")
            paths.append(path)
        target_paths[node] = tuple(sorted(set(paths)))
    if set(shapes.subjects()) != visited:
        return reject("Unreferenced/custom shapes graph content")
    return DatatypeFocusPlan(
        True, "EXACT_DOMAIN_DATATYPE_ONLY", len(nodes), len(properties), target_paths
    )


def _copy_contract_graph(graph: Graph) -> Graph:
    copied = Graph()
    for prefix, namespace in graph.namespaces():
        copied.bind(prefix, namespace)
    for triple in graph:
        copied.add(triple)
    return copied


def validate_full_rdfs(data: Graph, shapes: Graph, ontology: Graph):
    # PySHACL adds two RDFS declarations to its shapes graph during loading.
    # Copy only these small contract graphs, never the caller's ABox.
    return validate(
        data,
        shacl_graph=_copy_contract_graph(shapes),
        ont_graph=_copy_contract_graph(ontology),
        inference="rdfs",
        advanced=True,
    )


def validate_complete_graph(data: Graph, shapes: Graph, ontology: Graph):
    """Validate every applicable value; unsupported contracts retain full RDFS.

    For the guarded datatype-only profile, every constrained path's exact domain
    entails its target class, so subjects of those paths are precisely the focus
    nodes which can violate a datatype constraint. No path values can be added by
    supported RDFS inference. Original PySHACL datatype checks and shape identities
    therefore produce the same violation multiset without expanding unrelated
    closure triples. The caller retains source completeness and all other S6 gates.
    """
    started = perf_counter()
    libraries = library_versions()
    applicability = (
        datatype_focus_plan(data, shapes, ontology)
        if libraries == VERIFIED_LIBRARIES
        else DatatypeFocusPlan(False, "Library versions require renewed equivalence validation")
    )
    if not applicability.supported:
        result = validate_full_rdfs(data, shapes, ontology)
        mode = "FULL_RDFS_FALLBACK"
    else:
        rewritten = _copy_contract_graph(shapes)
        for node, paths in (applicability.target_paths or {}).items():
            rewritten.remove((node, SH.targetClass, None))
            for path in paths:
                rewritten.add((node, SH.targetSubjectsOf, path))
        # Core-only guarded shapes need no remaining target inference. Keep the
        # complete ABox and original datatype evaluator; never coerce or sample.
        result = validate(
            data,
            shacl_graph=rewritten,
            ont_graph=None,
            inference="none",
            advanced=False,
            inplace=True,
        )
        mode = "DOMAIN_ENTAILED_DATATYPE_FOCUS"
    return result, {
        "policy_version": POLICY,
        "mode": mode,
        "reason": applicability.reason,
        "node_shape_count": applicability.node_shapes,
        "property_shape_count": applicability.property_shapes,
        "library_versions": libraries,
        "validation_seconds": round(perf_counter() - started, 6),
    }
