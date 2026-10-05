"""Bounded relational evidence plans over current project-owned S1 snapshots.

The model supplies an AST, never SQL. Every source is version-filtered and
catalog-verified in the same read-only transaction as the result query.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime

import psycopg
from psycopg import sql

from .pipeline import StructuredDataImportError, StructuredDataPipeline
from .snapshot_hub import _json_value
from .source_evidence import (
    SNAPSHOT_SYSTEM_COLUMNS,
    _column_names,
    _hash,
    _match_snapshot_table,
    snapshot_evidence_binding,
    verify_snapshot_evidence_binding,
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
CASTS = {"text": "text", "decimal": "numeric", "integer": "bigint", "datetime": "timestamptz", "date": "date"}
OPS = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
AGGREGATES = {"count": "COUNT", "count_distinct": "COUNT", "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}


def _fail(message):
    raise StructuredDataImportError(message)


def _object(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        _fail(f"{label} 必须是对象且只能使用字段 {', '.join(allowed)}")
    return value


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        _fail("输出名和来源别名须为小写字母开头的 1–48 位字母/数字/下划线")
    return sql.Identifier(value).as_string()


def compile_source_query_plan(plan: dict, *, project_id: str, schema_snapshot: dict, datasource_inventory: dict):
    _object(plan, ("sources", "from", "joins", "select", "filters", "group_by", "having", "order_by", "limit"), "query_plan")
    sources = plan.get("sources")
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 4:
        _fail("sources 须声明 1–4 个别名到 S1 已登记快照表的映射")
    bindings = {}
    for alias, table in sources.items():
        _name(alias)
        if alias == "evidence_rows":
            _fail("evidence_rows 是平台保留来源别名")
        if not isinstance(table, str):
            _fail("sources 的值必须是 S1 来源表名或物理快照表名")
        matches = _match_snapshot_table(schema_snapshot, table)
        if len(matches) != 1:
            _fail("来源表未登记、存在歧义或不是受控数据库快照；请使用当前 S1 精确表名")
        bindings[alias] = snapshot_evidence_binding(project_id=project_id, schema_snapshot=schema_snapshot,
                            datasource_inventory=datasource_inventory, selected=matches[0])
    base = plan.get("from")
    if base not in bindings:
        _fail("from 必须引用 sources 中的起始别名")
    limit = plan.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        _fail("limit 必须为 1–100 的整数")
    params = []

    def field(ref):
        if not isinstance(ref, str) or ref.count(".") != 1:
            _fail("字段引用须为 来源别名.已登记业务列")
        alias, column = ref.split(".")
        if alias not in bindings or column not in (_column_names(bindings[alias]["selected"]) - SNAPSHOT_SYSTEM_COLUMNS):
            _fail(f"未登记的业务字段：{ref}")
        return sql.Identifier(alias, column).as_string()

    def expression(spec, aggregate=False):
        _object(spec, ("field", "cast", "normalize", "aggregate"), "字段表达式")
        agg = spec.get("aggregate")
        if agg is not None and (not aggregate or agg not in AGGREGATES):
            _fail("aggregate 仅在 select 使用 count/count_distinct/sum/avg/min/max")
        if agg == "count" and "field" not in spec:
            if set(spec) != {"aggregate"}:
                _fail("count(*) 不接受转换或归一操作")
            return "COUNT(*)"
        value = field(spec.get("field"))
        cast = spec.get("cast")
        normalize = spec.get("normalize")
        if cast is not None:
            if cast not in CASTS:
                _fail("cast 只接受 text/decimal/integer/datetime/date")
            value = f"CAST(NULLIF(CAST({value} AS text), '') AS {CASTS[cast]})"
        if normalize is not None:
            if normalize not in {"upper", "lower"} or cast not in {None, "text"}:
                _fail("normalize 只支持文本 upper/lower")
            value = f"{normalize.upper()}(CAST({value} AS text))"
        if agg:
            value = f"{AGGREGATES[agg]}({'DISTINCT ' if agg == 'count_distinct' else ''}{value})"
        return value

    def scalar(value):
        if (value is None or not isinstance(value, str | bool | int | float)
                or isinstance(value, float) and not math.isfinite(value)
                or isinstance(value, str) and len(value) > 2000):
            _fail("比较值须为长度不超过 2000 的文本或有限数值/布尔；空值使用 is_null/is_not_null")
        params.append(value)
        return "%s"

    def conditions(items, selected=None):
        if not isinstance(items, list) or len(items) > 12:
            _fail("filters/having 每组最多 12 项")
        result = []
        for item in items:
            allowed = ("field", "op", "value") if selected is not None else ("field", "cast", "normalize", "op", "value", "value_field")
            _object(item, allowed, "比较条件")
            if selected is not None:
                if item.get("field") not in selected:
                    _fail("having 须引用 select 输出名")
                left = selected[item["field"]]
            else:
                left = expression({k: item[k] for k in ("field", "cast", "normalize") if k in item})
            op = item.get("op")
            if op in {"is_null", "is_not_null"}:
                if "value" in item or "value_field" in item:
                    _fail("空值检查不接受比较值")
                result.append(f"{left} IS {'NOT ' if op == 'is_not_null' else ''}NULL")
            elif op in OPS:
                if ("value" in item) == ("value_field" in item):
                    _fail("比较条件必须且只能提供 value 或 value_field")
                right = expression({"field": item["value_field"], **{k: item[k] for k in ("cast", "normalize") if k in item}}) if "value_field" in item else scalar(item["value"])
                result.append(f"{left} {OPS[op]} {right}")
            else:
                _fail("op 只支持 eq/ne/lt/lte/gt/gte/is_null/is_not_null")
        return result

    ctes = []
    for alias, binding in bindings.items():
        # Project only registered business columns; dataset_id is never model-visible.
        columns = sorted(_column_names(binding["selected"]) - SNAPSHOT_SYSTEM_COLUMNS)
        if not columns:
            _fail("来源快照没有业务列")
        cols = ', '.join(sql.Identifier(c).as_string() for c in columns)
        ctes.append(f"{_name(alias)} AS (SELECT {cols} FROM orion_data.{sql.Identifier(binding['physical']).as_string()} WHERE dataset_id=%s)")
        params.append(binding["dataset_id"])
    joins = plan.get("joins", [])
    if not isinstance(joins, list) or len(joins) > 3:
        _fail("joins 最多 3 个等值连接，不允许笛卡尔积")
    joined = {base}
    from_sql = _name(base)
    for join in joins:
        _object(join, ("left", "right"), "join")
        left, right = field(join.get("left")), field(join.get("right"))
        la, ra = join["left"].split('.')[0], join["right"].split('.')[0]
        if (la in joined) == (ra in joined):
            _fail("每个 join 必须把一个新来源连接到此前已连接来源")
        new_alias = ra if la in joined else la
        joined.add(new_alias)
        from_sql += f" JOIN {_name(new_alias)} ON {left}={right}"
    if joined != set(bindings):
        _fail("所有来源必须通过 joins 连通，不允许未连接来源")
    selections = plan.get("select")
    if not isinstance(selections, dict) or not 1 <= len(selections) <= 12:
        _fail("select 须为 1–12 个输出名到表达式的映射")
    selected = {_name(alias): expression(spec, aggregate=True) for alias, spec in selections.items()}
    nonagg = {name for name, spec in selections.items() if not spec.get('aggregate')}
    has_agg = len(nonagg) != len(selections)
    group = plan.get('group_by', [])
    if (not isinstance(group, list) or any(not isinstance(v, str) for v in group)
            or len(group) > 6 or len(group) != len(set(group)) or not set(group).issubset(nonagg)
            or has_agg and set(group) != nonagg or group and set(group) != nonagg):
        _fail("group_by 须恰好列出全部非聚合输出名（纯聚合用 []），最多 6 项")
    where = conditions(plan.get('filters', []))
    having = conditions(plan.get('having', []), {name: selected[_name(name)] for name in selections})
    if having and not (group or has_agg):
        _fail("having 仅用于分组/聚合结果")
    statement = 'WITH ' + ', '.join(ctes) + ', evidence_rows AS (SELECT '
    statement += ', '.join(f'{expr} AS {alias}' for alias, expr in selected.items()) + ' FROM ' + from_sql
    if where:
        statement += ' WHERE ' + ' AND '.join(where)
    if group:
        statement += ' GROUP BY ' + ', '.join(selected[_name(name)] for name in group)
    if having:
        statement += ' HAVING ' + ' AND '.join(having)
    orders = plan.get('order_by') or [{"field": name, "direction": "ASC"} for name in selections]
    if not isinstance(orders, list) or not 1 <= len(orders) <= 12:
        _fail('order_by 须为 1–12 项')
    order = []
    for item in orders:
        _object(item, ('field', 'direction'), '排序')
        if item.get('field') not in selections or item.get('direction', 'ASC') not in {'ASC', 'DESC'}:
            _fail('排序必须引用 select 输出名，direction 为 ASC/DESC')
        order.append(_name(item['field']) + ' ' + item.get('direction', 'ASC'))
    statement += ') SELECT ' + ', '.join(selected) + ', COUNT(*) OVER () FROM evidence_rows ORDER BY ' + ', '.join(order) + ' LIMIT %s'
    params.append(limit + 1)
    return statement, params, bindings, list(selections), limit


def query_source_plan(*, reader_url: str, project_id: str, schema_snapshot: dict,
                      datasource_inventory: dict, query_plan: dict) -> dict:
    if not reader_url:
        _fail("ORION_SOURCE_DATA_READER_URL 未配置，不能回退写连接")
    statement, params, bindings, fields, limit = compile_source_query_plan(query_plan, project_id=project_id,
                                            schema_snapshot=schema_snapshot, datasource_inventory=datasource_inventory)
    pipeline = StructuredDataPipeline(reader_url)
    try:
        with psycopg.connect(pipeline.database_url) as connection, connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cursor.execute('SET LOCAL statement_timeout = 15000')
            cursor.execute('SET LOCAL TIME ZONE \'UTC\'')
            for binding in bindings.values():
                verify_snapshot_evidence_binding(cursor, project_id=project_id, binding=binding)
            cursor.execute(statement, params)
            raw = cursor.fetchall()
    except psycopg.Error as exc:
        raise StructuredDataImportError(f"来源查询未完成（SQLSTATE={exc.sqlstate or 'unknown'}）；请检查字段转换、窗口参数或查询规模，不以失败作为空结果。") from None
    rows = [{field: _json_value(value) for field, value in zip(fields, row[:-1], strict=True)} for row in raw[:limit]]
    # Limit returned cell text explicitly; a truncated cell is never an exact baseline.
    truncated_cells = []
    for index, row in enumerate(rows):
        for field, value in row.items():
            if isinstance(value, str) and len(value) > 2000:
                row[field] = value[:2000]
                truncated_cells.append({"row": index, "field": field})
    result = {"rows": rows, "result_fields": fields, "total_row_count": int(raw[0][-1]) if raw else 0,
              "returned_row_count": len(rows), "truncated": len(raw) > limit, "truncated_cells": truncated_cells}
    return {"status": "COMPLETE", "project_id": project_id, **result, "readonly_verified": True,
            "query_plan": query_plan, "query_sha256": _hash({"sql": statement, "parameters": params}),
            "result_sha256": _hash(result), "observed_at": datetime.now(UTC).isoformat(),
            "sources": [{"alias": alias, "source_table": binding['source_table'], "dataset_id": binding['dataset_id'],
                         "snapshot_set_id": binding['snapshot_set_id'], "source_sha256": binding['dataset']['source_sha256']}
                        for alias, binding in bindings.items()],
            "validation_scope": "SOURCE_EVIDENCE_ONLY", "ontology_runtime_verified": False,
            "message": "结果来自当前 S1 快照全量查询，返回行可能截断；不是 Ontop、规则执行或 S6/S7 验收。业务语义与本体查询仍须独立对账。"}
