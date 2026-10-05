"""Pinned Wren subprocess: governed PostgreSQL pushdown or isolated fixtures.

No path, credential or model supplied by an agent is accepted by the API caller.
The worker adds policy/identity validation before the official planning pipeline.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_ROWS = 50000
MAX_BYTES = 32 * 1024 * 1024
MAX_RESULT_ROWS = 5000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")
FUNCTIONS = {"count", "sum", "avg", "min", "max", "abs", "round", "ceil", "ceiling", "floor", "lower", "upper",
             "length", "char_length", "coalesce", "nullif", "cast", "try_cast", "date_trunc", "timestamp_trunc",
             "extract", "date", "date_part", "substring", "trim", "concat", "concat_ws", "greatest", "least",
             "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "if", "case"}


class AnalysisError(ValueError):
    """Safe public policy failure; upstream exceptions are never echoed."""


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def ident(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise AnalysisError("分析标识不受支持。")
    return '"' + value + '"'


def validate_sql(sql, manifest, *, dialect="duckdb", allow_top_limit=False):
    """Close upstream physical-table bypass and prove declared JOIN grains.
    """
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not 1 <= len(sql) <= 20000:
        raise AnalysisError("分析 SQL 长度不受支持。")
    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except Exception as exc:
        raise AnalysisError("分析 SQL 无法解析。") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise AnalysisError("分析只允许单条 SELECT。")
    ast = statements[0]
    if ast.find(exp.Offset) or any(not allow_top_limit or node is not ast.args.get("limit") for node in ast.find_all(exp.Limit)):
        raise AnalysisError("查询不能含 LIMIT/OFFSET 或对子查询抽样；结果数量由平台参数控制。")
    if sum(1 for _ in ast.walk()) > 1500:
        raise AnalysisError("分析 SQL 复杂度超过预算。")
    if ast.find(exp.TableSample):
        raise AnalysisError("分析不能抽样读取来源；请使用明确业务条件。")
    if ast.find(exp.Into) or ast.find(exp.Lock):
        raise AnalysisError("分析禁止锁或写入目标。")
    _validate_joins(ast, manifest, dialect)
    names = {m["name"].lower() for m in manifest.get("models", []) + manifest.get("views", [])}
    ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    if any(c.args.get("recursive") for c in ast.find_all(exp.With)):
        raise AnalysisError("分析不支持递归 CTE。")
    for table in ast.find_all(exp.Table):
        if table.catalog or table.db or not isinstance(table.this, exp.Identifier):
            raise AnalysisError("禁止物理库表限定和表函数；只能引用已发布分析模型。")
        if table.name.lower() not in names | ctes:
            raise AnalysisError("查询引用未发布的分析模型。")
    for column in ast.find_all(exp.Column):
        if column.catalog or column.db:
            raise AnalysisError("禁止物理库字段限定；使用模型字段或已发布关系字段。")
    for func in ast.find_all(exp.Func):
        if isinstance(func, exp.And | exp.Or | exp.Not):
            continue
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if name.lower() not in FUNCTIONS:
            raise AnalysisError("查询使用了未允许的分析函数。")
    # SELECT without any governed source is not a business analysis request.
    if not any(t.name.lower() in names for t in ast.find_all(exp.Table)):
        raise AnalysisError("分析查询必须读取已发布模型。")
    return sql


def _validate_joins(ast, manifest, dialect):
    """Prove declared key lineage and target uniqueness before accepting JOINs.

    A CTE grouped by projected business keys establishes a unique grain. Two
    separately aggregated child relations can therefore join without multiplying
    money. Arbitrary derived keys and unproved many-to-many joins stay blocked.
    """
    from sqlglot import exp, parse_one

    models = {m["name"].lower(): m for m in manifest.get("models", [])}
    sources = {}
    for name, model in models.items():
        columns = {c["name"].lower(): (name, c.get("properties", {}).get("sourceColumn", c["name"]).lower())
                   for c in model.get("columns", []) if not c.get("relationship")}
        primary = str(model.get("primaryKey", "_key")).lower()
        sources[name] = {"columns": columns, "unique": [{columns[primary]}] if primary in columns else []}
    edges = set()
    for relation in manifest.get("relationships", []):
        condition = parse_one(relation["condition"], dialect=dialect)
        if not isinstance(condition, exp.EQ) or not all(isinstance(c, exp.Column) for c in (condition.this, condition.expression)):
            continue
        def token(column):
            return sources.get(column.table.lower(), {}).get("columns", {}).get(column.name.lower())
        left, right = token(condition.this), token(condition.expression)
        if left and right and relation.get("joinType") != "MANY_TO_MANY":
            edges.add(frozenset((left, right)))

    def resolve(column, aliases):
        if not isinstance(column, exp.Column):
            return None
        if column.table:
            return aliases.get(column.table.lower(), {}).get("columns", {}).get(column.name.lower())
        matches = [source["columns"][column.name.lower()] for source in aliases.values() if column.name.lower() in source["columns"]]
        return matches[0] if len(matches) == 1 else None

    def analyze(select):
        if not isinstance(select, exp.Select):
            raise AnalysisError("关联预聚合只支持有明确业务键的 SELECT。")
        from_node = select.args.get("from") or select.args.get("from_")
        base = from_node.this if from_node else None
        joins = select.args.get("joins") or []
        if not isinstance(base, exp.Table) or base.name.lower() not in sources:
            if joins:
                raise AnalysisError("关联必须从已发布模型或已核验粒度的 CTE 开始。")
            return {"columns": {}, "unique": []}
        source = sources[base.name.lower()]
        aliases = {base.alias_or_name.lower(): source}
        fanouts = 0
        has_aggregation = bool(select.args.get("group") or any(expression.find(exp.AggFunc) for expression in select.expressions))
        for join in joins:
            target = join.this
            if (not isinstance(target, exp.Table) or target.name.lower() not in sources
                    or str(join.args.get("side") or "").upper() not in {"", "LEFT"}
                    or str(join.args.get("kind") or "").upper() not in {"", "INNER", "OUTER"}
                    or not join.args.get("on") or join.args.get("using")):
                raise AnalysisError("只允许已发布关系的 INNER/LEFT 等值关联。")
            target_alias = target.alias_or_name.lower()
            if target_alias in aliases:
                raise AnalysisError("关联别名重复。")
            target_source = sources[target.name.lower()]
            target_keys = set()
            predicates = join.args["on"].flatten() if isinstance(join.args["on"], exp.And) else [join.args["on"]]
            for predicate in predicates:
                if not isinstance(predicate, exp.EQ) or not all(isinstance(c, exp.Column) for c in (predicate.this, predicate.expression)):
                    continue
                a, b = predicate.this, predicate.expression
                if a.table.lower() == target_alias:
                    a, b = b, a
                if b.table.lower() != target_alias or a.table.lower() not in aliases:
                    continue
                left, right = resolve(a, aliases), resolve(b, {target_alias: target_source})
                if left and right and (left == right or frozenset((left, right)) in edges):
                    target_keys.add(right)
            if not target_keys:
                raise AnalysisError("JOIN 条件没有匹配已发布关系键。")
            unique = any(key <= target_keys for key in target_source["unique"])
            fanouts += int(not unique)
            if fanouts > 1 or (fanouts and has_aggregation):
                raise AnalysisError("一对多关联可能重复计数；请先按业务主键分别汇总再关联。")
            aliases[target_alias] = target_source
        columns = {}
        for expression in select.expressions:
            item = expression.this if isinstance(expression, exp.Alias) else expression
            if isinstance(item, exp.Star):
                for alias_source in aliases.values():
                    columns.update(alias_source["columns"])
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                columns.update(aliases.get(item.table.lower(), {}).get("columns", {}))
            else:
                columns[expression.alias_or_name.lower()] = resolve(item, aliases)
        projected = {token for token in columns.values() if token is not None}
        group = select.args.get("group")
        if group:
            group_keys = [resolve(column, aliases) for column in group.expressions]
            unique = [set(group_keys)] if group_keys and all(key is not None and key in projected for key in group_keys) else []
        elif not fanouts and not has_aggregation:
            unique = [key for key in source["unique"] if key <= projected]
        else:
            unique = []
        return {"columns": columns, "unique": unique}

    ctes = list((ast.args.get("with") or ast.args.get("with_") or exp.With()).expressions)
    for cte in ctes:
        name = cte.alias_or_name.lower()
        if name in sources or cte.args.get("alias").args.get("columns"):
            raise AnalysisError("CTE 名称冲突或列重命名粒度不明确。")
        sources[name] = analyze(cte.this)
    analyzed = {id(ast), *[id(cte.this) for cte in ctes]}
    analyze(ast)
    for select in ast.find_all(exp.Select):
        if id(select) not in analyzed and select.args.get("joins"):
            analyze(select)


def _bind_parameters(sql, parameters, dialect):
    """Render typed parameter AST literals; pattern wildcards default to text."""
    from sqlglot import exp, parse

    if not isinstance(parameters, dict) or len(parameters) > 30:
        raise AnalysisError("SQL 参数必须为不超过30项的对象。")
    statements = parse(sql, dialect=dialect)
    if len(statements) != 1:
        raise AnalysisError("分析只允许单条 SQL。")
    ast = statements[0]
    used = set()
    def literal(value):
        if value is None:
            return exp.Null()
        if type(value) is bool:
            return exp.Boolean(this=value)
        if type(value) in {int, float} and math.isfinite(value):
            return exp.Literal.number(str(value))
        if isinstance(value, str) and len(value) <= 2000 and "\x00" not in value:
            return exp.Literal.string(value)
        raise AnalysisError("分析参数只支持有界标量。")
    def replace(node):
        if isinstance(node, exp.Like | exp.ILike) and isinstance(node.expression, exp.Placeholder):
            key = node.expression.name
            if key in parameters and isinstance(parameters[key], dict):
                item = parameters[key]
                if set(item) != {"value", "match"} or item["match"] not in {"contains", "prefix", "exact"} or not isinstance(item["value"], str):
                    raise AnalysisError("文本参数需要 value 和 contains/prefix/exact 匹配方式。")
                value = item["value"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                value = ("%" if item["match"] == "contains" else "") + value + ("%" if item["match"] != "exact" else "")
                used.add(key)
                copied = node.copy()
                copied.set("expression", literal(value))
                return exp.Escape(this=copied, expression=exp.Literal.string("\\"))
        if isinstance(node, exp.Placeholder):
            key = node.name
            if key not in parameters:
                raise AnalysisError("SQL 缺少明确参数。")
            used.add(key)
            return literal(parameters[key])
        return node
    ast = ast.transform(replace)
    if used != set(parameters):
        raise AnalysisError("存在未使用的分析参数。")
    return ast.sql(dialect=dialect)


def _json_value(value):
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AnalysisError("分析结果含非有限金额。")
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise AnalysisError("分析结果含非有限数值。")
    return value


def _prepare_data(directory, descriptor, table_rows):
    import duckdb

    if not isinstance(table_rows, dict) or set(table_rows) != {t["name"] for t in descriptor["source_tables"]}:
        raise AnalysisError("必须提供完整、精确匹配的已绑定数据表。")
    if sum(len(rows) for rows in table_rows.values()) > MAX_ROWS:
        raise AnalysisError("离线数据超过 50000 行执行预算；应使用源端查询，不能抽样。")
    if len(json.dumps(table_rows, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise AnalysisError("离线数据超过 32 MiB 执行预算；应使用源端查询，不能截断。")
    counts = {}
    with duckdb.connect(directory + "/receipt.duckdb") as connection:
        for source in descriptor["source_tables"]:
            rows = table_rows[source["name"]]
            if len(rows) != source["expected_count"]:
                raise AnalysisError("输入行数与发布的完整来源数量不一致。")
            columns = source["columns"]
            if any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
                raise AnalysisError("输入字段与发布来源投影不一致。")
            # Stage scalar lexicals, then let approved MDL casts define meaning.
            values = []
            for row in rows:
                staged = []
                for column in columns:
                    value = row[column]
                    if value is not None and not isinstance(value, str | int | float | bool):
                        raise AnalysisError("离线源字段必须为标量，不能含对象或数组。")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise AnalysisError("离线源字段含非有限数值。")
                    semantic_types = source.get("semantic_types", {}).get(column, [])
                    if value not in {None, ""} and "bigint" in semantic_types:
                        lexical = str(value).strip()
                        if not re.fullmatch(r"[+-]?[0-9]+", lexical) or not -(2**63) <= int(lexical) < 2**63:
                            raise AnalysisError("整数来源不是有效64位整数；不能取整、溢出或把布尔值当数值。")
                    if value not in {None, ""} and "date" in semantic_types:
                        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value)):
                            raise AnalysisError("日期来源须明确采用 YYYY-MM-DD。")
                        try:
                            date.fromisoformat(str(value))
                        except ValueError as exc:
                            raise AnalysisError("日期来源包含无效日历日期。") from exc
                    if value not in {None, ""} and "timestamptz" in semantic_types:
                        try:
                            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        except ValueError as exc:
                            raise AnalysisError("时间戳格式无效。") from exc
                        if stamp.tzinfo is None:
                            raise AnalysisError("时间戳未声明时区，不能推测业务时间。")
                    if value not in {None, ""} and "decimal" in semantic_types:
                        number = Decimal(str(value))
                        if not number.is_finite() or number.as_tuple().exponent < -18 or number.adjusted() >= 20:
                            raise AnalysisError("离线金额超出精确 DECIMAL(38,18) 范围；改用源端 NUMERIC 查询。")
                    staged.append(None if value is None else str(value))
                values.append(staged)
            connection.execute(f'CREATE TABLE {ident(source["name"])} (' + ", ".join(ident(c) + " VARCHAR" for c in columns) + ")")
            if values:
                connection.executemany(f'INSERT INTO {ident(source["name"])} VALUES (' + ",".join("?" for _ in columns) + ")", values)
            counts[source["name"]] = len(rows)
    return counts


def _plan_sql(plan, manifest, limit, dialect, *, exporting=False):
    from wren_core import cube_query_to_sql

    if not isinstance(plan, dict) or set(plan) not in ({"sql"}, {"sql", "parameters"}, {"cube_query"}):
        raise AnalysisError("分析计划只能包含 sql 或 cube_query。")
    if "cube_query" in plan:
        query = dict(plan["cube_query"])
        if not isinstance(query.get("measures"), list) or not 1 <= len(query["measures"]) <= 20:
            raise AnalysisError("指标数量须为 1 到 20。")
        if len(query.get("dimensions", [])) + len(query.get("timeDimensions", [])) > 6:
            raise AnalysisError("分析最多支持六个分组维度。")
        if len(query.get("filters", [])) > 20 or query.get("offset", 0) not in {0, None}:
            raise AnalysisError("筛选条件过多或未使用平台结果分页。")
        # The source result cap is applied after grouping, never before it.
        if exporting:
            query.pop("limit", None)
        else:
            query["limit"] = limit + 1
        query.pop("offset", None)
        sql = cube_query_to_sql(json.dumps(query), json.dumps(manifest))
        # The native cube builder emits unquoted model names (e.g. Order).
        # Quote semantic identifiers before Wren planning, never patch physical SQL.
        from sqlglot import parse_one
        sql = parse_one(sql, dialect=dialect).sql(dialect=dialect, identify=True)
    else:
        sql = plan["sql"]
        if "parameters" in plan:
            sql = _bind_parameters(sql, plan["parameters"], dialect)
    allow_limit = "cube_query" in plan and not exporting
    validate_sql(sql, manifest, dialect=dialect, allow_top_limit=allow_limit)
    normalized = _normalize_scoped_aliases(sql, dialect)
    return validate_sql(normalized, manifest, dialect=dialect, allow_top_limit=allow_limit)



def _normalize_scoped_aliases(sql, dialect):
    """Work around Wren 0.15 alias collisions without changing SQL scope.

    SQLGlot scope.columns includes correctly resolved correlated references;
    collect references before mutating aliases so cached scopes remain sound.
    """
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.scope import traverse_scope

    ast = parse_one(sql, dialect=dialect)
    scopes = traverse_scope(ast)
    refs = [(scope, name, node) for scope in scopes for name, node in scope.references
            if isinstance(node, exp.Table) and node.args.get("alias")]
    counts = Counter(name for _, name, _ in refs)
    reserved = {identifier.name for identifier in ast.find_all(exp.Identifier)}
    replacements = []
    sequence = 0
    for scope, name, node in refs:
        if counts[name] < 2:
            continue
        while True:
            sequence += 1
            alias = f"orion_scope_{sequence}"
            if alias not in reserved:
                reserved.add(alias)
                break
        columns = [column for column in [*scope.columns, *scope.stars]
                   if isinstance(column, exp.Column) and column.table == name]
        replacements.append((node, alias, columns))
    for node, alias, columns in replacements:
        node.args["alias"].set("this", exp.to_identifier(alias))
        for column in columns:
            column.set("table", exp.to_identifier(alias))
    return ast.sql(dialect=dialect) if replacements else sql

def _catalog_guard(connection, descriptor):
    """Use the same transaction for constraint/schema verification and analysis."""
    module_path = Path(__file__).resolve().parents[2] / "services/structured_data/source_registration.py"
    if not module_path.is_file():
        raise AnalysisError("动态来源目录核验组件尚未就绪。")
    spec = importlib.util.spec_from_file_location("orion_wren_source_registration", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checked = []
    with connection.cursor() as cursor:
        for source in descriptor["source_tables"]:
            expected = source.get("catalog_contract") or source
            qualified = source.get("qualified_source_table") or source["physical_schema"] + "." + source["physical_table"]
            # Keep DDL from changing a verified relation until the query finishes.
            cursor.execute("LOCK TABLE " + ident(source["physical_schema"]) + "." + ident(source["physical_table"]) + " IN ACCESS SHARE MODE")
            actual = module.read_postgres_source_contract(cursor, source_id=source["source_id"],
                authorized_tables=[qualified], authorized_columns={qualified: expected["columns"]})
            if len(actual) != 1 or actual[0]["schema_signature"] != source["schema_signature"]:
                raise AnalysisError("来源结构或主外键约束已变化，请重新核验业务模型。")
            checked.append({"source_id": source["source_id"], "source_table": source["source_table"], "schema_signature": source["schema_signature"]})
    return {"status": "PASSED", "method": "CATALOG_CONSTRAINTS", "tables": checked}


def _freshness_guard(connection, payload, *, transaction=None):
    freshness = payload.get("execution", {}).get("data_freshness")
    if payload["descriptor"].get("execution_scope") != "LIVE_SOURCE_DATABASE":
        return None
    if not isinstance(freshness, dict):
        raise AnalysisError("动态数据源缺少本次新鲜度检查。")
    try:
        observed = datetime.fromisoformat(freshness["observed_at"])
        if observed.tzinfo is None:
            raise AnalysisError("missing timezone")
        age = (datetime.now(UTC) - observed).total_seconds() * 1000
        threshold = freshness["threshold_ms"]
        if (type(threshold) is not int or not 100 <= threshold <= 60000 or age < -1000
                or freshness.get("query_node") not in {"PRIMARY", "REPLICA"}
                or (freshness.get("query_node") == "REPLICA" and age > threshold)):
            raise AnalysisError("expired")
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("来源新鲜度检查已过期或状态未知；请重新查询。") from exc
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_is_in_recovery()")
        replica = cursor.fetchone()[0]
        if replica != (freshness.get("query_node") == "REPLICA"):
            raise AnalysisError("查询节点角色已变化，请重新核验来源。")
        if replica:
            barrier = freshness.get("primary_wal_barrier")
            if not isinstance(barrier, str) or not re.fullmatch(r"[0-9A-F]+/[0-9A-F]+", barrier, re.I):
                raise AnalysisError("副本缺少可验证的主库位点。")
            cursor.execute("SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (barrier,))
            if cursor.fetchone()[0] is not True:
                raise AnalysisError("副本尚未追上本次主库位点，不能返回过期数据。")
    verified = datetime.now(UTC)
    if not replica:
        # A new primary connection observes the authoritative node now. Parent
        # process/queue age is not replication lag. An established RR snapshot,
        # however, must never be relabelled with the current wall-clock time.
        snapshot_at = (datetime.fromisoformat(transaction["transaction_started_at"])
                       if transaction is not None else verified)
        snapshot_age = (verified - snapshot_at).total_seconds() * 1000
        if snapshot_at.tzinfo is None or snapshot_age < -1000 or snapshot_age > threshold:
            raise AnalysisError("本次只读事务快照已超过新鲜度预算；请重新建立查询快照。")
        return {**freshness, "upstream_observed_at": freshness["observed_at"],
                "observed_at": snapshot_at.isoformat(), "verified_at": verified.isoformat(),
                "lag_ms": 0, "observation_age_ms": round(snapshot_age, 3),
                "upstream_observation_age_ms": round(age, 3), "verification_method": "CURRENT_PRIMARY_READ_ONLY_SNAPSHOT"}
    age = (verified - observed).total_seconds() * 1000
    if age > threshold:
        raise AnalysisError("副本的新鲜度位点检查已过期；请重新核验主库位点。")
    return {**freshness, "verified_at": verified.isoformat(), "observation_age_ms": round(age, 3)}


def _transaction_receipt(connection, descriptor):
    timezone_name = descriptor.get("business_timezone", "UTC")
    if timezone_name not in {"UTC", "Asia/Shanghai"}:
        raise AnalysisError("当前业务时区尚未验证。")
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SELECT set_config('TimeZone', %s, true), set_config('standard_conforming_strings', 'on', true)", (timezone_name,))
        cursor.execute("SELECT current_database(), current_setting('transaction_isolation'), current_setting('transaction_read_only'), transaction_timestamp(), txid_current_snapshot()::text, pg_is_in_recovery(), pg_last_xact_replay_timestamp(), CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn()::text ELSE pg_current_wal_lsn()::text END, md5(COALESCE(inet_server_addr()::text,'local') || ':' || COALESCE(inet_server_port()::text,'local') || ':' || (SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database()))")
        database, isolation, readonly, at, snapshot, recovery, replay_at, lsn, database_identity = cursor.fetchone()
        if readonly != "on" or isolation != "repeatable read":
            raise AnalysisError("查询未进入只读一致性事务。")
        if any(source.get("database") and source["database"] != database for source in descriptor["source_tables"]):
            raise AnalysisError("连接数据库与来源登记身份不一致。")
        if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE" and any(
                source.get("database_identity") != database_identity for source in descriptor["source_tables"]):
            raise AnalysisError("执行节点身份与登记来源不一致；拒绝读取同名的其他数据库。")
        return {"database": database, "database_identity": database_identity, "isolation_level": "REPEATABLE READ", "read_only": True,
                "transaction_started_at": at.isoformat(), "transaction_snapshot": snapshot,
                "query_node": "REPLICA" if recovery else "PRIMARY", "wal_position": lsn,
                "last_replay_at": replay_at.isoformat() if replay_at else None, "business_timezone": timezone_name,
                "freshness_note": "事务快照表示查询节点可见范围；仅凭回放时间不能证明复制延迟或永久历史重演。"}


def _csv_value(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return "" if value is None else _json_value(value)


def _stream_export(connection, expanded_sql, config):
    max_rows, max_bytes = config.get("max_rows", 100000), config.get("max_bytes", 100 * 1024 * 1024)
    if type(max_rows) is not int or not 1 <= max_rows <= 1000000 or type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise AnalysisError("导出行数或字节预算无效。")
    path = Path(config["path"])
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise AnalysisError("导出必须使用服务端分配的全新文件路径。")
    partial = Path(str(path) + ".partial")
    rows = 0
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    descriptor = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            with connection.cursor(name="orion_wren_export") as cursor:
                cursor.itersize = 2000
                cursor.execute(expanded_sql)
                # Named cursors expose description after the first fetch.
                chunk = cursor.fetchmany(2000)
                writer.writerow([_csv_value(field.name) for field in cursor.description])
                while chunk:
                    for row in chunk:
                        rows += 1
                        if rows > max_rows:
                            raise AnalysisError("导出超过行数预算；未生成不完整的下载文件。")
                        writer.writerow([_csv_value(value) for value in row])
                        if stream.tell() > max_bytes:
                            raise AnalysisError("导出超过字节预算；未生成不完整的下载文件。")
                    chunk = cursor.fetchmany(2000)
            stream.flush()
            os.fsync(stream.fileno())
        size = partial.stat().st_size
        if size > max_bytes:
            raise AnalysisError("导出超过字节预算。")
        with partial.open("rb") as raw:
            checksum = "sha256:" + hashlib.file_digest(raw, "sha256").hexdigest()
        # Atomic publication without overwriting an existing file or symlink.
        os.link(partial, path)
        partial.unlink()
        return {"row_count": rows, "byte_count": size, "sha256": checksum,
                "format": "csv", "formula_escape": True, "complete": True}
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _execute_engine(payload, manifest, connection_info, data_source, counts):
    from wren import WrenEngine
    from wren.config import WrenConfig

    limit = payload.get("limit", 100)
    exporting = payload.get("operation") == "export"
    maximum = 1000000 if exporting else MAX_RESULT_ROWS
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise AnalysisError("查询或导出结果预算不受支持。")
    if exporting and data_source != "postgres":
        raise AnalysisError("全量导出需要源端 SQL 查询，不能拼接网页预览。")
    encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
    sql = _plan_sql(payload["plan"], manifest, limit, data_source, exporting=exporting)
    started = datetime.now(UTC).isoformat()
    start_clock = time.monotonic()
    transaction, validation = None, None
    descriptor = payload["descriptor"]
    with WrenEngine(encoded, data_source, connection_info, config=WrenConfig(strict_mode=True), fallback=False) as engine:
        connection = None
        if data_source == "postgres":
            # Official connector owns execution and Arrow conversion; switching
            # its fresh connection to a short transaction preserves one snapshot.
            connection = engine._get_connector().connection
            _freshness_guard(connection, payload)
            connection.autocommit = False
        with connection.transaction() if connection is not None else nullcontext():
            if connection is not None:
                transaction = _transaction_receipt(connection, descriptor)
            if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE":
                if connection is None:
                    raise AnalysisError("动态业务源不能由离线数据冒充。")
                validation = _catalog_guard(connection, descriptor)
            else:
                # Legacy snapshots without DB business-key constraints retain
                # their explicit conflict check. Live queries never scan keys.
                for model in manifest["models"]:
                    identity = model.get("primaryKey", "_key")
                    identity_sql = (f'SELECT {ident(identity)}, COUNT(*) AS n FROM {ident(model["name"])} '
                                    f'GROUP BY {ident(identity)} HAVING COUNT(*) > 1 LIMIT 1')
                    if engine.query(identity_sql, limit=1).num_rows:
                        raise AnalysisError("同一业务标识存在冲突记录；需治理身份粒度后再统计，避免关联重复计数。")
            expanded = engine.dry_plan(sql)
            engine.dry_run(sql)
            freshness = _freshness_guard(connection, payload, transaction=transaction) if connection is not None else None
            query_started = datetime.now(UTC).isoformat()
            if exporting:
                export_result = _stream_export(connection, expanded, payload["export"])
                rows, variables, row_types, truncated = [], [], {}, False
            else:
                table = engine.query(sql, limit=limit + 1)
                rows = [{k: _json_value(v) for k, v in row.items()} for row in table.slice(0, limit).to_pylist()]
                variables = table.column_names
                row_types = {field.name: str(field.type) for field in table.schema}
                truncated = table.num_rows > limit
    result = {"rows": rows, "variables": variables, "row_types": row_types,
            "number_encoding": "DECIMAL_AND_UNSAFE_INTEGER_AS_EXACT_STRINGS",
            "sql": sql, "requested_sql": payload["plan"].get("sql"),
            "sql_normalization": "SCOPED_AST_ALIASES_AND_TYPED_PARAMETERS",
            "expanded_sql": expanded, "truncated": truncated,
            "engine": "WrenAI", "engine_version": version("wrenai"), "core_version": version("wren-core-py"),
            "mdl_sha256": digest(manifest), "source_row_counts": counts,
            "execution_mode": "SOURCE_POSTGRES" if data_source == "postgres" else "ISOLATED_DUCKDB",
            "execution_scope": descriptor.get("execution_scope", "CONTROLLED_SNAPSHOT_POSTGRES"),
            "transaction": transaction, "source_validation": validation,
            "started_at": started, "completed_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": round((time.monotonic() - start_clock) * 1000, 3),
            "parameters_sha256": digest(payload["plan"].get("parameters", {})),
            "strict_mode": True, "fallback": False, "statistics_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"}
    result["query_started_at"] = query_started
    result["query_finished_at"] = result["completed_at"]
    if freshness is not None:
        result["data_freshness"] = {**freshness, "queried_at": query_started}
    if exporting:
        result["export"] = export_result
        result["row_count"] = export_result["row_count"]
        result["byte_count"] = export_result["byte_count"]
        result["sha256"] = export_result["sha256"]
    return result


def _validate_project(descriptor):
    from pathlib import Path

    from wren.context import build_json, validate_project

    path = Path(descriptor["project_path"])
    issues = validate_project(path)
    if any(item.level == "error" for item in issues):
        raise AnalysisError("持久 Wren 项目未通过官方结构校验。")
    compiled = build_json(path)
    if compiled != descriptor["mdl_postgres"]:
        raise AnalysisError("持久项目的官方编译结果与发布来源编译描述不一致。")
    return {"status": "PASSED", "mdl_sha256": digest(compiled), "warning_count": len(issues)}


def execute(payload):
    descriptor = payload["descriptor"]
    if payload.get("operation") == "validate_project":
        return _validate_project(descriptor)
    execution = payload.get("execution") or {"data_source": "duckdb"}
    data_source = execution.get("data_source")
    if data_source not in {"duckdb", "postgres"}:
        raise AnalysisError("当前仅允许已验证 PostgreSQL 源端或隔离 DuckDB 执行。")
    key = "mdl_postgres" if data_source == "postgres" else "mdl"
    manifest = descriptor[key]
    if digest(manifest) != descriptor[key + "_sha256"]:
        raise AnalysisError("持久分析模型指纹不一致。")
    if data_source == "postgres":
        connection = dict(execution["connection_info"])
        kwargs = dict(connection.get("kwargs") or {})
        timeout = 120000 if payload.get("operation") == "export" else 25000
        kwargs["options"] = (f"-c statement_timeout={timeout} -c default_transaction_read_only=on "
                             "-c lock_timeout=3000 -c idle_in_transaction_session_timeout=130000 -c TimeZone=UTC")
        kwargs["connect_timeout"] = "10"
        kwargs.pop("autocommit", None)  # Official connector requires string kwargs and defaults to True.
        connection["kwargs"] = kwargs
        counts = {} if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE" else {s["name"]: s["expected_count"] for s in descriptor["source_tables"]}
        try:
            return _execute_engine(payload, manifest, connection, data_source, counts)
        except AnalysisError:
            raise
        except Exception as exc:
            # Database exceptions can carry connection addresses and source SQL.
            raise RuntimeError("Wren 源端规划或执行失败；请核对发布类型、字段及数据库状态。") from exc
    with TemporaryDirectory(prefix="orion-wren-project-") as directory:
        counts = _prepare_data(directory, descriptor, payload["table_rows"])
        return _execute_engine(payload, manifest, {"url": directory, "format": "duckdb"}, data_source, counts)


def _encode_response(response):
    """Check the entire response before writing any bytes to the parent pipe."""
    chunks, size = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
    for chunk in encoder.iterencode(response):
        size += len(chunk.encode("utf-8"))
        if size > MAX_RESPONSE_BYTES:
            raise AnalysisError("分析结果超过 4 MiB 响应预算；请减少返回字段或使用受控导出，统计来源范围不变。")
        chunks.append(chunk)
    return "".join(chunks)


if __name__ == "__main__":
    try:
        sys.stdout.write(_encode_response(execute(json.load(sys.stdin))))
    except Exception as exc:
        # Never echo arbitrary SQL, temporary paths, connection strings or secrets.
        safe = str(exc) if type(exc) in {AnalysisError, RuntimeError} else "Wren 分析执行失败；请核对来源和模型。"
        print(json.dumps({"error": safe[:800]}, ensure_ascii=False))
