from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from services.structured_data.multi_source import (
    SAFE_IDENTIFIER,
    CrossSourceSnapshotSet,
    EntityResolutionContract,
    MultiSourceContractError,
)

IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")


class ProjectedDataProperty(BaseModel):
    column: str = Field(pattern=SAFE_IDENTIFIER.pattern)
    property_iri: str
    datatype_iri: str = "http://www.w3.org/2001/XMLSchema#string"

    @model_validator(mode="after")
    def validate_iris(self) -> ProjectedDataProperty:
        if not IRI.fullmatch(self.property_iri) or not IRI.fullmatch(self.datatype_iri):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: property/datatype IRI is invalid"
            )
        return self


class SnapshotEntityProjection(BaseModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    class_iri: str
    role: Literal["CANONICAL", "RELATED"]
    iri_key_column: str | None = Field(default=None, pattern=SAFE_IDENTIFIER.pattern)
    resource_iri_prefix: str | None = None
    relation_from_canonical_iri: str | None = None
    data_properties: list[ProjectedDataProperty] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_projection(self) -> SnapshotEntityProjection:
        iris = [self.class_iri]
        if self.resource_iri_prefix:
            iris.append(self.resource_iri_prefix)
        if self.relation_from_canonical_iri:
            iris.append(self.relation_from_canonical_iri)
        if any(not IRI.fullmatch(value) for value in iris):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: entity/relation IRI is invalid"
            )
        if self.role == "RELATED" and not all(
            (self.iri_key_column, self.resource_iri_prefix, self.relation_from_canonical_iri)
        ):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: related projection is incomplete"
            )
        if self.role == "CANONICAL" and any(
            (self.iri_key_column, self.resource_iri_prefix, self.relation_from_canonical_iri)
        ):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: canonical IRI must come from identity links"
            )
        return self


class DerivedObjectProperty(BaseModel):
    property_iri: str
    domain_iri: str
    range_iri: str
    rule_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_derived_relation(self) -> DerivedObjectProperty:
        if any(
            not IRI.fullmatch(value)
            for value in (self.property_iri, self.domain_iri, self.range_iri)
        ):
            raise MultiSourceContractError(
                "G-S4-RELATION-COVERAGE: derived relation IRI/domain/range is invalid"
            )
        return self


class SnapshotSemanticProjection(BaseModel):
    project_id: str
    snapshot_set_id: str = Field(pattern=r"^SS-[A-F0-9]{24}$")
    identity_contract_id: str = Field(pattern=r"^ER-[A-Z0-9-]{8,80}$")
    ontology_iri: str
    projections: list[SnapshotEntityProjection] = Field(min_length=2)
    derived_object_properties: list[DerivedObjectProperty] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self) -> SnapshotSemanticProjection:
        if not IRI.fullmatch(self.ontology_iri):
            raise MultiSourceContractError("G-S3-SOURCE-MAPPING-COVERAGE: ontology IRI is invalid")
        canonical = [item for item in self.projections if item.role == "CANONICAL"]
        if len(canonical) != 1:
            raise MultiSourceContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: exactly one canonical projection is required"
            )
        if len({item.source_id for item in self.projections}) != len(self.projections):
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: duplicate projection source"
            )
        return self


class SnapshotSemanticArtifacts(BaseModel):
    ontology_ttl: str
    mapping_obda: str
    mapping_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_set_id: str
    source_ids: list[str]
    class_count: int = Field(ge=1)
    object_property_count: int = Field(ge=1)
    data_property_count: int = Field(ge=0)


