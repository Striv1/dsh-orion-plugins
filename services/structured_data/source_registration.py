"""Service-owned source registry and bounded PostgreSQL catalog contracts.

The agent and UI see identity/scope summaries; passwords remain in environment
references. Catalog helpers deliberately import only the standard library so an
isolated Wren worker can load this exact implementation by a fixed file path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_ROOT = Path(os.environ.get("ORION_WREN_SOURCE_REGISTRY_ROOT") or Path(__file__).resolve().parents[2] / ".orion-runtime/wren/sources")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,159}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
_ENV = re.compile(r"^ORION_[A-Z0-9_]+_SOURCE_URL$")
_VERSION = "orion-service-source-v2"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def schema_contract_signature(table: dict[str, Any]) -> str:
    contract = {key: table[key] for key in (
        "qualified_source_table", "columns", "types", "nullable", "primary_key", "foreign_keys")}
    contract["column_details"] = table.get("column_details", {})
    return _digest(contract)


def _qualified(value: str) -> tuple[str, str]:
    parts = str(value).split(".")
    if len(parts) != 2 or any(not _NAME.fullmatch(p) for p in parts):
        raise ValueError("来源必须使用明确的 schema.table 标识，不能使用通配或 SQL 片段。")
    return parts[0], parts[1]


def read_postgres_source_contract(cursor: Any, *, source_id: str,
        authorized_tables: list[str], authorized_columns: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Read only relation/column/constraint catalogs, never scan business rows."""
    if not authorized_tables or len(authorized_tables) > 500 or len(set(authorized_tables)) != len(authorized_tables):
        raise ValueError("来源表范围必须明确、唯一且不超过500张表。")
    column_scope = authorized_columns or {}
    if set(column_scope) - set(authorized_tables):
        raise ValueError("列范围引用了未授权的限定表。")
    tables = []
    for qualified in sorted(authorized_tables):
        schema, name = _qualified(qualified)
        cursor.execute("SELECT relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relname=%s", (schema, name))
        relation = cursor.fetchone()
        if not relation or relation[0] not in {"r", "p"}:
            raise ValueError("授权来源不存在或不是已支持的 PostgreSQL 物理表。")
        detail_keys = ("udt_schema", "udt_name", "domain_schema", "domain_name", "character_maximum_length",
            "numeric_precision", "numeric_precision_radix", "numeric_scale", "datetime_precision",
            "is_generated", "generation_expression")
        cursor.execute("SELECT column_name, data_type, is_nullable, " + ", ".join(detail_keys) + " FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, name))
        actual = cursor.fetchall()
        if not actual or len(actual) > 500 or len({r[0] for r in actual}) != len(actual):
            raise ValueError("授权来源字段不存在、重复或超过预算。")
        selected = column_scope.get(qualified, [r[0] for r in actual])
        if (not selected or len(selected) != len(set(selected))
                or not set(selected).issubset({r[0] for r in actual})):
            raise ValueError("授权列与真实 PostgreSQL 目录不一致。")
        actual = [r for r in actual if r[0] in selected]
        cursor.execute("""SELECT c.conname, c.contype,
            array_agg(a.attname ORDER BY u.ordinality), nr.nspname, tr.relname,
            array_agg(ar.attname ORDER BY u.ordinality), c.convalidated, c.condeferrable
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
            LEFT JOIN pg_catalog.pg_class tr ON tr.oid=c.confrelid
            LEFT JOIN pg_catalog.pg_namespace nr ON nr.oid=tr.relnamespace
            LEFT JOIN LATERAL unnest(c.conkey,c.confkey) WITH ORDINALITY AS u(attnum,refnum,ordinality) ON true
            LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=u.attnum
            LEFT JOIN pg_catalog.pg_attribute ar ON ar.attrelid=tr.oid AND ar.attnum=u.refnum
            WHERE n.nspname=%s AND t.relname=%s AND c.contype IN ('p','f')
            GROUP BY c.conname,c.contype,nr.nspname,tr.relname,c.convalidated,c.condeferrable
            ORDER BY c.contype,c.conname""", (schema, name))
        primary_key, foreign_keys = [], []
        for constraint, kind, columns, ref_schema, ref_table, ref_columns, validated, deferred in cursor.fetchall():
            if not validated or deferred:
                raise ValueError("来源存在未验证或可延迟约束，当前分析合同不支持免扫描信任。")
            if not set(columns).issubset(selected):
                continue  # A hidden key cannot become an available semantic join.
            if kind == "p":
                primary_key = list(columns)
            else:
                foreign_keys.append({"name": constraint, "columns": list(columns),
                    "referenced_schema": ref_schema, "referenced_table": ref_table,
                    "referenced_columns": list(ref_columns), "validated": True, "deferrable": False})
        table = {"source_id": source_id, "source_table": name, "qualified_source_table": qualified,
            "physical_schema": schema, "physical_table": name, "columns": [r[0] for r in actual],
            "types": {r[0]: r[1] for r in actual}, "nullable": {r[0]: r[2] == "YES" for r in actual},
            "column_details": {r[0]: dict(zip(detail_keys, r[3:], strict=True)) for r in actual},
            "primary_key": primary_key, "foreign_keys": foreign_keys}
        table["schema_signature"] = schema_contract_signature(table)
        tables.append(table)
    return tables


def connection_identity_sha256(connection_env: str) -> str:
    """Fingerprint service endpoint/principal without persisting its password."""
    import psycopg.conninfo

    if not _ENV.fullmatch(str(connection_env)) or not os.getenv(connection_env, "").strip():
        raise ValueError("来源只读服务连接未配置。")
    try:
        params = psycopg.conninfo.conninfo_to_dict(os.environ[connection_env].replace("postgresql+psycopg://", "postgresql://", 1))
    except Exception:
        raise ValueError("来源连接引用格式无效。") from None
    if not all(params.get(k) for k in ("host", "dbname", "user")):
        raise ValueError("服务连接必须明确主机、数据库及账号。")
    return _digest({"host": params["host"].lower(), "port": params.get("port", "5432"),
                    "dbname": params["dbname"], "user": params["user"]})


def _registry_path(project_id: str, source_id: str, registry_root: Path | None) -> Path:
    if not _ID.fullmatch(str(project_id)) or not _ID.fullmatch(str(source_id)) or ".." in (project_id, source_id):
        raise ValueError("来源登记身份无效。")
    root = Path(registry_root or DEFAULT_REGISTRY_ROOT)
    path = root / project_id / f"{source_id}.json"
    if root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        raise ValueError("来源登记不能使用符号链接。")
    return path


def load_service_source(project_id: str, source_id: str, *, registry_root: Path | None = None) -> dict[str, Any]:
    path = _registry_path(project_id, source_id, registry_root)
    # FileNotFoundError intentionally means no service registration. Corruption
    # must be ValueError so callers never silently fall back to stale snapshots.
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("来源登记超过大小预算。")
    try:
        record = json.loads(raw)
        expected = record.pop("record_sha256")
        if (expected != _digest(record) or record.get("contract_version") != _VERSION
                or record.get("project_id") != project_id or record.get("source_id") != source_id
                or record.get("state") != "ACTIVE" or not _ENV.fullmatch(record.get("connection_env", ""))
                or not record.get("source_tables")):
            raise ValueError("Invalid identity or checksum")
        for table in record["source_tables"]:
            if table["schema_signature"] != schema_contract_signature(table):
                raise ValueError("Invalid schema checksum")
        if record["schema_signature"] != _digest([t["schema_signature"] for t in record["source_tables"]]):
            raise ValueError("Invalid combined schema checksum")
        record["record_sha256"] = expected
        return record
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("来源登记身份、状态或完整性校验失败，不能静默回退其他来源。") from None


def safe_source_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in (
        "service_source_id", "project_id", "source_id", "provider", "state", "database", "schemas",
        "authorized_tables", "query_node", "freshness_threshold_ms", "dataset_type", "schema_signature",
        "chat2db_datasource_id", "chat2db_binding_status", "tested_at")}


