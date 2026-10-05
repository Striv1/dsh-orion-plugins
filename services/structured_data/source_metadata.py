"""Read a version-bound snapshot schema without guessing columns or executing data SQL."""
from __future__ import annotations

import psycopg

from .pipeline import StructuredDataImportError, StructuredDataPipeline
from .source_evidence import (
    SNAPSHOT_SYSTEM_COLUMNS,
    _column_names,
    _hash,
    _match_snapshot_table,
    snapshot_evidence_binding,
    verify_snapshot_evidence_binding,
)


def describe_snapshot_source(*, reader_url: str, project_id: str, schema_snapshot: dict,
                             datasource_inventory: dict, describe_table: str) -> dict:
    if not isinstance(describe_table, str) or not describe_table:
        raise StructuredDataImportError("describe_table 必须为 S1 已登记表名")
    matches = _match_snapshot_table(schema_snapshot, describe_table)
    if len(matches) != 1:
        raise StructuredDataImportError("describe_table 须唯一匹配受控数据库快照；同名表使用 source_id.table 或物理表名")
    binding = snapshot_evidence_binding(project_id=project_id, schema_snapshot=schema_snapshot,
                                       datasource_inventory=datasource_inventory, selected=matches[0])
    if not reader_url:
        raise StructuredDataImportError("ORION_SOURCE_DATA_READER_URL 未配置，不能核验来源身份")
    pipeline = StructuredDataPipeline(reader_url)
    try:
        with psycopg.connect(pipeline.database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = 15000")
            verify_snapshot_evidence_binding(cursor, project_id=project_id, binding=binding)
    except psycopg.Error as exc:
        raise StructuredDataImportError(f"来源目录核验失败（SQLSTATE={exc.sqlstate or 'unknown'}）；不能将缓存结构当作有效来源。") from None
    selected = binding["selected"]
    columns = sorted(_column_names(selected) - SNAPSHOT_SYSTEM_COLUMNS)
    result = {
        "source_id": selected.get("source_id"), "source_table": binding["source_table"],
        "physical_table": binding["physical"], "dataset_id": binding["dataset_id"],
        "snapshot_set_id": binding["snapshot_set_id"],
        "columns": [{"name": name, "snapshot_sql_type": (selected.get("column_types") or {}).get(name, "unknown")}
                    for name in columns],
    }
    return {"status": "COMPLETE", "project_id": project_id, "source_schema": result,
            "schema_sha256": _hash(result), "source_sha256": binding["dataset"]["source_sha256"],
            "readonly_verified": True, "validation_scope": "SOURCE_SCHEMA_ONLY", "ontology_runtime_verified": False,
            "message": "字段来自当前工程 S1 固定快照，已复核实时目录身份；snapshot_sql_type 为快照存储类型（可能统一为 text），不是业务 datatype 或原库类型。技术行键不是业务唯一标识，需按已登记业务证据确认。未查询数据行、不做本体运行验收。"}
