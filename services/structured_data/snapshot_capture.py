"""Capture one authorized read-only database source into Snapshot Hub.

Shared by the workflow MCP tool `capture_database_snapshot` and the CLI
`scripts/import_database_snapshot.py` so the 3081 agent can finish S1 source
intake without an operator shell. `env://` and opaque Chat2DB datasource
references are accepted; credential values never enter arguments or receipts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .multi_source import SourceBinding
from .snapshot_hub import PostgresSourceReader, SnapshotHub

SOURCE_ENV_PATTERN = re.compile(r"^ORION_[A-Z0-9_]+_SOURCE_URL$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class SnapshotCaptureError(ValueError):
    """Raised when a capture request violates the source intake contract."""


def list_source_connections() -> dict[str, Any]:
    """Return configured env:// source references without revealing values."""

    names = sorted(
        name for name, value in os.environ.items()
        if SOURCE_ENV_PATTERN.match(name) and str(value).strip()
    )
    return {
        "connections": [{"connection_ref": f"env://{name}", "connection_env": name} for name in names],
        "target_configured": bool(str(os.getenv("ORION_SOURCE_DATA_URL") or "").strip()),
        "providers": [{"provider": "chat2db", "edition": "community",
                       "supported_engines": ["POSTGRESQL"],
                       "reference_format": "chat2db://community/<datasource_id>",
                       "requires_readonly_transaction": True,
                       "execution_mode": "STRUCTURED_CLI_SNAPSHOT",
                       "large_data_note": "这是有预算的完整快照采集，不是增量CDC或巨量数据实时分析通道。"}],
        "note": "可复用 Chat2DB 已配置的 PostgreSQL 只读连接编号；已有 env 引用继续兼容。凭据值不会返回。",
    }


