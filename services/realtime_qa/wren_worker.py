"""Standalone worker: official Wren MDL planner + its read-only DuckDB connector.

Invoked by the platform with a validated bounded plan, never arbitrary SQL.
Uses a disposable local database; no source database or LLM connection.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sys
from datetime import date, datetime
from decimal import Decimal
from importlib.metadata import version
from tempfile import TemporaryDirectory


def _literal(value):
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if type(value) in {int, float} and math.isfinite(value):
        return str(value)
    raise ValueError("无效过滤值。")


def execute(payload):
    import duckdb
    from wren import WrenEngine
    from wren.config import WrenConfig

    rows, columns, plan = payload["rows"], payload["columns"], payload["plan"]
    lookup = {column["field"]: column for column in columns}

    def field(name):
        if name not in lookup:
            raise ValueError(f"字段 {name} 不在本次证据结果中。")
        return lookup[name]

    dimensions = []
    for dimension in plan["dimensions"]:
        column = field(dimension["field"])
        expression = column["column"]
        if dimension["period"] != "value":
            # Interpret only explicit ISO dates; never guess timezone/date format.
            for row in rows:
                value = row.get(column["field"])
                if value is not None:
                    if not isinstance(value, str) or len(value) != 10:
                        raise ValueError("时间分组仅接受 YYYY-MM-DD 日期；时间戳须先明确时区和日期口径。")
                    date.fromisoformat(value)
            expression = f"CAST(DATE_TRUNC('{dimension['period']}', CAST({expression} AS DATE)) AS VARCHAR)"
        dimensions.append(expression)
    metrics = []
    for metric in plan["metrics"]:
        operation = metric["operation"]
        if operation == "count_rows":
            expression = "COUNT(*)"
        else:
            column = field(metric["field"])
            if operation in {"sum", "avg", "min", "max"} and column["type"] not in {"bigint", "double"}:
                raise ValueError(f"指标 {metric['name']} 需要已确认的数值字段；不自动转换文本或未知值。")
            argument = column["column"]
            expression = (f"COUNT(DISTINCT {argument})" if operation == "count_distinct"
                          else f"COUNT({argument})" if operation == "count_non_null"
                          else f"{operation.upper()}({argument})")
        metrics.append(f'{expression} AS "{metric["name"]}"')
    predicates = []
    operators = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for item in plan["filters"]:
        column = field(item["field"])
        left, operation = column["column"], item["operator"]
        if operation in {"is_null", "not_null"}:
            predicates.append(f"{left} IS {'NOT ' if operation == 'not_null' else ''}NULL")
        else:
            value = item["value"]
            compatible = ((column["type"] in {"bigint", "double"} and type(value) in {int, float})
                          or (column["type"] == "boolean" and type(value) is bool)
                          or (column["type"] == "varchar" and isinstance(value, str)))
            if not compatible:
                raise ValueError(f"过滤值与字段 {item['field']} 的类型不一致。")
            predicates.append(f"{left} {operators[operation]} {_literal(value)}")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    group_select = [f"{expression} AS group_{index+1}" for index, expression in enumerate(dimensions)]
    group = " GROUP BY " + ", ".join(dimensions) if dimensions else ""
    # Stable ordering supports both repeatable charts and pagination.
    order = (" ORDER BY " + ", ".join(f"group_{i+1} ASC NULLS LAST" for i in range(len(dimensions)))) if dimensions else ""
    sql = "SELECT " + ", ".join([*group_select, *metrics]) + " FROM source_rows" + where + group + order
    details_sql = "SELECT source_row" + (", " + ", ".join(group_select) if dimensions else "") + " FROM source_rows" + where + " ORDER BY source_row"
    mdl = {"catalog": "orion", "schema": "analysis", "models": [{"name": "source_rows",
        "refSql": "SELECT * FROM receipt.main.input_rows", "primaryKey": "source_row",
        "columns": [{"name": "source_row", "type": "bigint"}, *[
            {"name": column["column"], "type": "varchar" if column["type"] == "unknown" else column["type"],
             "properties": {"description": column["field"]}} for column in columns]]}],
        "relationships": [], "views": []}
    mdl_text = json.dumps(mdl, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    with TemporaryDirectory(prefix="orion-wren-") as directory:
        connection = duckdb.connect(directory + "/receipt.duckdb")
        definitions = ["source_row BIGINT", *[f"{column['column']} {'VARCHAR' if column['type'] == 'unknown' else column['type']}" for column in columns]]
        connection.execute("CREATE TABLE input_rows (" + ", ".join(definitions) + ")")
        if rows:
            values = [[index, *[row.get(column["field"]) for column in columns]] for index, row in enumerate(rows)]
            connection.executemany("INSERT INTO input_rows VALUES (" + ",".join("?" for _ in definitions) + ")", values)
        connection.close()
        engine = WrenEngine(base64.b64encode(mdl_text.encode()).decode(), "duckdb",
            {"url": directory, "format": "duckdb"}, config=WrenConfig(strict_mode=True), fallback=False)
        expanded = engine.dry_plan(sql)
        engine.dry_run(sql)
        results = engine.query(sql, limit=plan["limit"] + 1).to_pylist()
        details = engine.query(details_sql, limit=len(rows) + 1).to_pylist()
    displayed = results[:plan["limit"]]
    groups = []
    for result in displayed:
        indices = [item["source_row"] for item in details if all(
            item[f"group_{i+1}"] == result[f"group_{i+1}"] for i in range(len(dimensions)))]
        groups.append({"source_row_numbers": indices, "matched_row_count": len(indices)})
    return {"rows": displayed, "variables": [*(f"group_{i+1}" for i in range(len(dimensions))), *(m["name"] for m in plan["metrics"])],
        "truncated": len(results) > plan["limit"], "engine": "WrenAI", "engine_version": version("wrenai"),
        "core_version": version("wren-core-py"), "duckdb_version": version("duckdb"),
        "mdl": mdl, "mdl_sha256": "sha256:" + hashlib.sha256(mdl_text.encode()).hexdigest(),
        "sql": sql, "expanded_sql": expanded, "details_sql": details_sql,
        "groups": groups, "source_row_count": len(rows), "matched_row_count": len(details),
        "strict_mode": True, "fallback": False}


def json_value(item):
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral_value() else str(item)
    if isinstance(item, date | datetime):
        return item.isoformat()
    raise TypeError(f"Unsupported result type: {type(item).__name__}")


if __name__ == "__main__":
    try:
        output = execute(json.load(sys.stdin))
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, default=json_value))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:1500]}, ensure_ascii=False))