def list_service_sources(project_id: str | None = None, *, registry_root: Path | None = None) -> list[dict[str, Any]]:
    root = Path(registry_root or DEFAULT_REGISTRY_ROOT)
    if project_id is not None and not _ID.fullmatch(project_id):
        raise ValueError("工程身份无效。")
    files = sorted((root / project_id).glob("*.json") if project_id else root.glob("*/*.json"))
    return [safe_source_summary(load_service_source(path.parent.name, path.stem, registry_root=root)) for path in files]


def register_service_source(*, project_id: str, source_id: str, connection_env: str,
        database: str, authorized_tables: list[str], owner: str, pii_scope: str,
        authorized_columns: dict[str, list[str]] | None = None, chat2db_datasource_id: int | None = None,
        query_node: str = "PRIMARY", freshness_threshold_ms: int = 10000,
        dataset_type: str = "PRODUCTION", primary_connection_env: str | None = None,
        registry_root: Path | None = None) -> dict[str, Any]:
    """Administrator registration. Publication authorization is checked by caller.

    No service credentials are accepted in a JSON request or returned. A missing
    Chat2DB receipt remains UNVERIFIED and does not invalidate an explicit service
    source. A proven identity mismatch is rejected, never hidden as unavailable.
    """
    import psycopg

    path = _registry_path(project_id, source_id, registry_root)
    if query_node not in {"PRIMARY", "REPLICA"} or dataset_type not in {"PRODUCTION", "TEST_ONLY"}:
        raise ValueError("查询节点或数据性质无效。")
    if type(freshness_threshold_ms) is not int or not 100 <= freshness_threshold_ms <= 60000:
        raise ValueError("新鲜度阈值必须在100至60000毫秒之间。")
    if not owner.strip() or not pii_scope.strip():
        raise ValueError("必须登记来源责任人与个人信息范围。")
    if chat2db_datasource_id is not None and (type(chat2db_datasource_id) is not int or chat2db_datasource_id < 1):
        raise ValueError("Chat2DB 编号无效。")
    identity = connection_identity_sha256(connection_env)
    primary_identity = connection_identity_sha256(primary_connection_env) if primary_connection_env else None
    tables, cluster_identifier = [], None
    try:
        with psycopg.connect(os.environ[connection_env].replace("postgresql+psycopg://", "postgresql://", 1),
                connect_timeout=10) as conn, conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout=10000")
            cursor.execute("SELECT current_database(), pg_is_in_recovery(), current_setting('transaction_read_only'), "
                "md5(COALESCE(inet_server_addr()::text,'local') || ':' || COALESCE(inet_server_port()::text,'local') "
                "|| ':' || (SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database()))")
            observed = cursor.fetchone()
            if not observed or observed[0] != database or observed[2] != "on" or observed[1] != (query_node == "REPLICA"):
                raise ValueError("服务连接实际数据库、节点类型或只读事务与登记不一致。")
            cursor.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user")
            if any(cursor.fetchone()):
                raise ValueError("服务连接使用高权限账号，不能登记为分析只读连接。")
            tables = read_postgres_source_contract(cursor, source_id=source_id,
                authorized_tables=authorized_tables, authorized_columns=authorized_columns)
            for table in tables:
                cursor.execute("SELECT has_table_privilege(current_user,%s,'SELECT'), "
                    "has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')", (table["qualified_source_table"],) * 2)
                rights = cursor.fetchone()
                if rights != (True, False):
                    raise ValueError("服务连接必须对授权表仅有读取权限。")
            cursor.execute("SAVEPOINT source_identity")
            try:
                cursor.execute("SELECT system_identifier::text FROM pg_catalog.pg_control_system()")
                cluster_identifier = cursor.fetchone()[0]
            except psycopg.Error:
                cursor.execute("ROLLBACK TO SAVEPOINT source_identity")
            cursor.execute("RELEASE SAVEPOINT source_identity")
    except psycopg.Error:
        raise ValueError("来源只读服务连接或目录核验失败；连接信息已隐藏。") from None
    provider, chat2db_status, chat2db_reason = "EXPLICIT_SERVICE", "NOT_REQUESTED", None
    if chat2db_datasource_id is not None:
        from .chat2db_source import Chat2DBCLI, Chat2DBSourceError

        try:
            receipt = Chat2DBCLI().database_identity(chat2db_datasource_id, database)
        except Chat2DBSourceError:
            chat2db_status, chat2db_reason = "UNVERIFIED", "CHAT2DB_IDENTITY_UNAVAILABLE"
        else:
            if receipt["database_identity"] != observed[3]:
                raise ValueError("Chat2DB 所选数据库与服务连接不是同一来源，未登记关联。")
            provider, chat2db_status = "CHAT2DB_VERIFIED", "VERIFIED"
    record = {"contract_version": _VERSION, "service_source_id": f"{project_id}:{source_id}",
        "project_id": project_id, "source_id": source_id, "provider": provider, "state": "ACTIVE",
        "database": database, "schemas": sorted({t["physical_schema"] for t in tables}),
        "authorized_tables": sorted(authorized_tables),
        "authorized_columns": {t["qualified_source_table"]: t["columns"] for t in tables},
        "connection_env": connection_env, "connection_identity_sha256": identity,
        "primary_connection_env": primary_connection_env, "primary_connection_identity_sha256": primary_identity,
        "cluster_identifier": cluster_identifier, "query_node": query_node,
        "freshness_threshold_ms": freshness_threshold_ms, "dataset_type": dataset_type,
        "owner": owner.strip(), "pii_scope": pii_scope.strip(), "source_tables": tables,
        "schema_signature": _digest([t["schema_signature"] for t in tables]),
        "database_identity": observed[3], "chat2db_datasource_id": chat2db_datasource_id,
        "chat2db_binding_status": chat2db_status, "chat2db_binding_reason": chat2db_reason,
        "tested_at": datetime.now(UTC).isoformat()}
    record["record_sha256"] = _digest(record)
    encoded = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("来源登记超过大小预算；请拆分明确的业务来源范围。")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record