def _parse_exclusions(values: list[str] | dict[str, list[str]] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if isinstance(values, dict):
        for table, columns in values.items():
            result.setdefault(str(table).strip(), set()).update(str(c).strip() for c in columns)
        return result
    for value in values or []:
        table, separator, column = str(value).partition(":")
        if not separator or not table.strip() or not column.strip():
            raise SnapshotCaptureError("排除字段必须使用 table:column")
        result.setdefault(table.strip(), set()).add(column.strip())
    return result


def capture_database_snapshot(
    *,
    project_id: str,
    source_id: str,
    connection_env: str | None = None,
    chat2db_datasource_id: int | None = None,
    database: str,
    tables: list[str],
    pii_scope: str,
    owner: str,
    schema: str = "public",
    exclude_columns: list[str] | dict[str, list[str]] | None = None,
    dataset_type: str = "TEST_ONLY",
    production_evidence_basis: str | None = None,
    snapshot_version: str | None = None,
    hub_factory: Callable[[str], Any] = SnapshotHub,
    profiler: Any | None = None,
    scope_preflight: Callable[[SourceBinding, Any | None], None] | None = None,
) -> dict[str, Any]:
    """Register, profile, capture and atomically promote one PostgreSQL source."""

    connection_env = str(connection_env or "").strip()
    if bool(connection_env) == (chat2db_datasource_id is not None):
        raise SnapshotCaptureError("connection_env 与 chat2db_datasource_id 必须且只能提供一个")
    if chat2db_datasource_id is not None:
        if type(chat2db_datasource_id) is not int or not 1 <= chat2db_datasource_id <= 10**19 - 1:
            raise SnapshotCaptureError("chat2db_datasource_id 必须是有效正整数")
        connection_ref = f"chat2db://community/{chat2db_datasource_id}"
    else:
        if not SOURCE_ENV_PATTERN.match(connection_env):
            raise SnapshotCaptureError(
                "connection_env 必须是 ORION_<NAME>_SOURCE_URL 形式的已登记只读连接变量"
            )
        if not str(os.getenv(connection_env) or "").strip():
            raise SnapshotCaptureError(f"{connection_env} 未配置；请先在 .env 登记只读连接并重启 3081")
        connection_ref = f"env://{connection_env}"
    target_url = str(os.getenv("ORION_SOURCE_DATA_URL") or "").strip()
    if not target_url:
        raise SnapshotCaptureError("ORION_SOURCE_DATA_URL 未配置")
    for label, value in (("source_id", source_id), ("database", database), ("schema", schema)):
        if not value or not re.match(r"^[A-Za-z0-9_\-]+$", str(value)):
            raise SnapshotCaptureError(f"{label} 只能包含字母、数字、下划线和连字符")
    if not pii_scope.strip() or not owner.strip():
        raise SnapshotCaptureError("pii_scope 与 owner 必须明确登记")
    basis = str(production_evidence_basis or "").strip()
    if dataset_type not in {"PRODUCTION", "TEST_ONLY"}:
        raise SnapshotCaptureError("dataset_type 只能是 PRODUCTION 或 TEST_ONLY")
    if dataset_type == "PRODUCTION" and len(basis) < 8:
        raise SnapshotCaptureError(
            "PRODUCTION 快照必须提供 production_evidence_basis：写明授权人、授权依据与数据性质"
        )
    if dataset_type == "TEST_ONLY" and basis:
        raise SnapshotCaptureError("TEST_ONLY 快照不能声明生产证据依据")

    normalized_tables = list(dict.fromkeys(str(t).strip() for t in tables or []))
    if not normalized_tables or any(not _IDENTIFIER.match(t.split(".")[-1]) for t in normalized_tables):
        raise SnapshotCaptureError("tables 必须是明确的表名列表，不能为空或使用通配")
    exclusions = _parse_exclusions(exclude_columns)
    unknown_tables = set(exclusions) - set(normalized_tables)
    if unknown_tables:
        raise SnapshotCaptureError("排除字段引用了未授权表：" + ", ".join(sorted(unknown_tables)))

    discovery = SourceBinding(
        project_id=project_id,
        source_id=source_id,
        engine="POSTGRESQL",
        connection_ref=connection_ref,
        database=database,
        schemas=[schema],
        authorized_tables=normalized_tables,
        readonly_attested=True,
        pii_scope=pii_scope,
        owner=owner,
    )
    if scope_preflight is not None:
        scope_preflight(discovery, None)
    if profiler is None and chat2db_datasource_id is not None:
        from .chat2db_source import Chat2DBSourceReader

        profiler = Chat2DBSourceReader()
    profile = (profiler or PostgresSourceReader()).profile(discovery)
    allowed: dict[str, list[str]] = {}
    for table_profile in profile.tables:
        excluded = exclusions.get(table_profile.table, set())
        existing = {item.name for item in table_profile.columns}
        missing = excluded - existing
        if missing:
            raise SnapshotCaptureError(f"{table_profile.table} 排除字段不存在：" + ", ".join(sorted(missing)))
        columns = [item.name for item in table_profile.columns if item.name not in excluded]
        if not columns:
            raise SnapshotCaptureError(f"{table_profile.table} 排除后没有可导入字段")
        allowed[table_profile.table] = columns

    binding = discovery.model_copy(update={"authorized_columns": allowed})
    if scope_preflight is not None:
        retained_profile = profile.model_copy(update={"tables": [
            table.model_copy(update={"columns": [c for c in table.columns if c.name in allowed[table.table]]})
            for table in profile.tables
        ]})
        scope_preflight(binding, retained_profile)
    hub = hub_factory(target_url)
    hub.register_sources([binding])
    hub.profile_sources([binding])
    version = snapshot_version or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot_set = hub.capture_and_promote(
        project_id=project_id,
        bindings=[binding],
        snapshot_version=version,
        dataset_type=dataset_type,
        production_evidence=dataset_type == "PRODUCTION",
    )
    return {
        "project_id": project_id,
        "source_id": source_id,
        "connection_ref": binding.connection_ref,
        "provider": "chat2db" if chat2db_datasource_id is not None else "registered_connection",
        **({"capture_mode": "PAGED_FULL_PROJECTION_CHECKSUM", "capture_limits": {
            "supported_engines": ["POSTGRESQL"], "max_rows_per_table": 1_000_000,
            "max_bytes_per_table": 256 * 1024 * 1024, "page_size": 999,
            "cost": "每页重新核验完整投影，约 O(pages * rows)；巨量分析应走源端聚合。",
            "incremental_cdc": False,
        }} if chat2db_datasource_id is not None else {}),
        "dataset_type": dataset_type,
        "production_evidence_basis": basis or None,
        "snapshot_set_id": snapshot_set.snapshot_set_id,
        "snapshot_version": snapshot_set.snapshot_version,
        "snapshot_complete": snapshot_set.snapshot_complete,
        "production_evidence": snapshot_set.production_evidence,
        "manifest_sha256": snapshot_set.manifest_sha256,
        "dataset_ids": [item.dataset_id for item in snapshot_set.source_snapshots],
        "sources": [
            {
                "dataset_id": item.dataset_id,
                "source_sha256": item.source_sha256,
                "table_count": len(item.tables),
                "source_rows": sum(t.source_row_count for t in item.tables),
                "snapshot_rows": sum(t.snapshot_row_count for t in item.tables),
                "tables": [
                    {"source": t.table, "target": t.target_table, "rows": t.snapshot_row_count}
                    for t in item.tables
                ],
            }
            for item in snapshot_set.source_snapshots
        ],
        "next_action": "调用 record_data_understanding_from_datasets，传入 dataset_ids 生成 S1。",
    }
