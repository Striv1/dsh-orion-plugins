from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from services.structured_data.multi_source import (
    CanonicalIdentityLink,
    CrossSourceSnapshotSet,
    EntityResolutionContract,
    IdentityResolutionReceipt,
    IdentityRowReference,
    MultiSourceContractError,
    SourceBinding,
    SourceColumnProfile,
    SourceProfileReceipt,
    SourceSnapshotManifest,
    SourceTableProfile,
    TableSnapshot,
    build_cross_source_snapshot_set,
)


def _sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return value


def _text_value(value: Any) -> str | None:
    normalized = _json_value(value)
    if normalized is None:
        return None
    if isinstance(normalized, str):
        return normalized
    if isinstance(normalized, bool):
        return str(normalized).lower()
    if isinstance(normalized, int | float):
        return str(normalized)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _split_table(binding: SourceBinding, table: str) -> tuple[str, str]:
    if "." in table:
        schema_name, table_name = table.split(".", 1)
        if schema_name not in binding.schemas:
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: table schema is outside the binding")
        return schema_name, table_name
    if len(binding.schemas) != 1:
        raise MultiSourceContractError(
            "G-S1-SOURCE-SCOPE: unqualified table requires exactly one schema"
        )
    return binding.schemas[0], table


def resolve_connection_ref(connection_ref: str) -> str:
    if not connection_ref.startswith("env://"):
        raise MultiSourceContractError(
            "SOURCE_NOT_BOUND: only env:// connection references are locally resolvable"
        )
    variable = connection_ref.removeprefix("env://")
    value = os.environ.get(variable, "").strip()
    if not value:
        raise MultiSourceContractError(
            f"SOURCE_NOT_BOUND: connection environment is missing: {variable}"
        )
    return value


class SourceReader(Protocol):
    engine: str

    def profile(self, binding: SourceBinding) -> SourceProfileReceipt: ...

    def rows(
        self, binding: SourceBinding, table: str, columns: list[str]
    ) -> Iterator[dict[str, Any]]: ...


