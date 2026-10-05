"""Shared RDF type checks at the compiler and exported-artifact boundaries."""
from __future__ import annotations

from typing import Any

from rdflib import OWL, RDF, RDFS, XSD, Graph, URIRef

_PREFIXES = {"xsd": str(XSD), "rdf": str(RDF), "rdfs": str(RDFS), "owl": str(OWL)}


def normalize_datatype_iri(value: Any) -> str:
    """Expand supported datatype CURIEs before constructing RDF URIRef nodes."""
    text = str(value).strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    prefix, separator, local = text.partition(":")
    if prefix in _PREFIXES and separator and local:
        text = _PREFIXES[prefix] + local
    elif not (text.startswith(("http://", "https://", "urn:")) and separator):
        raise ValueError(f"数据类型必须是完整 IRI 或内建前缀：{value}")
    if any(char.isspace() or char in '<>"{}|\\^`' for char in text):
        raise ValueError(f"无效数据类型 IRI：{value}")
    return text


def validate_ontology_types(
    graph: Graph, design: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Return structural errors; reasoner consistency alone does not check these."""
    issues: list[dict[str, str]] = []

    def add(code: str, entity: Any, message: str) -> None:
        issues.append({"code": code, "entity": str(entity), "message": message})

    classes = set(graph.subjects(RDF.type, OWL.Class)) | set(graph.subjects(RDF.type, RDFS.Class))
    data = set(graph.subjects(RDF.type, OWL.DatatypeProperty))
    objects = set(graph.subjects(RDF.type, OWL.ObjectProperty))
    annotations = set(graph.subjects(RDF.type, OWL.AnnotationProperty))
    declared_datatypes = set(graph.subjects(RDF.type, RDFS.Datatype))

    def is_datatype(term: Any) -> bool:
        return (str(term).startswith(str(XSD)) or term in declared_datatypes
                or term in {RDFS.Literal, RDF.langString, RDF.XMLLiteral, RDF.HTML})

    for entity in sorted(data & objects, key=str):
        add("PROPERTY_TYPE_COLLISION", entity, "属性同时声明为 ObjectProperty 和 DatatypeProperty")
    for entity in sorted((data | objects) & annotations, key=str):
        add("PROPERTY_TYPE_COLLISION", entity, "业务属性不能同时声明为 AnnotationProperty")
    for entity in sorted(classes, key=str):
        if is_datatype(entity) or str(entity).startswith("xsd:"):
            add("DATATYPE_AS_CLASS", entity, "数据类型被错误声明为业务类")
    for term in sorted(set(graph.all_nodes()), key=str):
        if isinstance(term, URIRef) and any(str(term).startswith(prefix + ":") for prefix in _PREFIXES):
            add("UNEXPANDED_BUILTIN_IRI", term, "内建前缀被作为字面 IRI，必须先展开命名空间")
    for entity in sorted(data, key=str):
        for target in graph.objects(entity, RDFS.range):
            if not is_datatype(target):
                add("INVALID_DATA_PROPERTY_RANGE", entity, f"数据属性 range 未声明为 datatype：{target}")
    for entity in sorted(objects, key=str):
        for target in graph.objects(entity, RDFS.range):
            if is_datatype(target) or str(target).startswith("xsd:"):
                add("INVALID_OBJECT_PROPERTY_RANGE", entity, f"对象属性 range 不能是 datatype：{target}")
    for entity in sorted(data | objects, key=str):
        for target in graph.objects(entity, RDFS.domain):
            if is_datatype(target):
                add("INVALID_PROPERTY_DOMAIN", entity, f"属性 domain 不能是 datatype：{target}")
    if design is not None:
        for field, expected_type in (("classes", OWL.Class), ("object_properties", OWL.ObjectProperty), ("data_properties", OWL.DatatypeProperty)):
            for item in design.get(field) or []:
                entity = URIRef(str(item["iri"]))
                if (entity, RDF.type, expected_type) not in graph:
                    add("MISSING_DESIGN_DECLARATION", entity, f"缺少 S4 类型声明：{expected_type}")
                if field == "data_properties":
                    try:
                        expected_range = normalize_datatype_iri(item.get("range", ""))
                    except ValueError as exc:
                        add("INVALID_DESIGN_DATATYPE", entity, str(exc))
                        continue
                    actual = sorted(str(target) for target in graph.objects(entity, RDFS.range))
                    if actual != [expected_range]:
                        add("DATA_RANGE_DESIGN_MISMATCH", entity, f"数据属性 range 与 S4 不一致：expected={expected_range}, actual={actual}")
    return issues
