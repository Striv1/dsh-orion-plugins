"""Separate reviewed business citations from resolved execution dependencies.

Dependencies come from the tables and columns actually used by the compiler,
never from free-text citations or a second model-authored source declaration.
"""
from __future__ import annotations


def source_dependency(table: dict, columns) -> dict:
    columns = sorted(set(columns))
    if (not columns or not set(columns) <= set(table.get("columns", []))
            or any(not str(table.get(key) or "").strip()
                   for key in ("source_id", "source_table", "name", "dataset_id"))):
        raise ValueError("执行来源必须是已解析的登记快照及其实际业务列。")
    return {"source_id": table["source_id"], "source_table": table["source_table"],
            "snapshot_table": table["name"], "dataset_id": table["dataset_id"], "columns": columns}


def source_capability(dependencies) -> dict:
    tables, columns = {}, {}
    for dependency in dependencies:
        sid = dependency["source_id"]
        tables.setdefault(sid, set()).add(dependency["source_table"])
        columns.setdefault(sid, set()).update(dependency["columns"])
    return {
        "source_ids": sorted(tables),
        "source_tables": sorted({t for values in tables.values() for t in values}),
        "source_columns": sorted({c for values in columns.values() for c in values}),
        "source_tables_by_id": {sid: sorted(tables[sid]) for sid in sorted(tables)},
        "source_columns_by_id": {sid: sorted(columns[sid]) for sid in sorted(columns)},
    }


def business_evidence_refs(mappings, rules=()) -> set[str]:
    """Known citations from reviewed mappings/rules, without creating lineage."""
    return {str(value) for item in [*mappings, *rules] if isinstance(item, dict)
            for value in [item.get("id"), *(item.get("source_refs") or [])] if value}
