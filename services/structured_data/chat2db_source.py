"""Read governed PostgreSQL sources through Chat2DB's structured CLI API.

Passwords remain with Chat2DB.  This module accepts an opaque datasource ID and
builds a small set of SELECT statements itself; there is no arbitrary SQL API.
The CLI JSON surface is deliberately used instead of the lossy MCP text table.

CLI 0.2.1 can start its runtime during a query.  We preflight an already healthy
runtime and never issue lifecycle commands, but a runtime exit between that
check and the query remains an upstream lifecycle race.  Service ownership and
runtime recovery belong to the platform supervisor, not this source reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .multi_source import (
    MultiSourceContractError,
    SourceBinding,
    SourceColumnProfile,
    SourceProfileReceipt,
    SourceTableProfile,
)

_REF = re.compile(r"^chat2db://community/([1-9][0-9]{0,18})$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
_MD5 = re.compile(r"^[a-f0-9]{32}$")
_PAGE_FIELDS = ("total", "fingerprint", "ordinal", "row_hex", "row_digest", "readonly", "database")


class Chat2DBSourceError(MultiSourceContractError):
    """A source is unavailable or its complete, typed receipt cannot be proven."""


def _fail(code: str, message: str) -> None:
    raise Chat2DBSourceError(f"{code}: {message}")


def parse_chat2db_ref(reference: str) -> int:
    match = _REF.fullmatch(str(reference))
    if not match:
        _fail("SOURCE_NOT_BOUND", "Chat2DB 引用必须是 chat2db://community/<正整数编号>")
    return int(match[1])


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail("G-S1-SOURCE-SCOPE", "Chat2DB 来源包含不支持的标识符")
    return '"' + value + '"'


def _literal(value: str) -> str:
    # Metadata identifiers are encoded so SQL literals never include raw input.
    return "convert_from(decode('" + value.encode("utf-8").hex() + "','hex'),'UTF8')"


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _default_binary() -> str:
    configured = os.getenv("ORION_CHAT2DB_CLI", "").strip()
    return configured or shutil.which("chat2db") or str(
        Path.home() / "Desktop/Chat2DB 工具与报告/tools/chat2db"
    )


class Chat2DBCLI:
    """Narrow subprocess boundary; only safe errors and metadata escape it."""

    def __init__(self, binary: str | None = None, *, timeout: float = 30,
                 max_response_bytes: int = 16 * 1024 * 1024,
                 runner: Callable[..., Any] = subprocess.run) -> None:
        self.binary = binary or _default_binary()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.runner = runner

    def _call(self, arguments: list[str]) -> dict[str, Any]:
        try:
            result = self.runner(
                [self.binary, *arguments, "--edition", "community", "--json"],
                capture_output=True, timeout=self.timeout, check=False,
            )
            # CLI errors are JSON on stderr; success data is JSON on stdout.
            # Parse either envelope but never forward untrusted diagnostic text.
            raw = result.stdout if result.returncode == 0 else (getattr(result, "stderr", None) or result.stdout)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not raw or len(raw) > self.max_response_bytes:
                _fail("CHAT2DB_UNAVAILABLE", "Chat2DB 未返回预算内的成功回执，请检查受管运行时")
            payload = json.loads(raw)
            if (isinstance(payload, dict) and isinstance(payload.get("error"), dict)
                    and payload["error"].get("code") == "runtime_state_invalid_json"):
                _fail("CHAT2DB_RUNTIME_STATE_INCOMPATIBLE", "Chat2DB 运行时登记格式与 CLI 不兼容，需受管恢复；未启动或修改服务")
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                code = payload["error"].get("code")
                if code in {"runtime_process_still_running", "runtime_endpoint_not_ready", "runtime_process_identity_unavailable"}:
                    _fail("CHAT2DB_RUNTIME_NOT_READY", "Chat2DB 独立运行时未通过生命周期核验；桌面目录可单独核验，分析可使用已登记服务连接")
            if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
                _fail("CHAT2DB_UNAVAILABLE", "Chat2DB 操作未成功，未使用部分结果")
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("edition") != "community":
                _fail("CHAT2DB_CONTRACT", "Chat2DB CLI 版本或 edition 合同不匹配")
            return data
        except Chat2DBSourceError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            # Never include provider stderr, connection strings or CLI payloads.
            _fail("CHAT2DB_UNAVAILABLE", "Chat2DB 结构化接口不可用或响应无效")

    def ensure_ready(self) -> None:
        data = self._call(["runtime", "status"])
        health = data.get("health")
        if (data.get("command") != "runtime_status" or data.get("status") != "running"
                or data.get("healthy") is not True or not isinstance(health, dict)
                or health.get("ready") is not True or health.get("edition") != "community"
                or health.get("apiVersion") != "v1"):
            _fail("CHAT2DB_RUNTIME_NOT_READY", "Chat2DB 受管运行时尚未就绪；不会在来源读取时启动服务")

    def describe_source(self, datasource_id: int) -> dict[str, Any]:
        self.ensure_ready()
        data = self._call(["db", "datasource", "--data-source-id", str(datasource_id)])
        row = data.get("data")
        if (data.get("command") != "db_datasource" or not isinstance(row, dict)
                or row.get("id") != datasource_id):
            _fail("CHAT2DB_SOURCE_IDENTITY", "Chat2DB 来源编号与请求不符")
        # No host, user, URL, password, or token is returned to callers.
        return {"datasource_id": datasource_id, "label": str(row.get("alias") or ""),
                "engine": str(row.get("dbType") or "").upper(),
                "connection_ref": f"chat2db://community/{datasource_id}"}

    def _mcp_text(self, name: str, arguments: dict[str, Any]) -> str:
        """Use the existing desktop MCP for small identity/catalog receipts only."""
        data = self._call(["mcp", "call", name, "--args-json", json.dumps(arguments)])
        result = data.get("result")
        if (data.get("command") != "mcp_call" or data.get("tool_name") != name
                or not isinstance(result, dict) or result.get("isError") is True):
            _fail("CHAT2DB_CATALOG_UNAVAILABLE", "Chat2DB 桌面目录或身份核验不可用")
        blocks = result.get("content")
        if not isinstance(blocks, list) or len(blocks) != 1 or blocks[0].get("type") != "text":
            _fail("CHAT2DB_CONTRACT", "Chat2DB 身份回执格式无效")
        text = blocks[0].get("text")
        for _ in range(4):
            if not isinstance(text, str):
                break
            try:
                decoded = json.loads(text)
            except ValueError:
                break
            if not isinstance(decoded, str):
                break
            text = decoded
        if not isinstance(text, str) or len(text) > 256 * 1024 or "MCP tool '" in text:
            _fail("CHAT2DB_CATALOG_UNAVAILABLE", "Chat2DB 未返回可验证的目录或身份")
        return text

    def list_sources(self) -> list[dict[str, Any]]:
        text = self._mcp_text("list_all_datasources", {})
        sources = []
        for line in text.splitlines():
            fields = dict(re.findall(r"(?:^|;\s*)(id|name|type)\s*[:=]\s*([^;]*)", line))
            if not fields:
                continue
            if not str(fields.get("id", "")).isdigit() or not fields.get("type"):
                _fail("CHAT2DB_CONTRACT", "Chat2DB 来源目录缺少编号或引擎")
            sources.append({"datasource_id": int(fields["id"]), "label": fields.get("name", "").strip(),
                            "engine": fields["type"].strip().upper()})
        if len({s["datasource_id"] for s in sources}) != len(sources):
            _fail("CHAT2DB_CONTRACT", "Chat2DB 来源目录含重复编号")
        return sources

    def database_identity(self, datasource_id: int, database: str) -> dict[str, str]:
        """A one-row, short-cell probe; never use MCP for bulk business data."""
        source = [s for s in self.list_sources() if s["datasource_id"] == datasource_id]
        if len(source) != 1 or source[0]["engine"] != "POSTGRESQL":
            _fail("CHAT2DB_SOURCE_IDENTITY", "Chat2DB 来源不在当前 PostgreSQL 目录中")
        sql = ("SELECT 'ORION_SOURCE_ID_V1' AS protocol, "
               "encode(convert_to(current_database(),'UTF8'),'hex') AS database_hex, "
               "md5(COALESCE(inet_server_addr()::text,'local') || ':' || "
               "COALESCE(inet_server_port()::text,'local') || ':' || "
               "(SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database())) AS database_identity")
        text = self._mcp_text("execute_sql", {"dataSourceId": datasource_id, "databaseName": database,
                                              "sql": sql, "pageSize": 2})
        rows = [line.split("\t") for line in text.splitlines() if re.match(r"^\d+\t", line)]
        if (not re.search(r"^success: true\s*$", text, re.M)
                or not re.search(r"^sqlType: SELECT\s*$", text, re.M)
                or not re.search(r"^rows: 1, hasNextPage: false\s*$", text, re.M)
                or len(rows) != 1 or len(rows[0]) != 4
                or rows[0][:2] != ["1", "ORION_SOURCE_ID_V1"]
                or rows[0][2] != database.encode("utf-8").hex()
                or not _MD5.fullmatch(rows[0][3])):
            _fail("CHAT2DB_SOURCE_IDENTITY", "Chat2DB 数据库身份回执不完整或与所选数据库不符")
        return {"database": database, "database_identity": rows[0][3], "provider": "CHAT2DB_DESKTOP_MCP"}

    def _select(self, datasource_id: int, database: str, schema: str,
                sql: str, *, page_size: int) -> dict[str, Any]:
        # Internal callers only: every statement is generated by this module.
        self.ensure_ready()
        data = self._call([
            "sql", "query", "--data-source-id", str(datasource_id),
            "--database", database, "--schema", schema, "--sql", sql,
            "--page-no", "1", "--page-size", str(page_size), "--no-row-number",
        ])
        result = data.get("data")
        if data.get("command") != "sql_query" or not isinstance(result, dict):
            _fail("CHAT2DB_CONTRACT", "Chat2DB 查询未返回结构化结果")
        return result


def _decode_query(result: dict[str, Any], expected: tuple[str, ...], limit: int) -> list[list[Any]]:
    columns, rows = result.get("columns"), result.get("rows")
    if (not isinstance(columns, list) or not all(isinstance(c, dict) for c in columns)
            or tuple(c.get("name") for c in columns) != expected
            or not all(isinstance(c.get("type"), str) and c["type"] for c in columns)
            or not isinstance(rows, list) or len(rows) > limit
            or type(result.get("rowCount")) is not int or result["rowCount"] != len(rows)
            or result.get("pageNo") != 1 or result.get("pageSize") != limit
            or result.get("hasNextPage") is not False or result.get("truncated") is not False
            or result.get("updateCount") not in (None, 0)):
        _fail("CHAT2DB_INCOMPLETE", "Chat2DB 查询格式、分页或截断状态不完整")
    for row in rows:
        if (not isinstance(row, list) or len(row) != len(expected)
                or any(value is not None and not isinstance(value, str) for value in row)):
            _fail("CHAT2DB_CONTRACT", "Chat2DB 列数或原始值类型不匹配")
    return rows


class Chat2DBSourceReader:
    """SourceReader implementation for explicit PostgreSQL table projections.

    Each page is one PostgreSQL MVCC statement.  A complete projected-content
    digest is recomputed for every page, so mutation between pages fails closed.
    This is not a cross-table transaction: SnapshotHub retains its existing
    independently captured table semantics.  The CLI must return untruncated
    JSON; application paging is encoded in SQL rather than CLI result paging.
    """

    engine = "POSTGRESQL"

    def __init__(self, client: Chat2DBCLI | None = None, *, page_size: int = 999,
                 max_rows: int = 1_000_000, max_bytes: int = 256 * 1024 * 1024) -> None:
        if not 1 <= page_size <= 999 or max_rows < 1 or max_bytes < 1:
            raise ValueError("Invalid Chat2DB source capture budget")
        self.client = client or Chat2DBCLI()
        self.page_size, self.max_rows, self.max_bytes = page_size, max_rows, max_bytes

    def _scope(self, binding: SourceBinding, table: str) -> tuple[int, str, str]:
        if (binding.engine != self.engine or binding.status != "ACTIVE"
                or binding.access_mode != "READ_ONLY" or binding.readonly_attested is not True
                or table not in binding.authorized_tables):
            _fail("G-S1-SOURCE-SCOPE", "Chat2DB 只接受已登记的活动 PostgreSQL 只读表范围")
        datasource_id = parse_chat2db_ref(binding.connection_ref)
        _identifier(binding.database)
        if "." in table:
            schema, name = table.split(".", 1)
        elif len(binding.schemas) == 1:
            schema, name = binding.schemas[0], table
        else:
            _fail("G-S1-SOURCE-SCOPE", "多 Schema 来源必须使用限定表名")
        if schema not in binding.schemas:
            _fail("G-S1-SOURCE-SCOPE", "Chat2DB 表超出已登记 Schema")
        _identifier(schema)
        _identifier(name)
        return datasource_id, schema, name

    def _query(self, binding: SourceBinding, schema: str, sql: str,
               fields: tuple[str, ...], limit: int) -> list[list[Any]]:
        result = self.client._select(parse_chat2db_ref(binding.connection_ref), binding.database,
                                     schema, sql, page_size=limit)
        return _decode_query(result, fields, limit)

    def _guard(self, binding: SourceBinding, schema: str) -> None:
        rows = self._query(binding, schema,
            "SELECT current_database()::text AS database, "
            "current_setting('transaction_read_only')::text AS readonly", ("database", "readonly"), 2)
        if rows != [[binding.database, "on"]]:
            _fail("G-S1-SOURCE-READONLY", "Chat2DB 实际数据库或只读事务不符；请使用已配置的只读连接")

    def profile(self, binding: SourceBinding) -> SourceProfileReceipt:
        datasource = self.client.describe_source(parse_chat2db_ref(binding.connection_ref))
        if datasource["engine"] != self.engine:
            _fail("CHAT2DB_SOURCE_IDENTITY", "Chat2DB 实际数据库引擎与登记不符")
        tables = []
        for table in binding.authorized_tables:
            _, schema, name = self._scope(binding, table)
            self._guard(binding, schema)
            # Views/foreign tables can invoke unreviewed functions or another
            # source. Only physical/partitioned tables enter this adapter.
            sql = (
                "SELECT c.column_name::text AS name, c.data_type::text AS type, "
                "c.is_nullable::text AS nullable, c.ordinal_position::text AS ordinal "
                "FROM information_schema.columns c JOIN pg_catalog.pg_namespace n "
                "ON n.nspname=c.table_schema JOIN pg_catalog.pg_class t "
                "ON t.relnamespace=n.oid AND t.relname=c.table_name "
                f"WHERE c.table_schema={_literal(schema)} AND c.table_name={_literal(name)} "
                "AND t.relkind IN ('r','p') ORDER BY c.ordinal_position LIMIT 501"
            )
            raw = self._query(binding, schema, sql, ("name", "type", "nullable", "ordinal"), 502)
            if not raw or len(raw) > 500:
                _fail("G-S1-SOURCE-SCOPE", "Chat2DB 表不存在、不是普通表或超过字段预算")
            try:
                columns = [SourceColumnProfile(name=r[0], data_type=r[1], nullable=r[2] == "YES",
                                              ordinal_position=int(r[3])) for r in raw]
                if any(r[2] not in {"YES", "NO"} for r in raw):
                    raise ValueError("Invalid nullability")
            except (ValueError, TypeError):
                _fail("CHAT2DB_CONTRACT", "Chat2DB 字段类型元数据不完整")
            if len({c.name for c in columns}) != len(columns):
                _fail("CHAT2DB_CONTRACT", "Chat2DB 字段目录重复")
            allowed = binding.authorized_columns.get(table)
            if allowed is not None:
                if not allowed or not set(allowed).issubset({c.name for c in columns}):
                    _fail("G-S1-SOURCE-SCOPE", "Chat2DB 字段范围不在实际表结构内")
                columns = [c for c in columns if c.name in allowed]
            query = (
                f"SELECT count(*)::text AS count, current_database()::text AS database, "
                "current_setting('transaction_read_only')::text AS readonly "
                f"FROM {_identifier(schema)}.{_identifier(name)}"
            )
            counts = self._query(binding, schema, query, ("count", "database", "readonly"), 2)
            if (len(counts) != 1 or counts[0][1:] != [binding.database, "on"]
                    or not str(counts[0][0]).isdigit()):
                _fail("G-S1-SOURCE-READONLY", "Chat2DB 未返回完整的只读行数证据")
            count = int(counts[0][0])
            if count > self.max_rows:
                _fail("CHAT2DB_CAPTURE_BUDGET", "来源超过本次采集行数预算，未截断导入")
            tables.append(SourceTableProfile(table=table, row_count=count, columns=columns,
                schema_sha256=_sha256([c.model_dump(mode="json") for c in columns])))
        canonical = {"project_id": binding.project_id, "source_id": binding.source_id,
                     "engine": binding.engine, "database": binding.database, "readonly_verified": True,
                     "tables": [t.model_dump(mode="json") for t in tables]}
        return SourceProfileReceipt(**canonical, profiled_at=datetime.now(UTC), access_mode="READ_ONLY",
            schema_fingerprint=_sha256(canonical["tables"]), receipt_sha256=_sha256(canonical))

    def rows(self, binding: SourceBinding, table: str, columns: list[str]) -> Iterator[dict[str, Any]]:
        _, schema, name = self._scope(binding, table)
        allowed = binding.authorized_columns.get(table)
        if (not columns or len(columns) != len(set(columns)) or len(columns) > 500
                or (allowed is not None and not set(columns).issubset(allowed))):
            _fail("G-S1-SOURCE-SCOPE", "Chat2DB 查询字段超出正式范围或含重复项")
        for column in columns:
            _identifier(column)
        self._guard(binding, schema)
        offset, seen_bytes, expected = 0, 0, None
        content_digest = hashlib.md5(usedforsecurity=False)
        while True:
            projection = ", ".join(_identifier(c) for c in columns)
            # Ordering the full projection also handles tables without a PK and
            # legitimate duplicate rows. Global ordinals detect a repeated page.
            query = f"""WITH projected AS (
                SELECT to_jsonb(selected) AS body FROM (
                    SELECT {projection} FROM {_identifier(schema)}.{_identifier(name)}
                ) selected
            ), ordered AS (
                SELECT body::text AS body, md5(body::text) AS digest,
                    row_number() OVER (ORDER BY body::text COLLATE "C") AS ordinal
                FROM projected
            ), meta AS (
                SELECT count(*)::text AS total,
                    md5(COALESCE(string_agg(digest, '' ORDER BY ordinal), '')) AS fingerprint
                FROM ordered
            ), page AS (
                SELECT body, digest, ordinal FROM ordered ORDER BY ordinal
                LIMIT {self.page_size} OFFSET {offset}
            ) SELECT meta.total, meta.fingerprint, page.ordinal::text AS ordinal,
                encode(convert_to(page.body, 'UTF8'),'hex') AS row_hex,
                page.digest AS row_digest, current_setting('transaction_read_only')::text AS readonly,
                current_database()::text AS database
            FROM meta LEFT JOIN page ON true ORDER BY page.ordinal"""
            raw = self._query(binding, schema, query, _PAGE_FIELDS, self.page_size + 1)
            if not raw:
                _fail("CHAT2DB_INCOMPLETE", "Chat2DB 未返回快照页证据")
            signature = tuple(raw[0][:2])
            if (not str(signature[0]).isdigit() or not _MD5.fullmatch(str(signature[1]))
                    or (expected is not None and signature != expected)):
                _fail("CHAT2DB_SOURCE_CHANGED", "Chat2DB 来源在分页期间变化或指纹无效")
            expected = signature
            total = int(signature[0])
            if total > self.max_rows:
                _fail("CHAT2DB_CAPTURE_BUDGET", "来源超过本次采集行数预算，未截断导入")
            if total == 0:
                if (signature[1] != content_digest.hexdigest()
                        or raw != [["0", signature[1], None, None, None, "on", binding.database]]):
                    _fail("CHAT2DB_INCOMPLETE", "Chat2DB 空表证据无效")
                return
            if len(raw) != min(self.page_size, total - offset):
                _fail("CHAT2DB_INCOMPLETE", "Chat2DB 分页行数不完整")
            decoded = []
            for position, row in enumerate(raw, start=offset + 1):
                if (tuple(row[:2]) != expected or row[2] != str(position)
                        or row[5:] != ["on", binding.database]
                        or not isinstance(row[3], str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", row[3])
                        or not _MD5.fullmatch(str(row[4]))):
                    _fail("CHAT2DB_INCOMPLETE", "Chat2DB 来源身份、只读状态或分页序号不一致")
                try:
                    content = bytes.fromhex(row[3])
                    if hashlib.md5(content, usedforsecurity=False).hexdigest() != row[4]:
                        raise ValueError("Cell hash mismatch")
                    value = json.loads(content.decode("utf-8"), parse_float=Decimal,
                                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(value, dict) or set(value) != set(columns):
                        raise ValueError("Projection mismatch")
                except (ValueError, UnicodeError, TypeError):
                    _fail("CHAT2DB_INCOMPLETE", "Chat2DB 数据单元格被截断、类型无效或字段范围不符")
                seen_bytes += len(content)
                if seen_bytes > self.max_bytes:
                    _fail("CHAT2DB_CAPTURE_BUDGET", "来源超过本次采集字节预算，未晋升部分数据")
                decoded.append(value)
                content_digest.update(row[4].encode("ascii"))
            if offset + len(raw) == total and content_digest.hexdigest() != signature[1]:
                _fail("CHAT2DB_INCOMPLETE", "Chat2DB 完整数据与来源全量指纹不符")
            yield from decoded
            offset += len(raw)
            if offset == total:
                return