class PostgresSourceReader:
    engine = "POSTGRESQL"

    @staticmethod
    def _url(value: str) -> str:
        return value.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def _connection(self, binding: SourceBinding):
        connection = psycopg.connect(self._url(resolve_connection_ref(binding.connection_ref)))
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SHOW transaction_read_only")
                if str(cursor.fetchone()[0]).lower() != "on":
                    raise MultiSourceContractError(
                        "G-S1-SOURCE-READONLY: PostgreSQL read-only transaction not verified"
                    )
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def profile(self, binding: SourceBinding) -> SourceProfileReceipt:
        observed_at = datetime.now(UTC)
        tables: list[SourceTableProfile] = []
        with self._connection(binding) as connection, connection.cursor() as cursor:
            for table in binding.authorized_tables:
                schema_name, table_name = _split_table(binding, table)
                cursor.execute(
                    """
                    SELECT column_name, data_type, is_nullable, ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (schema_name, table_name),
                )
                raw_columns = cursor.fetchall()
                if not raw_columns:
                    raise MultiSourceContractError(
                        f"G-S1-SOURCE-SCOPE: authorized table does not exist: {table}"
                    )
                columns = [
                    SourceColumnProfile(
                        name=str(item[0]),
                        data_type=str(item[1]),
                        nullable=str(item[2]).upper() == "YES",
                        ordinal_position=int(item[3]),
                    )
                    for item in raw_columns
                ]
                allowed = binding.authorized_columns.get(table)
                if allowed:
                    existing = {item.name for item in columns}
                    if not set(allowed).issubset(existing):
                        raise MultiSourceContractError(
                            f"G-S1-SOURCE-SCOPE: authorized column missing from {table}"
                        )
                    columns = [item for item in columns if item.name in allowed]
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                        sql.Identifier(schema_name), sql.Identifier(table_name)
                    )
                )
                row_count = int(cursor.fetchone()[0])
                schema_payload = [item.model_dump(mode="json") for item in columns]
                tables.append(
                    SourceTableProfile(
                        table=table,
                        row_count=row_count,
                        columns=columns,
                        schema_sha256=_sha256(schema_payload),
                    )
                )
        canonical = {
            "project_id": binding.project_id,
            "source_id": binding.source_id,
            "engine": binding.engine,
            "database": binding.database,
            "tables": [item.model_dump(mode="json") for item in tables],
            "readonly_verified": True,
        }
        fingerprint = _sha256(canonical["tables"])
        return SourceProfileReceipt(
            **canonical,
            profiled_at=observed_at,
            access_mode="READ_ONLY",
            schema_fingerprint=fingerprint,
            receipt_sha256=_sha256(canonical),
        )

    def rows(
        self, binding: SourceBinding, table: str, columns: list[str]
    ) -> Iterator[dict[str, Any]]:
        schema_name, table_name = _split_table(binding, table)
        with self._connection(binding) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT {} FROM {}.{}").format(
                    sql.SQL(", ").join(sql.Identifier(item) for item in columns),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                )
            )
            for row in cursor:
                yield {name: _json_value(value) for name, value in zip(columns, row, strict=True)}


class MySQLSourceReader:
    engine = "MYSQL"

    @staticmethod
    def _connect(binding: SourceBinding):
        try:
            import pymysql
        except ImportError as exc:
            raise MultiSourceContractError(
                "UNSUPPORTED_OPERATOR: MySQL connector pymysql is not installed"
            ) from exc
        parsed = urlparse(resolve_connection_ref(binding.connection_ref))
        if parsed.scheme not in {"mysql", "mysql+pymysql"}:
            raise MultiSourceContractError("SOURCE_NOT_BOUND: invalid MySQL connection URL")
        return pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/") or binding.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=5,
        )

    @contextmanager
    def _connection(self, binding: SourceBinding):
        connection = self._connect(binding)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION READ ONLY")
                try:
                    cursor.execute("SELECT @@session.transaction_read_only")
                except Exception:
                    cursor.execute("SELECT @@session.tx_read_only")
                if int(cursor.fetchone()[0]) != 1:
                    raise MultiSourceContractError(
                        "G-S1-SOURCE-READONLY: MySQL read-only transaction not verified"
                    )
                cursor.execute("SHOW GRANTS FOR CURRENT_USER")
                grants = "\n".join(str(item[0]).upper() for item in cursor.fetchall())
                forbidden = (
                    "ALL PRIVILEGES",
                    " INSERT",
                    " UPDATE",
                    " DELETE",
                    " CREATE",
                    " DROP",
                    " ALTER",
                    " GRANT OPTION",
                )
                if "SELECT" not in grants or any(token in grants for token in forbidden):
                    raise MultiSourceContractError(
                        "G-S1-SOURCE-READONLY: MySQL account has write-capable grants"
                    )
            yield connection
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _quote(value: str) -> str:
        return "`" + value.replace("`", "``") + "`"

    def profile(self, binding: SourceBinding) -> SourceProfileReceipt:
        observed_at = datetime.now(UTC)
        tables: list[SourceTableProfile] = []
        with self._connection(binding) as connection, connection.cursor() as cursor:
            for table in binding.authorized_tables:
                schema_name, table_name = _split_table(binding, table)
                cursor.execute(
                    """
                    SELECT column_name, column_type, is_nullable, ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (schema_name, table_name),
                )
                raw_columns = cursor.fetchall()
                if not raw_columns:
                    raise MultiSourceContractError(
                        f"G-S1-SOURCE-SCOPE: authorized table does not exist: {table}"
                    )
                columns = [
                    SourceColumnProfile(
                        name=str(item[0]),
                        data_type=str(item[1]),
                        nullable=str(item[2]).upper() == "YES",
                        ordinal_position=int(item[3]),
                    )
                    for item in raw_columns
                ]
                allowed = binding.authorized_columns.get(table)
                if allowed:
                    existing = {item.name for item in columns}
                    if not set(allowed).issubset(existing):
                        raise MultiSourceContractError(
                            f"G-S1-SOURCE-SCOPE: authorized column missing from {table}"
                        )
                    columns = [item for item in columns if item.name in allowed]
                cursor.execute(
                    f"SELECT COUNT(*) FROM {self._quote(schema_name)}.{self._quote(table_name)}"
                )
                row_count = int(cursor.fetchone()[0])
                tables.append(
                    SourceTableProfile(
                        table=table,
                        row_count=row_count,
                        columns=columns,
                        schema_sha256=_sha256([item.model_dump(mode="json") for item in columns]),
                    )
                )
        canonical = {
            "project_id": binding.project_id,
            "source_id": binding.source_id,
            "engine": binding.engine,
            "database": binding.database,
            "tables": [item.model_dump(mode="json") for item in tables],
            "readonly_verified": True,
        }
        return SourceProfileReceipt(
            **canonical,
            profiled_at=observed_at,
            access_mode="READ_ONLY",
            schema_fingerprint=_sha256(canonical["tables"]),
            receipt_sha256=_sha256(canonical),
        )

    def rows(
        self, binding: SourceBinding, table: str, columns: list[str]
    ) -> Iterator[dict[str, Any]]:
        schema_name, table_name = _split_table(binding, table)
        selected = ", ".join(self._quote(item) for item in columns)
        query = f"SELECT {selected} FROM {self._quote(schema_name)}.{self._quote(table_name)}"
        with self._connection(binding) as connection, connection.cursor() as cursor:
            cursor.execute(query)
            for row in cursor:
                yield {name: _json_value(value) for name, value in zip(columns, row, strict=True)}


