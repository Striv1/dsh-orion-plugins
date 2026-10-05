from __future__ import annotations

from typing import Any

from rdflib import OWL, RDF, RDFS, SH, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import DCTERMS

from .ontology_types import normalize_datatype_iri, validate_ontology_types
from .reporting import ONTOLOGY_NAME_LABELS

ORION = Namespace("https://orion.example/vocab/workflow#")

# Fields consumed by this compiler. CQ-only revisions can reuse S5; changing
# any of these inputs requires fresh binary and constraint validation.
BUILD_DESIGN_FIELDS = (
    "ontology_iri", "version", "title_zh", "comment_zh", "classes",
    "object_properties", "data_properties", "logical_axioms",
)


def build_rdf_artifacts(design: dict[str, Any]) -> dict[str, str | int]:
    """把已通过 S4 的施工图确定性翻译为 OWL/TTL/SHACL 草稿。"""

    ontology_iri = str(design["ontology_iri"]).rstrip("#/")
    graph = Graph()
    graph.bind("owl", OWL)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("dcterms", DCTERMS)
    graph.bind("xsd", XSD)
    graph.bind("orion", ORION)
    graph.bind("sc", Namespace(f"{ontology_iri}#"))

    ontology = URIRef(ontology_iri)
    graph.add((ontology, RDF.type, OWL.Ontology))
    graph.add((ontology, OWL.versionInfo, Literal(str(design["version"]))))
    graph.add(
        (
            ontology,
            OWL.versionIRI,
            URIRef(f"{ontology_iri}/{design['version']}"),
        )
    )
    title_zh = str(design.get("title_zh") or "ORION 企业本体")
    comment_zh = str(
        design.get("comment_zh")
        or "由已评审 mapping.yaml 和 ontology-design.yaml 确定性构建。"
    )
    graph.add((ontology, DCTERMS.title, Literal(title_zh, lang="zh")))
    graph.add((ontology, RDFS.label, Literal(title_zh, lang="zh")))
    graph.add(
        (
            ontology,
            RDFS.comment,
            Literal(comment_zh, lang="zh"),
        )
    )
    graph.add((ORION.sourceMappingId, RDF.type, OWL.AnnotationProperty))
    graph.add((ORION.sourceMappingId, RDFS.label, Literal("来源 Mapping 编号", lang="zh")))
    graph.add(
        (
            ORION.sourceMappingId,
            RDFS.comment,
            Literal("记录本体实体可追溯到的正式 mapping.yaml 条目。", lang="zh"),
        )
    )
    graph.add((ORION.sourceSemanticId, RDF.type, OWL.AnnotationProperty))
    graph.add((ORION.sourceSemanticId, RDFS.label, Literal("来源语义候选编号", lang="zh")))
    graph.add((ORION.sourceRuleId, RDF.type, OWL.AnnotationProperty))
    graph.add((ORION.sourceRuleId, RDFS.label, Literal("来源规则编号", lang="zh")))

    def annotate(entity: URIRef, item: dict[str, Any], kind: str) -> None:
        name = str(item["name"])
        label_zh = str(item.get("label_zh") or ONTOLOGY_NAME_LABELS.get(name, name))
        comment_zh = str(item.get("comment_zh") or f"{label_zh}的{kind}定义；来源于正式 Mapping。")
        graph.add((entity, RDFS.label, Literal(label_zh, lang="zh")))
        graph.add((entity, RDFS.label, Literal(name, lang="en")))
        graph.add((entity, RDFS.comment, Literal(comment_zh, lang="zh")))
        for mapping_id in item.get("source_mapping_ids") or []:
            graph.add((entity, ORION.sourceMappingId, Literal(str(mapping_id))))
        for semantic_id in item.get("source_semantic_ids") or []:
            graph.add((entity, ORION.sourceSemanticId, Literal(str(semantic_id))))
        for rule_id in item.get("source_rule_ids") or []:
            graph.add((entity, ORION.sourceRuleId, Literal(str(rule_id))))

    for item in design.get("classes") or []:
        entity = URIRef(str(item["iri"]))
        graph.add((entity, RDF.type, OWL.Class))
        annotate(entity, item, "业务类（Class）")

    for item in design.get("object_properties") or []:
        entity = URIRef(str(item["iri"]))
        graph.add((entity, RDF.type, OWL.ObjectProperty))
        graph.add((entity, RDFS.domain, URIRef(str(item["domain"]))))
        graph.add((entity, RDFS.range, URIRef(str(item["range"]))))
        annotate(entity, item, "对象属性/业务关系（Object Property）")

    for item in design.get("data_properties") or []:
        entity = URIRef(str(item["iri"]))
        graph.add((entity, RDF.type, OWL.DatatypeProperty))
        graph.add((entity, RDFS.domain, URIRef(str(item["domain"]))))
        graph.add((entity, RDFS.range, URIRef(normalize_datatype_iri(item["range"]))))
        annotate(entity, item, "数据属性/字段含义（Data Property）")

    for item in design.get("logical_axioms") or []:
        axiom_type = str(item["axiom_type"]).upper()
        subject = URIRef(str(item["class"] if "class" in item else item["child"]))
        if axiom_type == "SUBCLASS_OF":
            graph.add((subject, RDFS.subClassOf, URIRef(str(item["parent"]))))
            continue
        if axiom_type == "DISJOINT_WITH":
            graph.add((subject, OWL.disjointWith, URIRef(str(item["other"]))))
            continue
        restriction = BNode()
        graph.add((restriction, RDF.type, OWL.Restriction))
        graph.add((restriction, OWL.onProperty, URIRef(str(item["property"]))))
        if axiom_type == "EQUIVALENT_DATA_HAS_VALUE":
            datatype = URIRef(normalize_datatype_iri(item.get("datatype") or XSD.string))
            graph.add((restriction, OWL.hasValue, Literal(item["value"], datatype=datatype)))
        elif axiom_type == "EQUIVALENT_OBJECT_SOME_VALUES_FROM":
            graph.add(
                (restriction, OWL.someValuesFrom, URIRef(str(item["filler"])))
            )
        else:  # S4 validates the enumeration before this builder is called.
            raise ValueError(f"unsupported logical axiom type: {axiom_type}")
        expression = BNode()
        members = BNode()
        graph.add((expression, RDF.type, OWL.Class))
        graph.add((expression, OWL.intersectionOf, members))
        Collection(
            graph,
            members,
            [URIRef(str(item["base_class"])), restriction],
        )
        graph.add((subject, OWL.equivalentClass, expression))

    shapes = Graph()
    shapes.bind("sh", SH)
    shapes.bind("xsd", XSD)
    shapes.bind("sc", Namespace(f"{ontology_iri}#"))
    data_by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in design.get("data_properties") or []:
        data_by_domain.setdefault(str(item["domain"]), []).append(item)
    for item in design.get("classes") or []:
        class_iri = str(item["iri"])
        shape = URIRef(f"{class_iri}Shape")
        shapes.add((shape, RDF.type, SH.NodeShape))
        shapes.add((shape, SH.targetClass, URIRef(class_iri)))
        shapes.add(
            (
                shape,
                RDFS.label,
                Literal(f"{ONTOLOGY_NAME_LABELS.get(str(item['name']), item['name'])}数据约束", lang="zh"),
            )
        )
        for prop in data_by_domain.get(class_iri, []):
            property_shape = URIRef(f"{shape}/{prop['name']}")
            shapes.add((shape, SH.property, property_shape))
            shapes.add((property_shape, SH.path, URIRef(str(prop["iri"]))))
            shapes.add((property_shape, SH.datatype, URIRef(normalize_datatype_iri(prop["range"]))))
            shapes.add((property_shape, SH.severity, SH.Violation))

    type_issues = validate_ontology_types(graph, design)
    if type_issues:
        raise ValueError(f"本体编译类型校验失败：{type_issues}")

    return {
        "ontology_ttl": str(graph.serialize(format="turtle")),
        "ontology_owl": str(graph.serialize(format="xml")),
        "shapes_ttl": str(shapes.serialize(format="turtle")),
        "ontology_triple_count": len(graph),
        "shape_triple_count": len(shapes),
    }
