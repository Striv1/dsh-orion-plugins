"""Compile S1 from the promoted Snapshot Hub, never from agent-written receipts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from psycopg import sql
from sqlalchemy.engine import make_url

from .multi_source import (
    CrossSourceSnapshotSet,
    SourceBinding,
    SourceProfileReceipt,
    build_cross_source_snapshot_set,
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def snapshot_reader_principal() -> str:
    """Return the platform read-only principal that serves imported snapshots."""
    raw = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
    if raw:
        try:
            username = str(make_url(raw).username or "").strip()
        except Exception:  # noqa: BLE001 - malformed URL falls back to the platform default
            username = ""
        if username:
            return username
    return "orion_source_reader"


def build_snapshot_handoff(
    cursor: Any, *, project_id: str, dataset_ids: list[str]
) -> dict[str, Any]:
    """Use the caller's read-only transaction for manifest, schema and count readback.

    An atomic snapshot set is indivisible: a partial/stale selection is rejected,
    not silently expanded. Relationships are not inferred from similar names.
    """
    cursor.execute(
        "SELECT s.manifest FROM orion_catalog.current_snapshot_sets c "
        "JOIN orion_catalog.snapshot_sets s USING(snapshot_set_id) WHERE c.project_id=%s",
        (project_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError("G-S1-SNAPSHOT-COMPLETE: 当前工程没有已晋升的 SnapshotHub 快照集。")
    snapshot = CrossSourceSnapshotSet.model_validate(row[0])
    if snapshot.project_id != project_id:
        raise ValueError("G-S1-SOURCE-SCOPE: 当前已晋升快照集不属于本工程，请用 capture_database_snapshot 为本工程重新采集。")
    if not snapshot.production_evidence:
        raise ValueError(
            "G-S1-SOURCE-SCOPE: 当前快照集是 TEST_ONLY，不能通过生产门禁。"
            "补救：以 dataset_type=PRODUCTION 重新调用 capture_database_snapshot，"
            "并在 production_evidence_basis 如实写明授权人、依据与数据性质（合成数据须注明为合成验收数据）。"
        )
    if set(dataset_ids) != {source.dataset_id for source in snapshot.source_snapshots}:
        raise ValueError("G-S1-SNAPSHOT-COMPLETE: dataset_ids 必须完整且精确匹配当前已晋升快照集。")
    bindings = []
    source_profiles = []
    schema_tables = []
    profile_tables = []
    evidence = []
    datasets = []
    checked_at = datetime.now(UTC).isoformat()
    binding_fields = (
        "project_id",
        "source_id",
        "engine",
        "connection_ref",
        "database",
        "catalog",
        "schemas",
        "authorized_tables",
        "authorized_columns",
        "access_mode",
        "readonly_attested",
        "pii_scope",
        "owner",
        "status",
    )
    for source in snapshot.source_snapshots:
        cursor.execute(
            "SELECT project_id, source_id, engine, connection_ref, database_name, catalog_name, "
            "schemas, authorized_tables, authorized_columns, access_mode, readonly_attested, "
            "pii_scope, owner_name, status FROM orion_catalog.source_bindings "
            "WHERE project_id=%s AND source_id=%s",
            (project_id, source.source_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"G-S1-SOURCE-SCOPE: {source.source_id} 缺少 SourceBinding。")
        binding = SourceBinding.model_validate(dict(zip(binding_fields, row, strict=True)))
        if binding.status != "ACTIVE":
            raise ValueError(f"G-S1-SOURCE-SCOPE: {source.source_id} 不是活动数据源。")
        bindings.append(binding)
        cursor.execute(
            "SELECT profile FROM orion_catalog.source_profiles WHERE project_id=%s "
            "AND source_id=%s ORDER BY profiled_at DESC LIMIT 1",
            (project_id, source.source_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"G-S1-SNAPSHOT-RECONCILIATION: {source.source_id} 缺少源画像回执。")
        source_profile = SourceProfileReceipt.model_validate(row[0])
        if source_profile.project_id != project_id or source_profile.source_id != source.source_id:
            raise ValueError("G-S1-SOURCE-SCOPE: 源画像身份不匹配。")
        original_tables = {table.table: table for table in source_profile.tables}
        source_profiles.append(source_profile.model_dump(mode="json"))
        source_rows = 0
        for table in source.tables:
            original = original_tables.get(table.table)
            if (
                original is None
                or original.schema_sha256 != table.schema_sha256
                or original.row_count != table.source_row_count
            ):
                raise ValueError(
                    f"G-S1-SNAPSHOT-RECONCILIATION: {table.table} 的源画像与快照不一致。"
                )
            if table.table not in binding.authorized_tables:
                raise ValueError(f"G-S1-SOURCE-SCOPE: {table.table} 超出授权范围。")
            target = table.target_table
            cursor.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema='orion_data' AND table_name=%s ORDER BY ordinal_position",
                (target,),
            )
            columns = cursor.fetchall()
            names = [item[0] for item in columns]
            if not {"dataset_id", "row_ordinal"}.issubset(names):
                raise ValueError(f"G-S1-SCHEMA: {target} 缺少快照身份列。")
            count_query = sql.SQL(
                "SELECT COUNT(*) AS row_count FROM orion_data.{} WHERE dataset_id=%s"
            ).format(sql.Identifier(target))
            cursor.execute(count_query, (source.dataset_id,))
            count = int(cursor.fetchone()[0])
            if count != table.snapshot_row_count:
                raise ValueError(
                    f"G-S1-SNAPSHOT-RECONCILIATION: {target} 实际 {count} 行，快照要求 {table.snapshot_row_count} 行。"
                )
            source_rows += count
            schema_tables.append(
                {
                    "name": target,
                    "source_id": source.source_id,
                    "source_table": table.table,
                    "dataset_id": source.dataset_id,
                    "columns": names,
                    "column_types": {item[0]: item[1] for item in columns},
                    "primary_key": ["dataset_id", "row_ordinal"],
                    "foreign_keys": [],
                    "row_count": count,
                }
            )
            profile_tables.append(
                {
                    "table": target,
                    "source_id": source.source_id,
                    "source_table": table.table,
                    "row_count": count,
                    "column_count": len(columns),
                    "empty": count == 0,
                }
            )
            result = {"row_count": count}
            # Counts are aggregate result values, not the number of returned rows.
            evidence.append(
                {
                    "id": f"SNAPSHOT-SQL-{len(evidence) + 1:03d}",
                    "purpose": f"精确回读 {table.table} 的当前版本快照行数",
                    "sql": sql.SQL(
                        "SELECT COUNT(*) AS row_count FROM orion_data.{} WHERE dataset_id={}"
                    )
                    .format(sql.Identifier(target), sql.Literal(source.dataset_id))
                    .as_string(),
                    "source_tables": [target],
                    "status": "PASSED",
                    "executed_via": "ORION_SNAPSHOT_HUB",
                    "executed_at": checked_at,
                    "expected_row_count": 1,
                    "actual_row_count": 1,
                    "result_value": count,
                    "result_sha256": _digest([result]),
                    "snapshot_set_id": snapshot.snapshot_set_id,
                    "dataset_id": source.dataset_id,
                }
            )
        datasets.append(
            {
                "project_id": project_id,
                "dataset_id": source.dataset_id,
                "source_id": source.source_id,
                "snapshot_set_id": snapshot.snapshot_set_id,
                "snapshot_version": source.snapshot_version,
                "row_count": source_rows,
                "source_sha256": source.source_sha256,
                "manifest_sha256": snapshot.manifest_sha256,
                "status": "READY",
                "registered_via": "ORION_SNAPSHOT_HUB",
                "production_evidence": True,
            }
        )
    rebuilt = build_cross_source_snapshot_set(
        project_id=project_id, bindings=bindings, snapshots=snapshot.source_snapshots
    )
    if (
        rebuilt.snapshot_set_id != snapshot.snapshot_set_id
        or rebuilt.manifest_sha256 != snapshot.manifest_sha256
    ):
        raise ValueError("G-S1-SNAPSHOT-RECONCILIATION: 快照集指纹不一致。")
    targets = [table["name"] for table in schema_tables]
    if len(targets) != len(set(targets)):
        raise ValueError("G-S1-SCHEMA: 不同来源的目标表名称冲突。")
    return {
        "project_id": project_id,
        "dataset_ids": dataset_ids,
        "s1": {
            "datasource_inventory": {
                "datasource_count": len(bindings),
                "datasources": [
                    {
                        "id": b.source_id,
                        "source_id": b.source_id,
                        "engine": "PostgreSQL",
                        "database": "orion_source_data",
                        "schema": "orion_data",
                        "connection_env": "ORION_SOURCE_DATA_READER_URL",
                        "database_principal": snapshot_reader_principal(),
                        "access_mode": "READ_ONLY_AFTER_IMPORT",
                        "via": "ORION Snapshot Hub",
                    }
                    for b in bindings
                ],
                "business_tables_scope": targets,
                "source_bindings": [b.model_dump(mode="json") for b in bindings],
                "cross_source_snapshot_set": snapshot.model_dump(mode="json"),
                "datasets": datasets,
            },
            "schema_snapshot": {
                "database": "orion_source_data",
                "schemas": ["orion_data"],
                "snapshot_set_id": snapshot.snapshot_set_id,
                "tables": schema_tables,
            },
            "data_profile": {
                "profile_mode": "FULL_IMPORT_WITH_EXACT_COUNTS",
                "profiled_at": checked_at,
                "snapshot_set_id": snapshot.snapshot_set_id,
                "source_profiles": source_profiles,
                "table_count": len(targets),
                "total_rows": sum(t["row_count"] for t in profile_tables),
                "empty_table_count": sum(t["empty"] for t in profile_tables),
                "tables": profile_tables,
            },
            "relation_candidates": [],
            "evidence_sql": evidence,
        },
    }
