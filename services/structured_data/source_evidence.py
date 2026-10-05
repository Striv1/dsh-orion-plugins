"""Bounded evidence counts on an S1-bound imported dataset or DB snapshot; never accepts SQL."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, time
from typing import Any

import psycopg
from psycopg import sql

from .pipeline import StructuredDataImportError, StructuredDataPipeline
from .snapshot_hub import _json_value


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


SNAPSHOT_SYSTEM_COLUMNS = frozenset({"dataset_id", "row_ordinal", "row_sha256"})


def _validate_query_shape(columns: set[str], group_by: list[str], equals: dict[str, Any] | None,
                          max_groups: int, reader_url: str) -> dict[str, Any]:
    if (not isinstance(group_by, list) or not 1 <= len(group_by) <= 6
            or not all(isinstance(name, str) and name in columns for name in group_by)
            or len(set(group_by)) != len(group_by)):
        raise StructuredDataImportError("group_by 须为 S1 中 1 至 6 个不同的已登记列。")
    equals = {} if equals is None else equals
    if (not isinstance(equals, dict) or len(equals) > 12
            or any(name not in columns for name in equals)):
        raise StructuredDataImportError("equals 仅接受最多 12 个已登记列的等值条件。")
    for value in equals.values():
        if (value is not None and not isinstance(value, str | int | float | bool)
                or isinstance(value, float) and not math.isfinite(value)
                or isinstance(value, str) and len(value) > 2000):
            raise StructuredDataImportError("等值条件必须为有限标量或 null，不能包含 SQL/表达式结构。")
    if type(max_groups) is not int or not 1 <= max_groups <= 500:
        raise StructuredDataImportError("max_groups 必须是 1 至 500 的整数。")
    if not reader_url:
        raise StructuredDataImportError("ORION_SOURCE_DATA_READER_URL 未配置，不能回退使用写入连接。")
    return equals


def _column_names(selected: dict[str, Any]) -> set[str]:
    return {
        item if isinstance(item, str) else item.get("column_name", item.get("name"))
        for item in selected.get("columns", [])
    } - {None, ""}


def _match_snapshot_table(schema_snapshot: dict[str, Any], table: str) -> list[dict[str, Any]]:
    """SnapshotHub tables are addressed by physical name or by their source table name."""
    matches = []
    for item in schema_snapshot.get("tables", []):
        if not isinstance(item, dict) or not item.get("dataset_id") or item.get("physical_version_table"):
            continue
        aliases = {item.get("name"), item.get("source_table")} - {None, ""}
        aliases |= {f"orion_data.{name}" for name in aliases}
        if item.get("source_id") and item.get("source_table"):
            aliases.add(f"{item['source_id']}.{item['source_table']}")
        if table in aliases:
            matches.append(item)
    return matches


def _run_grouped_count(cursor, *, physical: str, group_by: list[str], equals: dict[str, Any],
                       max_groups: int, source_types: dict[str, str],
                       dataset_scope: str | None = None):
    identifiers = sql.SQL(", ").join(map(sql.Identifier, group_by))
    predicates = []
    parameters: list[Any] = []
    if dataset_scope is not None:
        # Snapshot tables hold several captured versions; pin the S1 version.
        predicates.append(sql.SQL("{} = %s").format(sql.Identifier("dataset_id")))
        parameters.append(dataset_scope)
    predicates += [sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name)) for name in equals]
    parameters += [*equals.values(), max_groups + 1]
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(predicates) if predicates else sql.SQL("")
    # Window totals operate on all matching groups before LIMIT. The extra
    # group detects truncation without disguising a sample as a full count.
    statement = sql.SQL(
        "SELECT {}, COUNT(*), SUM(COUNT(*)) OVER (), COUNT(*) OVER () "
        "FROM orion_data.{}{} GROUP BY {} ORDER BY COUNT(*) DESC, {} LIMIT %s"
    ).format(identifiers, sql.Identifier(physical), where, identifiers, identifiers)
    cursor.execute(statement, parameters)
    raw = cursor.fetchall()
    matched_rows = int(raw[0][-2]) if raw else 0
    total_groups = int(raw[0][-1]) if raw else 0
    group_columns = [{"name": name, "source_type": source_types.get(name, "unknown")} for name in group_by]
    # Reuse SnapshotHub's precision-preserving scalar conversion. TIME is
    # also a JSON string, retaining timezone and fractional-second precision.
    groups = [{"values": {name: value.isoformat() if isinstance(value, time) else _json_value(value)
                          for name, value in zip(group_by, row[:len(group_by)], strict=True)},
               "count": int(row[-3])} for row in raw[:max_groups]]
    evidence_result = {
        "group_columns": group_columns, "groups": groups,
        "matched_row_count": matched_rows, "total_group_count": total_groups,
        "returned_group_count": len(groups), "truncated": total_groups > len(groups),
        "count_scope": "FULL_MATCHING_SOURCE",
        "value_encoding": "DECIMAL_AS_STRING_TEMPORAL_AS_ISO8601",
    }
    return statement, parameters, evidence_result


def _receipt(*, project_id, dataset_id, source_ref, source_sha256, table, physical, group_by,
             equals, statement, parameters, evidence_result, extra=None) -> dict[str, Any]:
    return {
        "status": "COMPLETE", "project_id": project_id, "dataset_id": dataset_id,
        "source_ref": source_ref, "source_sha256": source_sha256,
        "requested_table": table, "physical_version_table": physical,
        "group_by": group_by, "equals": equals, **evidence_result, **(extra or {}),
        "readonly_verified": True,
        "observed_at": datetime.now(UTC).isoformat(), "timeout_ms": 15000,
        "query_sha256": _hash({"sql": statement.as_string(), "parameters": parameters}),
        "result_sha256": _hash(evidence_result),
        "result_hash_fields": list(evidence_result),
        "message": "计数来自全部匹配行；groups 可能截断。此回执不改写 S1、不决定业务口径、不代表运行发布验证。",
    }


def snapshot_evidence_binding(*, project_id, schema_snapshot, datasource_inventory, selected):
    if selected.get("source_kind") == "STRUCTURED_FILE":
        from .execution_source import execution_schema

        # Resolve again from the reviewed inventory, never trust a caller's
        # file_import marker as authority to access an arbitrary ds_ table.
        canonical = execution_schema({**schema_snapshot, "tables": [selected]},
                                     datasource_inventory, project_id=project_id)["tables"][0]
        if canonical != selected:
            raise StructuredDataImportError("文件版本绑定与正式 S1 解析结果不一致。")
        return {"kind": "STRUCTURED_FILE", "dataset_id": selected["dataset_id"],
                "physical": selected["name"], "source_table": selected["source_table"],
                "source_ref": selected["source_ref"], "snapshot_set_id": None,
                "dataset": {"source_sha256": selected["file_import"]["source_sha256"]},
                "selected": selected}
    dataset_id = str(selected["dataset_id"])
    physical = str(selected.get("name") or "")
    source_table = str(selected.get("source_table") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", physical) or not source_table:
        raise StructuredDataImportError("S1 快照表缺少物理表名或来源表名，不能执行补查。")
    datasets = [item for item in datasource_inventory.get("datasets", [])
                if isinstance(item, dict) and item.get("dataset_id") == dataset_id]
    snapshot_set_id = schema_snapshot.get("snapshot_set_id")
    if (len(datasets) != 1 or datasets[0].get("project_id") != project_id
            or datasets[0].get("status") != "READY"
            or datasets[0].get("registered_via") != "ORION_SNAPSHOT_HUB"
            or not datasets[0].get("source_sha256") or not datasets[0].get("manifest_sha256")
            or not snapshot_set_id or datasets[0].get("snapshot_set_id") != snapshot_set_id):
        raise StructuredDataImportError("S1 快照 dataset 身份、快照集、来源哈希或 READY 状态不完整。")
    dataset = datasets[0]
    return {"dataset_id": dataset_id, "physical": physical, "source_table": source_table,
            "snapshot_set_id": snapshot_set_id, "dataset": dataset, "selected": selected}


def verify_snapshot_evidence_binding(cursor, *, project_id, binding):
    if binding.get("kind") == "STRUCTURED_FILE":
        return _verify_file_version_binding(cursor, project_id=project_id, binding=binding)
    dataset_id, physical, source_table, snapshot_set_id, dataset, selected = (
        binding[key] for key in ("dataset_id", "physical", "source_table", "snapshot_set_id", "dataset", "selected")
    )
    cursor.execute(
        "SELECT s.project_id, s.snapshot_complete, s.manifest_sha256 "
        "FROM orion_catalog.snapshot_sets s JOIN orion_catalog.snapshot_set_members m "
        "ON m.snapshot_set_id = s.snapshot_set_id "
        "WHERE s.snapshot_set_id=%s AND m.dataset_id=%s AND m.source_id=%s",
        (snapshot_set_id, dataset_id, selected.get("source_id")),
    )
    snapshot_set = cursor.fetchone()
    if (not snapshot_set or str(snapshot_set[0]) != project_id or snapshot_set[1] is not True
            or snapshot_set[2] != dataset["manifest_sha256"]):
        raise StructuredDataImportError("实时快照集不属于当前工程、未完成或 manifest 哈希与 S1 不一致。")
    cursor.execute(
        "SELECT project_id, source_sha256, snapshot_complete, manifest "
        "FROM orion_catalog.source_snapshots WHERE dataset_id=%s",
        (dataset_id,),
    )
    snapshot = cursor.fetchone()
    manifest_tables = (snapshot[3] or {}).get("tables", []) if snapshot else []
    members = [item for item in manifest_tables
               if item.get("target_table") == physical and item.get("table") == source_table]
    if (not snapshot or str(snapshot[0]) != project_id or snapshot[1] != dataset["source_sha256"]
            or snapshot[2] is not True or len(members) != 1):
        raise StructuredDataImportError("S1 与实时快照目录的来源哈希、物理表或来源表身份不一致。")


def _verify_file_version_binding(cursor, *, project_id, binding):
    selected = binding["selected"]
    imported = selected["file_import"]
    cursor.execute("SELECT project_id, status FROM orion_catalog.datasets WHERE dataset_id=%s",
                   (binding["dataset_id"],))
    owner = cursor.fetchone()
    # A later import supersedes the current pointer, not the immutable version
    # used by this design/release. Do not require it to remain the current view.
    if not owner or str(owner[0]) != project_id or str(owner[1]) not in {"READY", "SUPERSEDED"}:
        raise StructuredDataImportError("文件版本不属于当前工程或未完成正式导入。")
    receipt = StructuredDataPipeline("postgresql://unused")._receipt(cursor, binding["dataset_id"], unchanged=True)
    sheets = [s for s in receipt["sheets"] if s["table_name"] == binding["physical"]]
    if (receipt["project_id"] != project_id or receipt["document_id"] != imported["document_id"]
            or receipt["source_sha256"] != binding["dataset"]["source_sha256"]
            or len(sheets) != 1 or sheets[0]["view_name"] != binding["source_table"]
            or sheets[0]["source_name"] != imported["source_sheet"]
            or int(sheets[0]["row_count"]) != int(selected["row_count"])):
        raise StructuredDataImportError("文件版本目录与 S1 的文件、来源哈希、表或行数不一致。")
    from .execution_source import FILE_SYSTEM_COLUMNS
    if (set(selected["columns"]) - FILE_SYSTEM_COLUMNS
            != {c["column_name"] for c in sheets[0]["columns"]}):
        raise StructuredDataImportError("文件版本目录列与正式 S1 不一致。")


def _query_snapshot_counts(*, reader_url, project_id, schema_snapshot, datasource_inventory,
                           table, selected, group_by, equals, max_groups):
    binding = snapshot_evidence_binding(project_id=project_id, schema_snapshot=schema_snapshot,
                                        datasource_inventory=datasource_inventory, selected=selected)
    dataset_id, physical, source_table, snapshot_set_id, dataset = (
        binding[key] for key in ("dataset_id", "physical", "source_table", "snapshot_set_id", "dataset")
    )
    columns = _column_names(selected) - SNAPSHOT_SYSTEM_COLUMNS
    equals = _validate_query_shape(columns, group_by, equals, max_groups, reader_url)
    pipeline = StructuredDataPipeline(reader_url)
    with psycopg.connect(pipeline.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = 15000")
        verify_snapshot_evidence_binding(cursor, project_id=project_id, binding=binding)
        source_types = {name: str(kind) for name, kind in (selected.get("column_types") or {}).items()}
        statement, parameters, evidence_result = _run_grouped_count(
            cursor, physical=physical, group_by=group_by, equals=equals, max_groups=max_groups,
            source_types=source_types, dataset_scope=dataset_id,
        )
    return _receipt(
        project_id=project_id, dataset_id=dataset_id,
        source_ref=f"snapshot:{snapshot_set_id}:dataset:{dataset_id}:table:{source_table}",
        source_sha256=dataset["source_sha256"], table=table, physical=physical,
        group_by=group_by, equals=equals, statement=statement, parameters=parameters,
        evidence_result=evidence_result,
        extra={"snapshot_set_id": snapshot_set_id, "source_table": source_table,
               "manifest_sha256": dataset["manifest_sha256"], "registered_via": "ORION_SNAPSHOT_HUB"},
    )


def query_source_evidence_counts(
    *, reader_url: str, project_id: str, schema_snapshot: dict[str, Any],
    datasource_inventory: dict[str, Any], table: str, group_by: list[str],
    equals: dict[str, Any] | None = None, max_groups: int = 100,
) -> dict[str, Any]:
    """Resolve S1 aliases to the immutable version, then verify catalog identity."""
    if not isinstance(table, str) or not table.strip():
        raise StructuredDataImportError("table 必须来自当前 S1 Schema。")
    matches = []
    for item in schema_snapshot.get("tables", []):
        if not isinstance(item, dict) or item.get("schema") != "orion_data":
            continue
        aliases = {item.get("name"), item.get("table"), item.get("physical_version_table")} - {None, ""}
        aliases |= {f"orion_data.{name}" for name in aliases}
        if table in aliases:
            matches.append(item)
    if not matches:
        snapshot_matches = _match_snapshot_table(schema_snapshot, table)
        if len(snapshot_matches) == 1:
            return _query_snapshot_counts(
                reader_url=reader_url, project_id=project_id, schema_snapshot=schema_snapshot,
                datasource_inventory=datasource_inventory, table=table,
                selected=snapshot_matches[0], group_by=group_by, equals=equals, max_groups=max_groups,
            )
        if len(snapshot_matches) > 1:
            raise StructuredDataImportError("表名在 S1 快照中对应多张表，请改用 S1 的物理表名。")
    if len(matches) != 1 or not matches[0].get("physical_version_table"):
        raise StructuredDataImportError("只允许当前 S1 已登记且绑定物理版本的导入表或快照表；不扩展源库范围。")
    selected = matches[0]
    physical = selected["physical_version_table"]
    source_ref = str(selected.get("source_ref") or "")
    identity = re.fullmatch(r"dataset:([^:]+):sheet:(.+)", source_ref)
    if not identity:
        raise StructuredDataImportError("此表没有已登记的文件导入 dataset 身份，不能执行补查。")
    dataset_id = identity[1]
    datasets = [item for item in datasource_inventory.get("datasets", [])
                if item.get("dataset_id") == dataset_id]
    if (len(datasets) != 1 or datasets[0].get("project_id") != project_id
            or datasets[0].get("status") != "READY"
            or not datasets[0].get("source_sha256")):
        raise StructuredDataImportError("S1 dataset 身份、来源哈希或 READY 状态不完整。")
    equals = _validate_query_shape(_column_names(selected), group_by, equals, max_groups, reader_url)
    pipeline = StructuredDataPipeline(reader_url)
    with psycopg.connect(pipeline.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = 15000")
        cursor.execute("SELECT project_id, status FROM orion_catalog.datasets WHERE dataset_id=%s", (dataset_id,))
        owner = cursor.fetchone()
        if not owner or str(owner[0]) != project_id or str(owner[1]) != "READY":
            raise StructuredDataImportError("实时目录 dataset 不属于当前工程或未 READY。")
        receipt = pipeline._receipt(cursor, dataset_id, unchanged=True)
        sheets = [item for item in receipt["sheets"] if item["table_name"] == physical]
        if (receipt["source_sha256"] != datasets[0]["source_sha256"] or len(sheets) != 1
                or sheets[0]["view_name"] != selected.get("name", selected.get("table"))
                or sheets[0]["source_name"] != identity[2]):
            raise StructuredDataImportError("S1 与实时目录的版本表、别名或原始文件身份不一致。")
        catalog_columns = {item["column_name"]: item for item in sheets[0]["columns"]}
        if not (set(group_by) | set(equals)).issubset(catalog_columns):
            raise StructuredDataImportError("查询列不在此版本的实时目录中。")
        statement, parameters, evidence_result = _run_grouped_count(
            cursor, physical=physical, group_by=group_by, equals=equals, max_groups=max_groups,
            source_types={name: item.get("inferred_type", "unknown") for name, item in catalog_columns.items()},
        )
    return _receipt(
        project_id=project_id, dataset_id=dataset_id, source_ref=source_ref,
        source_sha256=receipt["source_sha256"], table=table, physical=physical,
        group_by=group_by, equals=equals, statement=statement, parameters=parameters,
        evidence_result=evidence_result,
    )