def render_snapshot_semantic_artifacts(
    *,
    snapshot_set: CrossSourceSnapshotSet,
    identity_contract: EntityResolutionContract,
    projection: SnapshotSemanticProjection,
) -> SnapshotSemanticArtifacts:
    """Render one Ontop mapping bound to an immutable multi-source snapshot set."""

    expected_sources = {item.source_id for item in snapshot_set.source_snapshots}
    projected_sources = {item.source_id for item in projection.projections}
    if (
        projection.project_id != snapshot_set.project_id
        or projection.snapshot_set_id != snapshot_set.snapshot_set_id
        or identity_contract.project_id != snapshot_set.project_id
        or projection.identity_contract_id != identity_contract.contract_id
    ):
        raise MultiSourceContractError(
            "G-S6-SNAPSHOT-BINDING: semantic projection binding mismatch"
        )
    if projected_sources != expected_sources:
        raise MultiSourceContractError(
            "G-S3-SOURCE-MAPPING-COVERAGE: every snapshot source needs one projection"
        )

    classes = sorted({item.class_iri for item in projection.projections})
    data_properties = {
        item.property_iri: item.datatype_iri
        for entity in projection.projections
        for item in entity.data_properties
    }
    relationships = {
        entity.relation_from_canonical_iri: (
            next(item.class_iri for item in projection.projections if item.role == "CANONICAL"),
            entity.class_iri,
        )
        for entity in projection.projections
        if entity.relation_from_canonical_iri
    }
    for item in projection.derived_object_properties:
        if item.property_iri in relationships:
            raise MultiSourceContractError(
                "G-S4-RELATION-COVERAGE: duplicate mapped/derived object property"
            )
        relationships[item.property_iri] = (item.domain_iri, item.range_iri)
    ontology_lines = [
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        f"<{projection.ontology_iri}> a owl:Ontology .",
        "",
    ]
    ontology_lines.extend(f"<{iri}> a owl:Class ." for iri in classes)
    for iri, (domain, range_) in sorted(relationships.items()):
        ontology_lines.append(
            f"<{iri}> a owl:ObjectProperty ; rdfs:domain <{domain}> ; rdfs:range <{range_}> ."
        )
    for iri, datatype in sorted(data_properties.items()):
        domains = sorted(
            entity.class_iri
            for entity in projection.projections
            if any(item.property_iri == iri for item in entity.data_properties)
        )
        if len(domains) != 1:
            raise MultiSourceContractError(
                "G-S3-SOURCE-MAPPING-COVERAGE: data property must have one explicit domain"
            )
        ontology_lines.append(
            f"<{iri}> a owl:DatatypeProperty ; rdfs:domain <{domains[0]}> ; "
            f"rdfs:range <{datatype}> ."
        )

    mapping_lines = [
        "[PrefixDeclaration]",
        "xsd: http://www.w3.org/2001/XMLSchema#",
        "",
        "[MappingDeclaration] @collection [[",
    ]
    for index, entity in enumerate(projection.projections, start=1):
        snapshot = next(
            item for item in snapshot_set.source_snapshots if item.source_id == entity.source_id
        )
        source_table_name = identity_contract.source_tables[entity.source_id]
        target_table = next(
            item.target_table for item in snapshot.tables if item.table == source_table_name
        )
        aliases = [item.column for item in entity.data_properties]
        target_subject = (
            "<{canonical_iri}>"
            if entity.role == "CANONICAL"
            else f"<{entity.resource_iri_prefix}{{{entity.iri_key_column}}}>"
        )
        target_parts = [f"{target_subject} a <{entity.class_iri}>"]
        target_parts.extend(
            f"<{item.property_iri}> {{{item.column}}}^^<{item.datatype_iri}>"
            for item in entity.data_properties
        )
        if entity.role == "RELATED":
            target_parts.append(
                f". <{{canonical_iri}}> <{entity.relation_from_canonical_iri}> {target_subject}"
            )
        select_columns = ["link.canonical_iri"]
        select_columns.extend(
            f"source.{column}"
            for column in dict.fromkeys(
                [*aliases, *([entity.iri_key_column] if entity.iri_key_column else [])]
            )
        )
        source_sql = (
            "SELECT "
            + ", ".join(select_columns)
            + " FROM orion_data.canonical_identity_members link "
            + f"JOIN orion_data.{target_table} source "
            + "ON source.dataset_id=link.dataset_id "
            + "AND source.row_ordinal=link.row_ordinal "
            + f"WHERE link.snapshot_set_id='{snapshot_set.snapshot_set_id}' "
            + f"AND link.contract_id='{identity_contract.contract_id}' "
            + f"AND link.source_id='{entity.source_id}'"
        )
        mapping_lines.extend(
            [
                f"mappingId MultiSourceProjection{index}",
                "target " + " ; ".join(target_parts) + " .",
                "source " + source_sql,
                "",
            ]
        )
    mapping_lines.append("]]")
    mapping = "\n".join(mapping_lines) + "\n"
    return SnapshotSemanticArtifacts(
        ontology_ttl="\n".join(ontology_lines) + "\n",
        mapping_obda=mapping,
        mapping_sha256="sha256:" + hashlib.sha256(mapping.encode("utf-8")).hexdigest(),
        snapshot_set_id=snapshot_set.snapshot_set_id,
        source_ids=sorted(expected_sources),
        class_count=len(classes),
        object_property_count=len(relationships),
        data_property_count=len(data_properties),
    )
