"""Normalize submitted S1 schema shapes without storage or orchestration."""
from __future__ import annotations

from typing import Any

from .errors import WorkflowGateError


def normalize_s1_columns(raw_columns: Any, *, table_name: str) -> list[str]:
    if raw_columns is None:
        return []
    if isinstance(raw_columns, str):
        candidates: list[Any] = [
            definition.strip() for definition in raw_columns.split(",") if definition.strip()
        ]
    elif isinstance(raw_columns, list):
        candidates = raw_columns
    else:
        raise WorkflowGateError(
            "G-S1-SCHEMA",
            f"表 {table_name} 的 columns 必须是数组或逗号分隔的字段定义。",
        )

    columns: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(candidates, start=1):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("column_name") or "").strip()
        else:
            definition = str(item).strip()
            name = definition.split(maxsplit=1)[0] if definition else ""
        name = name.strip('"`')
        if not name:
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                f"表 {table_name} 的第 {index} 个字段缺少名称。",
            )
        if name not in seen:
            seen.add(name)
            columns.append(name)
    return columns

def normalize_s1_schema_snapshot( schema_snapshot: Any) -> dict[str, Any]:
    if isinstance(schema_snapshot, list):
        normalized: dict[str, Any] = {}
        declared_tables: Any = schema_snapshot
    elif isinstance(schema_snapshot, dict):
        normalized = dict(schema_snapshot)
        declared_tables = normalized.get("tables") or []
    else:
        raise WorkflowGateError(
            "G-S1-SCHEMA",
            "Schema 快照必须是对象；兼容格式可以直接提供表定义数组。",
        )

    if not isinstance(declared_tables, list):
        raise WorkflowGateError("G-S1-SCHEMA", "Schema 快照的 tables 必须是数组。")

    table_columns = normalized.get("table_columns") or {}
    if not isinstance(table_columns, dict):
        raise WorkflowGateError("G-S1-SCHEMA", "Schema 快照的 table_columns 必须是对象。")
    normalized_columns = {
        str(name): normalize_s1_columns(columns, table_name=str(name))
        for name, columns in table_columns.items()
    }

    primary_key_map = normalized.get("primary_key_map") or {}
    if not isinstance(primary_key_map, dict):
        raise WorkflowGateError("G-S1-SCHEMA", "Schema 快照的 primary_key_map 必须是对象。")
    normalized_primary_keys: dict[str, list[str]] = {}
    for name, value in primary_key_map.items():
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                f"表 {name} 的主键必须是字符串或数组。",
            )
        normalized_primary_keys[str(name)] = [
            str(item).strip() for item in values if str(item).strip()
        ]

    normalized_tables: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(declared_tables, start=1):
        if not isinstance(item, dict):
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                f"Schema 快照的第 {index} 个表定义必须是对象。",
            )
        name = str(item.get("name") or item.get("table") or "").strip()
        if not name or name in seen_names:
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                f"Schema 快照的第 {index} 个表名称缺失或重复。",
            )
        seen_names.add(name)
        columns = normalize_s1_columns(item.get("columns"), table_name=name)
        primary_key = item.get("primary_key") or item.get("primary_keys") or []
        if isinstance(primary_key, str):
            primary_key = [primary_key]
        if not isinstance(primary_key, list):
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                f"表 {name} 的 primary_key 必须是字符串或数组。",
            )
        normalized_item = {
            **item,
            "name": name,
            "columns": columns,
            "primary_key": [str(value).strip() for value in primary_key if str(value).strip()],
        }
        normalized_item.pop("table", None)
        normalized_tables.append(normalized_item)
        if columns:
            normalized_columns[name] = columns
        if normalized_item["primary_key"]:
            normalized_primary_keys[name] = normalized_item["primary_key"]

    normalized["tables"] = normalized_tables
    normalized["table_columns"] = normalized_columns
    normalized["primary_key_map"] = normalized_primary_keys
    return normalized
