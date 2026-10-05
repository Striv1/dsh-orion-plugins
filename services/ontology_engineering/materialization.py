from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef

ORION = Namespace("https://orion.example/vocab/workflow#")


def _split_qualified_column(source: str) -> tuple[str, str]:
    normalized = source.strip().removeprefix("table:")
    match = re.fullmatch(
        r'(?P<table>(?:[A-Za-z_][\w$]*\.)?"?[A-Za-z_][\w$]*"?)\.(?P<column>"?[A-Za-z_][\w$]*"?)',
        normalized,
    )
    if match is None:
        return normalized.split("(", 1)[0], ""
    return match.group("table"), match.group("column").strip('"')


def _row_identity(row: dict[str, Any]) -> Any:
    for key in ("id", "_orion_source_row"):
        if row.get(key) is not None:
            return row[key]
    for key, value in row.items():
        if key.endswith("_id") and value is not None:
            return value
    return None


def materialize_rows(
    design: dict[str, Any],
    mapping_document: dict[str, Any],
    relation_candidates: list[dict[str, Any]],
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """依据正式 Mapping 把数据库行确定性物化为 RDF 实例图。"""

    base = str(design["ontology_iri"]).rstrip("#/")
    graph = Graph()
    graph.bind("sc", Namespace(f"{base}#"))
    graph.bind("orion", ORION)
    mapping = mapping_document.get("mappings") or []
    mapping_by_id = {str(item["id"]): item for item in mapping}
    class_by_table = {
        str(item["source"]): str(item["target"])
        for item in mapping
        if item.get("mapping_type") == "TABLE_TO_CLASS"
    }
    relation_index = {
        (str(item.get("source_table")), str(item.get("source_column"))): item
        for item in relation_candidates
    }
    design_classes = {str(item["name"]): item for item in design.get("classes") or []}

    def instance_iri(table: str, record_id: Any) -> URIRef:
        return URIRef(
            f"{base}#instance/{quote(str(table), safe='')}/{quote(str(record_id), safe='')}"
        )

    def value_instance_iri(class_name: str, value: Any) -> URIRef:
        return instance_iri(f"class:{class_name}", value)

    for table, class_name in class_by_table.items():
        class_item = design_classes[class_name]
        for row in rows_by_table.get(table, []):
            record_id = _row_identity(row)
            if record_id is None:
                continue
            subject = instance_iri(table, record_id)
            graph.add((subject, RDF.type, URIRef(str(class_item["iri"]))))
            label = row.get("name") or row.get("code") or f"{class_name}-{record_id}"
            graph.add((subject, RDFS.label, Literal(str(label), lang="zh")))
            graph.add((subject, ORION.sourceTable, Literal(table)))
            graph.add((subject, ORION.sourceRecordId, Literal(str(record_id))))

    for mapping_item in mapping:
        if mapping_item.get("mapping_type") != "COLUMN_VALUE_TO_CLASS":
            continue
        table, column = _split_qualified_column(str(mapping_item.get("source") or ""))
        class_name = str(mapping_item.get("target") or "").split(" (")[0].strip()
        class_item = design_classes.get(class_name)
        if not table or not column or class_item is None:
            continue
        for row in rows_by_table.get(table, []):
            value = row.get(column)
            if value is None or str(value).strip() == "":
                continue
            subject = value_instance_iri(class_name, value)
            graph.add((subject, RDF.type, URIRef(str(class_item["iri"]))))
            graph.add((subject, RDFS.label, Literal(str(value), lang="zh")))
            graph.add((subject, ORION.sourceTable, Literal(table)))
            graph.add((subject, ORION.sourceRecordId, Literal(str(value))))

    for prop in design.get("data_properties") or []:
        mapping_item = mapping_by_id[str(prop["source_mapping_ids"][0])]
        source = str(mapping_item["source"])
        table, column = _split_qualified_column(source)
        datatype = URIRef(str(prop["range"]))
        for row in rows_by_table.get(table, []):
            value = row.get(column)
            record_id = _row_identity(row)
            if record_id is None or value is None:
                continue
            if isinstance(value, Decimal):
                value = str(value)
            graph.add(
                (
                    instance_iri(table, record_id),
                    URIRef(str(prop["iri"])),
                    Literal(value, datatype=datatype),
                )
            )

    relationship_count = 0
    skipped_mapping_ids: list[str] = []
    for prop in design.get("object_properties") or []:
        mapping_item = mapping_by_id[str(prop["source_mapping_ids"][0])]
        source = str(mapping_item["source"])
        predicate = URIRef(str(prop["iri"]))
        mapping_type = str(mapping_item.get("mapping_type") or "").upper()
        if mapping_type == "COLUMN_VALUE_TO_OBJECT_PROPERTY":
            table, column = _split_qualified_column(source)
            range_name = str(mapping_item.get("range") or "").strip()
            for row in rows_by_table.get(table, []):
                record_id = _row_identity(row)
                value = row.get(column)
                if record_id is None or value is None or str(value).strip() == "":
                    continue
                graph.add(
                    (
                        instance_iri(table, record_id),
                        predicate,
                        value_instance_iri(range_name, value),
                    )
                )
                relationship_count += 1
            continue
        if mapping_type in {
            "SQL_TO_OBJECT_PROPERTY",
            "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
        }:
            skipped_mapping_ids.append(str(mapping_item.get("id") or ""))
            continue
        if "(" in source and len(mapping_item.get("source_refs") or []) >= 2:
            join_table = source.split("(", 1)[0]
            refs = [
                str(ref).removeprefix("table:")
                for ref in mapping_item["source_refs"]
            ]
            endpoints: list[tuple[str, str, str]] = []
            for ref in refs:
                table, column = _split_qualified_column(ref)
                relation = relation_index[(table, column)]
                endpoints.append(
                    (
                        column,
                        str(relation["target_table"]),
                        str(relation["target_column"]),
                    )
                )
            for row in rows_by_table.get(join_table, []):
                left, right = endpoints[0], endpoints[1]
                if row.get(left[0]) is None or row.get(right[0]) is None:
                    continue
                graph.add(
                    (
                        instance_iri(left[1], row[left[0]]),
                        predicate,
                        instance_iri(right[1], row[right[0]]),
                    )
                )
                relationship_count += 1
            continue

        table, column = _split_qualified_column(source)
        relation = relation_index[(table, column)]
        target_table = str(relation["target_table"])
        inverted = str(prop["name"]).startswith("has")
        for row in rows_by_table.get(table, []):
            record_id = _row_identity(row)
            if record_id is None or row.get(column) is None:
                continue
            source_node = instance_iri(table, record_id)
            target_node = instance_iri(target_table, row[column])
            graph.add(
                (target_node, predicate, source_node)
                if inverted
                else (source_node, predicate, target_node)
            )
            relationship_count += 1

    instance_count = len(set(graph.subjects(RDF.type, None)))
    return {
        "materialized_ttl": str(graph.serialize(format="turtle")),
        "triple_count": len(graph),
        "instance_count": instance_count,
        "relationship_count": relationship_count,
        "source_table_count": len(rows_by_table),
        "source_row_count": sum(len(rows) for rows in rows_by_table.values()),
        "skipped_mapping_ids": [item for item in skipped_mapping_ids if item],
    }