class SnapshotHub:
    """PostgreSQL-backed, project-scoped multi-source snapshot coordinator."""

    def __init__(self, target_database_url: str) -> None:
        self.target_database_url = target_database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        self.readers: dict[str, SourceReader] = {
            "POSTGRESQL": PostgresSourceReader(),
            "MYSQL": MySQLSourceReader(),
        }

    def _reader(self, binding: SourceBinding) -> SourceReader:
        if binding.connection_ref.startswith("chat2db://"):
            from .chat2db_source import Chat2DBSourceReader

            if binding.engine != Chat2DBSourceReader.engine:
                raise MultiSourceContractError("UNSUPPORTED_OPERATOR: Chat2DB snapshot adapter supports POSTGRESQL only")
            return Chat2DBSourceReader()
        reader = self.readers.get(binding.engine)
        if reader is None:
            raise MultiSourceContractError(
                f"UNSUPPORTED_OPERATOR: source engine is not implemented: {binding.engine}"
            )
        return reader

    def register_sources(self, bindings: list[SourceBinding]) -> list[SourceBinding]:
        if not bindings or len({item.project_id for item in bindings}) != 1:
            raise MultiSourceContractError(
                "G-S1-MULTI-SOURCE-INVENTORY: one project and at least one source are required"
            )
        if len({item.source_id for item in bindings}) != len(bindings):
            raise MultiSourceContractError("G-S1-MULTI-SOURCE-INVENTORY: duplicate source_id")
        with psycopg.connect(self.target_database_url) as connection, connection.cursor() as cursor:
            for binding in bindings:
                cursor.execute(
                    """
                    INSERT INTO orion_catalog.source_bindings (
                        project_id, source_id, engine, connection_ref, database_name,
                        catalog_name, schemas, authorized_tables, authorized_columns,
                        access_mode, readonly_attested, pii_scope, owner_name, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, source_id) DO UPDATE SET
                        engine=EXCLUDED.engine,
                        connection_ref=EXCLUDED.connection_ref,
                        database_name=EXCLUDED.database_name,
                        catalog_name=EXCLUDED.catalog_name,
                        schemas=EXCLUDED.schemas,
                        authorized_tables=EXCLUDED.authorized_tables,
                        authorized_columns=EXCLUDED.authorized_columns,
                        access_mode=EXCLUDED.access_mode,
                        readonly_attested=EXCLUDED.readonly_attested,
                        pii_scope=EXCLUDED.pii_scope,
                        owner_name=EXCLUDED.owner_name,
                        status=EXCLUDED.status,
                        updated_at=NOW()
                    """,
                    (
                        binding.project_id,
                        binding.source_id,
                        binding.engine,
                        binding.connection_ref,
                        binding.database,
                        binding.catalog,
                        binding.schemas,
                        binding.authorized_tables,
                        Jsonb(binding.authorized_columns),
                        binding.access_mode,
                        binding.readonly_attested,
                        binding.pii_scope,
                        binding.owner,
                        binding.status,
                    ),
                )
        return bindings

    def profile_sources(self, bindings: list[SourceBinding]) -> list[SourceProfileReceipt]:
        receipts = [self._reader(binding).profile(binding) for binding in bindings]
        with psycopg.connect(self.target_database_url) as connection, connection.cursor() as cursor:
            for receipt in receipts:
                cursor.execute(
                    """
                    INSERT INTO orion_catalog.source_profiles (
                        project_id, source_id, profiled_at, schema_fingerprint,
                        receipt_sha256, profile
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        receipt.project_id,
                        receipt.source_id,
                        receipt.profiled_at,
                        receipt.schema_fingerprint,
                        receipt.receipt_sha256,
                        Jsonb(receipt.model_dump(mode="json")),
                    ),
                )
        return receipts

    def capture_and_promote(
        self,
        *,
        project_id: str,
        bindings: list[SourceBinding],
        snapshot_version: str,
        dataset_type: str,
        production_evidence: bool,
        max_skew_seconds: int = 300,
    ) -> CrossSourceSnapshotSet:
        if any(not binding.authorized_columns for binding in bindings):
            raise MultiSourceContractError(
                "G-S1-SOURCE-SCOPE: snapshot capture requires explicit column allowlists"
            )
        profiles = [self._reader(binding).profile(binding) for binding in bindings]
        staged_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        manifests: list[SourceSnapshotManifest] = []
        for binding, profile in zip(bindings, profiles, strict=True):
            reader = self._reader(binding)
            table_manifests: list[TableSnapshot] = []
            source_digest_input: list[dict[str, Any]] = []
            for table_profile in profile.tables:
                columns = [item.name for item in table_profile.columns]
                rows = list(reader.rows(binding, table_profile.table, columns))
                if len(rows) != table_profile.row_count:
                    raise MultiSourceContractError(
                        "G-S1-SNAPSHOT-RECONCILIATION: source changed during snapshot"
                    )
                staged_rows[(binding.source_id, table_profile.table)] = rows
                source_digest_input.append({"table": table_profile.table, "rows": rows})
                table_hash = hashlib.sha256(
                    (
                        f"{project_id}:{binding.source_id}:{table_profile.table}:"
                        f"{table_profile.schema_sha256}"
                    ).encode()
                ).hexdigest()[:20]
                table_manifests.append(
                    TableSnapshot(
                        table=table_profile.table,
                        target_table=f"ms_{binding.source_id[:24]}_{table_hash}".replace("-", "_"),
                        source_row_count=table_profile.row_count,
                        snapshot_row_count=len(rows),
                        schema_sha256=table_profile.schema_sha256,
                        source_locator=(
                            f"{binding.engine.lower()}://{binding.source_id}/"
                            f"{table_profile.table}@{snapshot_version}"
                        ),
                    )
                )
            dataset_id = (
                "DS-"
                + hashlib.sha256(f"{project_id}:{binding.source_id}:{snapshot_version}".encode())
                .hexdigest()[:24]
                .upper()
            )
            manifests.append(
                SourceSnapshotManifest(
                    project_id=project_id,
                    source_id=binding.source_id,
                    engine=binding.engine,
                    dataset_id=dataset_id,
                    snapshot_version=snapshot_version,
                    captured_at=profile.profiled_at,
                    snapshot_complete=True,
                    source_sha256=_sha256(source_digest_input),
                    pii_scope=binding.pii_scope,
                    tables=table_manifests,
                    dataset_type=dataset_type,
                    production_evidence=production_evidence,
                )
            )
        snapshot_set = build_cross_source_snapshot_set(
            project_id=project_id,
            bindings=bindings,
            snapshots=manifests,
            max_skew_seconds=max_skew_seconds,
        )
        with psycopg.connect(self.target_database_url) as connection, connection.cursor() as cursor:
            for manifest in manifests:
                cursor.execute(
                    """
                    INSERT INTO orion_catalog.source_snapshots (
                        dataset_id, project_id, source_id, snapshot_version, captured_at,
                        snapshot_complete, source_sha256, dataset_type,
                        production_evidence, manifest
                    ) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s)
                    ON CONFLICT (dataset_id) DO NOTHING
                    """,
                    (
                        manifest.dataset_id,
                        project_id,
                        manifest.source_id,
                        manifest.snapshot_version,
                        manifest.captured_at,
                        manifest.source_sha256,
                        manifest.dataset_type,
                        manifest.production_evidence,
                        Jsonb(manifest.model_dump(mode="json")),
                    ),
                )
                cursor.execute(
                    "DELETE FROM orion_data.multi_source_snapshot_rows WHERE dataset_id=%s",
                    (manifest.dataset_id,),
                )
                ordinal = 0
                for table in manifest.tables:
                    profile = next(
                        item
                        for item in profiles[
                            next(
                                index
                                for index, item in enumerate(bindings)
                                if item.source_id == manifest.source_id
                            )
                        ].tables
                        if item.table == table.table
                    )
                    columns = [item.name for item in profile.columns]
                    cursor.execute(
                        sql.SQL(
                            "CREATE TABLE IF NOT EXISTS orion_data.{} ("
                            "dataset_id TEXT NOT NULL, row_ordinal BIGINT NOT NULL, "
                            "row_sha256 TEXT NOT NULL, {}, "
                            "PRIMARY KEY (dataset_id, row_ordinal))"
                        ).format(
                            sql.Identifier(table.target_table),
                            sql.SQL(", ").join(
                                sql.SQL("{} TEXT").format(sql.Identifier(column))
                                for column in columns
                            ),
                        )
                    )
                    cursor.execute(
                        sql.SQL("DELETE FROM orion_data.{} WHERE dataset_id=%s").format(
                            sql.Identifier(table.target_table)
                        ),
                        (manifest.dataset_id,),
                    )
                    for row in staged_rows[(manifest.source_id, table.table)]:
                        ordinal += 1
                        row_sha256 = _sha256(row)
                        cursor.execute(
                            """
                            INSERT INTO orion_data.multi_source_snapshot_rows (
                                dataset_id, project_id, source_id, table_name,
                                row_ordinal, row_sha256, row_payload
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                manifest.dataset_id,
                                project_id,
                                manifest.source_id,
                                table.table,
                                ordinal,
                                row_sha256,
                                Jsonb(row),
                            ),
                        )
                        cursor.execute(
                            sql.SQL(
                                "INSERT INTO orion_data.{} "
                                "(dataset_id, row_ordinal, row_sha256, {}) "
                                "VALUES (%s, %s, %s, {})"
                            ).format(
                                sql.Identifier(table.target_table),
                                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                            ),
                            (
                                manifest.dataset_id,
                                ordinal,
                                row_sha256,
                                *[_text_value(row.get(column)) for column in columns],
                            ),
                        )
            cursor.execute(
                """
                INSERT INTO orion_catalog.snapshot_sets (
                    snapshot_set_id, project_id, snapshot_version, promoted_at,
                    snapshot_complete, manifest_sha256, production_evidence, manifest
                ) VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s)
                ON CONFLICT (snapshot_set_id) DO NOTHING
                """,
                (
                    snapshot_set.snapshot_set_id,
                    project_id,
                    snapshot_set.snapshot_version,
                    snapshot_set.promoted_at,
                    snapshot_set.manifest_sha256,
                    snapshot_set.production_evidence,
                    Jsonb(snapshot_set.model_dump(mode="json")),
                ),
            )
            for manifest in manifests:
                cursor.execute(
                    """
                    INSERT INTO orion_catalog.snapshot_set_members (
                        snapshot_set_id, dataset_id, source_id
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        snapshot_set.snapshot_set_id,
                        manifest.dataset_id,
                        manifest.source_id,
                    ),
                )
            cursor.execute(
                """
                INSERT INTO orion_catalog.current_snapshot_sets (
                    project_id, snapshot_set_id, revision, promoted_at
                ) VALUES (%s, %s, 1, %s)
                ON CONFLICT (project_id) DO UPDATE SET
                    snapshot_set_id=EXCLUDED.snapshot_set_id,
                    revision=orion_catalog.current_snapshot_sets.revision + 1,
                    promoted_at=EXCLUDED.promoted_at
                """,
                (project_id, snapshot_set.snapshot_set_id, snapshot_set.promoted_at),
            )
        return snapshot_set

    def current_snapshot_set(self, project_id: str) -> CrossSourceSnapshotSet | None:
        with psycopg.connect(self.target_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT s.manifest
                FROM orion_catalog.current_snapshot_sets c
                JOIN orion_catalog.snapshot_sets s
                  ON s.snapshot_set_id=c.snapshot_set_id
                WHERE c.project_id=%s
                """,
                (project_id,),
            )
            row = cursor.fetchone()
        return CrossSourceSnapshotSet.model_validate(row[0]) if row else None

    def resolve_identities(
        self,
        *,
        snapshot_set: CrossSourceSnapshotSet,
        contract: EntityResolutionContract,
    ) -> IdentityResolutionReceipt:
        """Resolve explicit cross-source keys for exactly one immutable snapshot set."""

        if contract.project_id != snapshot_set.project_id:
            raise MultiSourceContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: contract belongs to another project"
            )
        snapshots = {item.source_id: item for item in snapshot_set.source_snapshots}
        if set(contract.source_join_keys) != set(snapshots):
            raise MultiSourceContractError(
                "G-S3-CROSS-SOURCE-IDENTITY: contract sources do not match snapshot set"
            )
        rows_by_source: dict[str, dict[str, list[IdentityRowReference]]] = {}
        with psycopg.connect(self.target_database_url) as connection, connection.cursor() as cursor:
            for source_id, keys in contract.source_join_keys.items():
                snapshot = snapshots[source_id]
                table = contract.source_tables[source_id]
                if table not in {item.table for item in snapshot.tables}:
                    raise MultiSourceContractError(
                        "G-S3-CROSS-SOURCE-IDENTITY: identity table is not in the snapshot"
                    )
                cursor.execute(
                    """
                    SELECT row_ordinal, row_sha256, row_payload
                    FROM orion_data.multi_source_snapshot_rows
                    WHERE dataset_id=%s AND source_id=%s AND table_name=%s
                    ORDER BY row_ordinal
                    """,
                    (snapshot.dataset_id, source_id, table),
                )
                grouped: dict[str, list[IdentityRowReference]] = {}
                for ordinal, row_sha256, payload in cursor.fetchall():
                    values: list[str] = []
                    for key in keys:
                        value = payload.get(key)
                        if value is None or not str(value).strip():
                            raise MultiSourceContractError(
                                "G-S3-CROSS-SOURCE-IDENTITY: join key is null or empty"
                            )
                        values.append(
                            self._normalize_identity_value(str(value), contract.normalization)
                        )
                    normalized = "\u001f".join(values)
                    grouped.setdefault(normalized, []).append(
                        IdentityRowReference(
                            source_id=source_id,
                            dataset_id=snapshot.dataset_id,
                            table=table,
                            row_ordinal=int(ordinal),
                            row_sha256=str(row_sha256),
                        )
                    )
                rows_by_source[source_id] = grouped

            self._validate_identity_cardinality(rows_by_source, contract)
            shared = set.intersection(*(set(rows) for rows in rows_by_source.values()))
            links: list[CanonicalIdentityLink] = []
            for normalized in sorted(shared):
                digest = _sha256({"contract_id": contract.contract_id, "key": normalized})
                references = [
                    reference
                    for source_id in sorted(rows_by_source)
                    for reference in rows_by_source[source_id][normalized]
                ]
                links.append(
                    CanonicalIdentityLink(
                        canonical_key_sha256=digest,
                        canonical_iri=(
                            f"urn:orion:canonical:{contract.canonical_entity}:"
                            f"{digest.removeprefix('sha256:')}"
                        ),
                        source_rows=references,
                    )
                )
            total_rows = sum(
                len(references)
                for grouped in rows_by_source.values()
                for references in grouped.values()
            )
            matched_rows = sum(len(link.source_rows) for link in links)
            canonical = {
                "contract_id": contract.contract_id,
                "project_id": contract.project_id,
                "snapshot_set_id": snapshot_set.snapshot_set_id,
                "links": [item.model_dump(mode="json") for item in links],
                "unmatched_row_count": total_rows - matched_rows,
            }
            receipt = IdentityResolutionReceipt(
                **canonical,
                resolved_at=datetime.now(UTC),
                matched_identity_count=len(links),
                receipt_sha256=_sha256(canonical),
            )
            cursor.execute(
                "DELETE FROM orion_data.canonical_identity_links "
                "WHERE snapshot_set_id=%s AND contract_id=%s",
                (snapshot_set.snapshot_set_id, contract.contract_id),
            )
            cursor.execute(
                "DELETE FROM orion_data.canonical_identity_members "
                "WHERE snapshot_set_id=%s AND contract_id=%s",
                (snapshot_set.snapshot_set_id, contract.contract_id),
            )
            for link in links:
                cursor.execute(
                    """
                    INSERT INTO orion_data.canonical_identity_links (
                        snapshot_set_id, contract_id, project_id,
                        canonical_key_sha256, canonical_iri, source_rows
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot_set.snapshot_set_id,
                        contract.contract_id,
                        contract.project_id,
                        link.canonical_key_sha256,
                        link.canonical_iri,
                        Jsonb([item.model_dump(mode="json") for item in link.source_rows]),
                    ),
                )
                for reference in link.source_rows:
                    cursor.execute(
                        """
                        INSERT INTO orion_data.canonical_identity_members (
                            snapshot_set_id, contract_id, project_id,
                            canonical_key_sha256, canonical_iri, source_id,
                            dataset_id, table_name, row_ordinal, row_sha256
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            snapshot_set.snapshot_set_id,
                            contract.contract_id,
                            contract.project_id,
                            link.canonical_key_sha256,
                            link.canonical_iri,
                            reference.source_id,
                            reference.dataset_id,
                            reference.table,
                            reference.row_ordinal,
                            reference.row_sha256,
                        ),
                    )
            cursor.execute(
                """
                INSERT INTO orion_catalog.identity_resolution_receipts (
                    receipt_sha256, contract_id, project_id, snapshot_set_id,
                    resolved_at, matched_identity_count, unmatched_row_count, receipt
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (receipt_sha256) DO NOTHING
                """,
                (
                    receipt.receipt_sha256,
                    receipt.contract_id,
                    receipt.project_id,
                    receipt.snapshot_set_id,
                    receipt.resolved_at,
                    receipt.matched_identity_count,
                    receipt.unmatched_row_count,
                    Jsonb(receipt.model_dump(mode="json")),
                ),
            )
        return receipt

    @staticmethod
    def _normalize_identity_value(value: str, normalization: str) -> str:
        if normalization == "EXACT":
            return value
        if normalization == "TRIM_UPPER":
            return value.strip().upper()
        if normalization == "STABLE_HASH":
            normalized = value.strip().upper()
            if not normalized.startswith("HASH-"):
                raise MultiSourceContractError(
                    "G-S3-CROSS-SOURCE-IDENTITY: STABLE_HASH key is not a stable hash identifier"
                )
            return normalized
        raise MultiSourceContractError(
            "G-S3-CROSS-SOURCE-IDENTITY: unsupported identity normalization"
        )

    @staticmethod
    def _validate_identity_cardinality(
        rows_by_source: dict[str, dict[str, list[IdentityRowReference]]],
        contract: EntityResolutionContract,
    ) -> None:
        authority = contract.source_authority[0]
        for source_id, grouped in rows_by_source.items():
            for references in grouped.values():
                count = len(references)
                invalid = (
                    (contract.cardinality == "ONE_TO_ONE" and count > 1)
                    or (
                        contract.cardinality == "MANY_TO_ONE"
                        and source_id == authority
                        and count > 1
                    )
                    or (
                        contract.cardinality == "ONE_TO_MANY"
                        and source_id != authority
                        and count > 1
                    )
                )
                if invalid or (count > 1 and contract.collision_policy == "FAIL"):
                    raise MultiSourceContractError(
                        "G-S3-CROSS-SOURCE-IDENTITY: duplicate key violates cardinality/collision policy"
                    )
