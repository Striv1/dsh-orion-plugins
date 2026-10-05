"""Bind dynamic database reads to an immutable, reviewed business model.

Source registrations are administrator-owned references, not client connection
parameters. A broken registration never silently falls back to an old snapshot.
"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import psycopg
from psycopg.conninfo import conninfo_to_dict

from services.structured_data.pipeline import StructuredDataPipeline


def resolve_analysis_project(binding, descriptor, data_mode="AUTO"):
    if data_mode == "SNAPSHOT":
        return descriptor
    from services.structured_data.source_registration import load_service_source

    registrations = {}
    source_ids = {table.get("source_id") for table in descriptor["source_tables"]}
    for source_id in sorted(source_ids, key=str):
        if not source_id:
            continue
        try:
            record = load_service_source(binding.project_id, source_id)
        except FileNotFoundError:
            continue
        if record.get("state") != "ACTIVE":
            raise ValueError("数据源服务登记已停用；未回退到旧快照。")
        published = binding.source_bindings.get(source_id, {})
        if (published.get("status") != "ACTIVE" or published.get("access_mode") != "READ_ONLY"
                or published.get("database") != record.get("database")
                or published.get("project_id") != binding.project_id
                or published.get("source_id") != source_id):
            raise ValueError("服务数据源与当前发布的来源身份或只读授权不一致。")
        permitted = published.get("authorized_columns", {})
        for table in descriptor["source_tables"]:
            if table.get("source_id") != source_id:
                continue
            name = table["source_table"]
            candidates = [t for t in record["source_tables"] if name in {t["source_table"], t["qualified_source_table"]}]
            if len(candidates) != 1:
                raise ValueError("动态来源表不存在或跨 Schema 存在歧义。")
            physical = candidates[0]
            allowed_name = next((n for n in (physical["qualified_source_table"], name)
                                 if n in published.get("authorized_tables", [])), None)
            if allowed_name and "." not in allowed_name and len(published.get("schemas", [])) != 1:
                raise ValueError("发布的裸表名涉及多个 Schema，需明确限定来源后才能实时查询。")
            if (not allowed_name or physical["physical_schema"] not in published.get("schemas", [])
                    or not set(table["columns"]) <= set(permitted.get(allowed_name, []))):
                raise ValueError("动态分析模型包含未被发布来源授权的表或字段。")
        registrations[source_id] = record
    if not registrations:
        if data_mode == "LIVE":
            raise ValueError("该发布版本尚未登记实时只读查询服务；不能用快照回答最新数据。")
        return descriptor
    if set(registrations) != source_ids:
        raise ValueError("模型涉及的来源尚未全部登记；不能混合实时与旧快照进行关联。")
    endpoints = {(r["connection_env"], r["database"]) for r in registrations.values()}
    if len(endpoints) != 1:
        raise ValueError("本期跨表分析要求同一数据库查询节点；跨数据库联合需独立授权与执行方案。")
    from services.realtime_qa.wren_project import build_live_project

    live = build_live_project(binding, descriptor, registrations)
    # Private execution-only data; catalog and evidence use explicit projections.
    live["_registrations"] = registrations
    return live


def connection_info(connection_env):
    value = os.getenv(connection_env, "").strip()
    if not value:
        raise ValueError("已登记数据源的只读服务连接未配置；请由管理员检查服务环境。")
    try:
        url = StructuredDataPipeline(value).database_url
        params = conninfo_to_dict(url)
    except (ValueError, psycopg.Error):
        raise ValueError("已登记数据源的只读连接格式无效。") from None
    if not {"host", "dbname", "user"} <= params.keys():
        raise ValueError("服务连接需明确数据库、主机及只读账号。")
    kwargs = {key: params[key] for key in ("sslmode", "sslrootcert", "sslcert", "sslkey") if key in params}
    kwargs.update(connect_timeout="5", options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000")
    return {"host": params["host"], "port": params.get("port", "5432"), "database": params["dbname"],
            "user": params["user"], "password": params.get("password", ""), "kwargs": kwargs}


def _connect(info):
    return psycopg.connect(host=info["host"], port=info["port"], dbname=info["database"],
                           user=info["user"], password=info["password"], **info["kwargs"])


def live_execution(descriptor):
    registrations = descriptor["_registrations"]
    first = next(iter(registrations.values()))
    from services.structured_data.source_registration import connection_identity_sha256

    for record in registrations.values():
        if connection_identity_sha256(record["connection_env"]) != record["connection_identity_sha256"]:
            raise ValueError("已登记的服务连接节点或账号发生变化，需重新验证来源。")
    info = connection_info(first["connection_env"])
    if info["database"] != first["database"]:
        raise ValueError("只读服务连接指向的数据库与来源登记不一致。")
    threshold = min(r.get("freshness_threshold_ms", 10000) for r in registrations.values())
    started = time.monotonic()
    try:
        with _connect(info) as conn, conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT current_database(), pg_is_in_recovery(), current_setting('transaction_read_only'), "
                           "md5(COALESCE(inet_server_addr()::text,'local') || ':' || COALESCE(inet_server_port()::text,'local') "
                           "|| ':' || (SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database()))")
            database, replica, readonly, node_identity = cursor.fetchone()
            if database != first["database"] or readonly != "on" or node_identity != first["database_identity"]:
                raise ValueError("查询节点身份或只读事务校验失败。")
            if replica != (first.get("query_node", "PRIMARY") == "REPLICA"):
                raise ValueError("查询节点角色已经变化；请重新核验数据源服务登记。")
            now = datetime.now(UTC).isoformat()
            freshness = {"status": "LIVE_QUERY", "upstream_requeried": True, "observed_at": now,
                         "queried_at": now, "query_node": "REPLICA" if replica else "PRIMARY",
                         "lag_ms": 0 if not replica else None, "threshold_ms": threshold,
                         "note_zh": "直接读取源数据库已提交数据；结果以本次只读事务可见范围为准。"}
            if replica:
                primary_ref = first.get("primary_connection_env")
                if not primary_ref:
                    raise ValueError("副本同步新鲜度未知：未登记主库进度探测连接，未执行要求最新的查询。")
                if (not first.get("cluster_identifier")
                        or connection_identity_sha256(primary_ref) != first.get("primary_connection_identity_sha256")):
                    raise ValueError("副本与主库的同源身份尚无法核验，不能确认数据新鲜度。")
                primary_info = connection_info(primary_ref)
                with _connect(primary_info) as primary, primary.cursor() as probe:
                    probe.execute("SELECT current_database(),pg_is_in_recovery(),pg_current_wal_lsn()::text,system_identifier::text FROM pg_catalog.pg_control_system()")
                    db, recovery, barrier, cluster = probe.fetchone()
                if db != database or recovery or cluster != first["cluster_identifier"]:
                    raise ValueError("主库进度探测节点与登记的业务数据库不匹配。")
                cursor.execute("SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (barrier,))
                if cursor.fetchone()[0] is not True:
                    raise ValueError("副本尚未回放到本次主库探测位点；请稍后重试，未用旧数据代替最新数据。")
                freshness.update(primary_wal_barrier=barrier, lag_ms=None,
                    visibility_bound_ms=round((time.monotonic() - started) * 1000, 3),
                    note_zh="查询副本已追上本次探测的主库位点；不把最后回放时间误作空闲源库的延迟。")
            if (time.monotonic() - started) * 1000 > threshold:
                raise ValueError("来源新鲜度探测超过时间预算；请重试。")
    except psycopg.Error as exc:
        raise ValueError(f"实时只读数据源不可用（SQLSTATE={exc.sqlstate or 'unknown'}）；未回退到旧快照。") from None
    return {"data_source": "postgres", "connection_info": info, "data_freshness": freshness,
            "source_registration_sha256": [r.get("record_sha256") for r in registrations.values()]}
