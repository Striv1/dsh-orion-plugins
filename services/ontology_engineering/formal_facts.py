"""Shared S6/S7/QA formal-fact RDF semantics; no source discovery or inference."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from .ontology_types import normalize_datatype_iri, validate_ontology_types
from .rdf_terms import canonical_literal

FORMAL_FACT = Namespace("urn:orion:formal-fact:")


class FormalFactContractError(RuntimeError):
    """A frozen fact binding cannot be interpreted as its declared RDF type."""

    def __init__(self, message: str, *, predicate: str, ontology_iri: str,
                 argument_index: int, datatype: str | None = None,
                 capability_name: str | None = None) -> None:
        super().__init__(message)
        self.predicate = predicate
        self.ontology_iri = ontology_iri
        self.argument_index = argument_index
        self.datatype = datatype
        self.capability_name = capability_name

    def to_dict(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "ontology_iri": self.ontology_iri,
                "argument_index": self.argument_index, "datatype": self.datatype,
                "capability_name": self.capability_name, "message": str(self)}


def non_binary_property_bindings(capability: dict[str, Any], kinds: dict[str, str]) -> list[str]:
    """Shared S3 preflight / S4 / QA rule: a property IRI always denotes a binary fact."""
    terms = capability.get("ontology_terms") or {}
    invalid = []
    for binding in capability.get("fact_bindings") or []:
        if not isinstance(binding, dict):
            continue
        iri = str(terms.get(binding.get("predicate")) or "")
        if kinds.get(iri) in {"DATA_PROPERTY", "OBJECT_PROPERTY"} and len(binding.get("arguments") or []) != 2:
            invalid.append(f"{binding.get('predicate')} → {iri}")
    return invalid


def validate_materialization_bindings(
    capability: dict[str, Any], kinds: dict[str, str], *, stage: str = "S6",
) -> None:
    """Binding sources do not carry datatypes; check their formal term/arity only."""
    invalid = non_binary_property_bindings(capability, kinds)
    if invalid:
        raise RuntimeError(f"{stage} 属性事实绑定必须为二元：" + "；".join(invalid))


def frozen_datatype_literal(value: str, datatype: str) -> Literal:
    iri = normalize_datatype_iri(datatype)
    if iri == "http://www.w3.org/2001/XMLSchema#string":
        # RDF 1.1: a simple literal IS xsd:string. Ontop/N-Triples emit the
        # simple form, and rdflib compares terms lexically, so a typed copy of
        # the same value would become a second term and duplicate joins/rows.
        return Literal(value)
    literal = Literal(value, datatype=URIRef(iri), normalize=False)
    # rdflib preserves unsupported/ill-typed literals instead of throwing.
    if literal.ill_typed is not False:
        raise RuntimeError(f"S6 数据属性值不符合冻结类型或类型不受支持：{iri}")
    local = iri.removeprefix("http://www.w3.org/2001/XMLSchema#")
    patterns = {
        "decimal": r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)",
        "boolean": r"true|false|1|0",
        "date": r"-?[0-9]{4,}-[0-9]{2}-[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?",
    }
    integer_types = {"integer", "nonPositiveInteger", "negativeInteger", "long", "int", "short", "byte",
                     "nonNegativeInteger", "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte", "positiveInteger"}
    if local in integer_types:
        patterns[local] = r"[+-]?[0-9]+"
    if local in patterns and re.fullmatch(patterns[local], value) is None:
        raise RuntimeError(f"S6 数据属性词法形式不符合冻结类型：{iri}")
    # The raw lexical form is validated above; the stored term is canonical so
    # "60" and "60.0" from different sources denote one RDF term.
    return canonical_literal(literal)


def materialize_formal_fact(
    graph: Graph,
    expression: str,
    ontology_terms: dict[str, Any],
    symbol_table: dict[str, str] | None = None,
    *,
    term_kinds: dict[str, str] | None = None,
    term_datatypes: dict[str, str] | None = None,
    strict_types: bool = False,
) -> URIRef | None:
    """Materialize one Semantica-compatible fact using stable source identities."""

    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_:-]*)\(([^()]*)\)", expression)
    if match is None:
        raise RuntimeError(f"事实表达式不合法：{expression}")
    predicate = match.group(1)
    arguments = [value.strip() for value in match.group(2).split(",")]
    if any(not value or value.startswith("?") for value in arguments):
        raise RuntimeError(f"事实必须包含已绑定的非空参数：{expression}")
    predicate_iri = str(ontology_terms.get(predicate) or "").strip()
    if not predicate_iri:
        raise RuntimeError(f"事实 {predicate} 缺少 ontology_terms IRI。")
    kind = (term_kinds or {}).get(predicate_iri)
    if strict_types and kind not in {"CLASS", "OBJECT_PROPERTY", "DATA_PROPERTY"}:
        raise RuntimeError(f"S6 事实缺少唯一冻结本体类型：{predicate_iri}")
    if kind in {"DATA_PROPERTY", "OBJECT_PROPERTY"} and len(arguments) != 2:
        raise RuntimeError(f"S6 属性事实必须为二元：{predicate_iri}")
    nodes = []
    source_values = []
    for value in arguments:
        source_value = (symbol_table or {}).get(value, value)
        source_values.append(source_value)
        if source_value.startswith(("http://", "https://", "urn:")):
            nodes.append(URIRef(source_value))
        else:
            nodes.append(
                URIRef(
                    "urn:orion:document-fact:"
                    + hashlib.sha256(source_value.encode("utf-8")).hexdigest()
                )
            )
    is_class = kind == "CLASS"
    if len(nodes) == 1:
        graph.add((nodes[0], RDF.type, URIRef(predicate_iri)))
    elif len(nodes) == 2 and not is_class:
        if kind == "DATA_PROPERTY":
            datatype = (term_datatypes or {}).get(predicate_iri)
            if not datatype:
                raise RuntimeError(f"S6 数据属性缺少冻结 datatype range：{predicate_iri}")
            try:
                target = frozen_datatype_literal(source_values[1], datatype)
            except RuntimeError as exc:
                raise FormalFactContractError(
                    f"事实 {predicate} 的第 2 个参数不符合数据属性 {predicate_iri} 的冻结类型 {datatype}；"
                    "请核对 fact_bindings 参数与 ontology_terms，不得用实体代替属性值或放宽类型。",
                    predicate=predicate, ontology_iri=predicate_iri, argument_index=2, datatype=datatype,
                ) from exc
        else:
            target = nodes[1]
        graph.add((nodes[0], URIRef(predicate_iri), target))
    else:
        # N-ary predicates are ordered assertions, not binary edges with dropped
        # arguments. Keep both the original engine symbol and resolved source
        # value. A business class type is added only when the approved S4 design
        # explicitly declares that role; untyped/legacy terms stay generic.
        identity = json.dumps([predicate_iri, source_values], ensure_ascii=False, separators=(",", ":"))
        fact = URIRef("urn:orion:reasoning-fact:" + hashlib.sha256(identity.encode()).hexdigest())
        graph.add((fact, RDF.type, FORMAL_FACT.Assertion))
        if is_class:
            graph.add((fact, RDF.type, URIRef(predicate_iri)))
        graph.add((fact, FORMAL_FACT.predicate, URIRef(predicate_iri)))
        graph.add((fact, FORMAL_FACT.predicateName, Literal(predicate)))
        graph.add((fact, FORMAL_FACT.arity, Literal(len(nodes))))
        graph.add((fact, FORMAL_FACT.expression, Literal(expression)))
        for position, (node, value, symbol) in enumerate(zip(nodes, source_values, arguments, strict=True), 1):
            argument = URIRef(f"{fact}:argument:{position}")
            graph.add((fact, URIRef(f"{RDF}_{position}"), argument))
            graph.add((argument, FORMAL_FACT.value, Literal(value)))
            graph.add((argument, FORMAL_FACT.engineSymbol, Literal(symbol)))
            graph.add((argument, FORMAL_FACT.denotes, node))
        return fact
    return None


def ontology_materialization_contract(ontology: Graph) -> tuple[dict[str, str], dict[str, str]]:
    """Read explicit types and unique datatype ranges from a verified release graph.

    The caller must verify the released asset hash before passing this graph.
    No implicit class/property declaration or datatype guess is accepted here.
    """
    issues = validate_ontology_types(ontology)
    if issues:
        raise RuntimeError("已发布本体类型合同无效：" + json.dumps(issues, ensure_ascii=False))
    roles: dict[str, set[str]] = {}
    for rdf_type, kind in ((OWL.Class, "CLASS"), (OWL.ObjectProperty, "OBJECT_PROPERTY"), (OWL.DatatypeProperty, "DATA_PROPERTY")):
        for subject in ontology.subjects(RDF.type, rdf_type):
            # Anonymous OWL expressions are legitimate ontology structure but
            # cannot serve as a named fact predicate/identity contract.
            if not isinstance(subject, URIRef):
                continue
            roles.setdefault(str(subject), set()).add(kind)
    if any(len(kinds) != 1 for kinds in roles.values()):
        raise RuntimeError("已发布本体存在非唯一本体类型，不能猜测事实实例化语义。")
    kinds = {iri: next(iter(values)) for iri, values in roles.items()}
    datatypes: dict[str, str] = {}
    for iri, kind in kinds.items():
        if kind != "DATA_PROPERTY":
            continue
        ranges = set(ontology.objects(URIRef(iri), RDFS.range))
        if len(ranges) != 1 or not isinstance(next(iter(ranges)), URIRef):
            raise RuntimeError(f"已发布数据属性必须具有唯一明确的 datatype range：{iri}")
        datatypes[iri] = normalize_datatype_iri(str(next(iter(ranges))))
    return kinds, datatypes


def validate_formal_fact_types(
    facts: list[str], capability: dict[str, Any], symbol_table: dict[str, str], *,
    capability_name: str, term_kinds: dict[str, str], term_datatypes: dict[str, str],
) -> None:
    """Apply the runtime materializer's type checks without changing a source graph."""
    validate_materialization_bindings(capability, term_kinds)
    graph = Graph()
    for fact in facts:
        try:
            materialize_formal_fact(
                graph, fact, capability.get("ontology_terms") or {}, symbol_table,
                term_kinds=term_kinds, term_datatypes=term_datatypes, strict_types=True,
            )
        except FormalFactContractError as exc:
            exc.capability_name = capability_name
            raise
