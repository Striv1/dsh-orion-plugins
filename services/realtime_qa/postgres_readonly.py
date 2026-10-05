from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg


class PostgresReadOnlyVerificationError(RuntimeError):
    pass


def attest_postgres_read_only_principal(
    database_url: str,
    *,
    principal: str,
    properties_path: Path,
    output_path: Path,
    verified_by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a PostgreSQL principal through catalog read-back and write an attestation.

    The administrative connection is used only for catalog SELECT statements. The
    attestation never contains the database URL or password.
    """

    principal = principal.strip()
    verified_by = verified_by.strip()
    if not principal or not verified_by:
        raise PostgresReadOnlyVerificationError(
            "principal and verified_by are required"
        )
    properties_path = properties_path.resolve()
    properties = _read_properties(properties_path)
    if properties.get("jdbc.user") != principal:
        raise PostgresReadOnlyVerificationError(
            "PostgreSQL principal does not match ontop.properties jdbc.user"
        )

    psycopg_url = database_url.replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    try:
        with psycopg.connect(psycopg_url) as connection, connection.cursor() as cursor:
            role = _read_role(cursor, principal)
            privilege_counts = _read_write_privilege_counts(cursor, principal)
            database_name = str(connection.info.dbname or "")
    except psycopg.Error as exc:
        raise PostgresReadOnlyVerificationError(
            "PostgreSQL catalog read-back failed"
        ) from exc

    unsafe_role_attributes = [
        name
        for name in (
            "rolsuper",
            "rolcreaterole",
            "rolcreatedb",
            "rolreplication",
            "rolbypassrls",
        )
        if bool(role[name])
    ]
    role_settings = _role_settings(role.get("rolconfig"))
    default_read_only = role_settings.get("default_transaction_read_only") == "on"
    effective_write_privileges = sum(privilege_counts.values())
    rejection_reasons: list[str] = []
    if not bool(role["rolcanlogin"]):
        rejection_reasons.append("principal cannot login")
    if unsafe_role_attributes:
        rejection_reasons.append(
            "unsafe role attributes: " + ", ".join(unsafe_role_attributes)
        )
    if effective_write_privileges:
        rejection_reasons.append(
            "effective write/create privileges are present"
        )
    if not default_read_only:
        rejection_reasons.append("default_transaction_read_only is not on")
    if rejection_reasons:
        raise PostgresReadOnlyVerificationError("; ".join(rejection_reasons))

    verified_at = (now or datetime.now().astimezone()).isoformat()
    attestation = {
        "schema_version": 1,
        "database_access_mode": "READ_ONLY",
        "write_privileges": False,
        "verification_method": "DATABASE_CATALOG_READBACK",
        "database_name": database_name,
        "database_principal": principal,
        "properties_sha256": _checksum(properties_path),
        "verified_by": verified_by,
        "verified_at": verified_at,
        "catalog_evidence": {
            "rolcanlogin": True,
            "unsafe_role_attributes": [],
            "default_transaction_read_only": "on",
            "effective_write_privilege_counts": privilege_counts,
        },
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(attestation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return attestation


def _read_role(cursor: psycopg.Cursor[Any], principal: str) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls, rolconfig
          FROM pg_catalog.pg_roles
         WHERE rolname = %s
        """,
        (principal,),
    )
    row = cursor.fetchone()
    if row is None:
        raise PostgresReadOnlyVerificationError(
            "PostgreSQL principal does not exist"
        )
    columns = [description.name for description in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def _read_write_privilege_counts(
    cursor: psycopg.Cursor[Any],
    principal: str,
) -> dict[str, int]:
    cursor.execute(
        """
        SELECT
          count(*) FILTER (
            WHERE c.relkind IN ('r', 'p', 'f')
              AND has_table_privilege(
                %s, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
          ) AS writable_relations,
          count(*) FILTER (
            WHERE c.relkind = 'S'
              AND has_sequence_privilege(%s, c.oid, 'USAGE,UPDATE')
          ) AS writable_sequences,
          count(DISTINCT n.oid) FILTER (
            WHERE n.nspname NOT LIKE 'pg_%%'
              AND n.nspname <> 'information_schema'
              AND has_schema_privilege(%s, n.oid, 'CREATE')
          ) AS creatable_schemas,
          CASE WHEN has_database_privilege(%s, current_database(), 'CREATE')
               THEN 1 ELSE 0 END AS creatable_database
        FROM pg_catalog.pg_namespace AS n
        LEFT JOIN pg_catalog.pg_class AS c ON c.relnamespace = n.oid
        WHERE n.nspname NOT LIKE 'pg_%%'
          AND n.nspname <> 'information_schema'
        """,
        (principal, principal, principal, principal),
    )
    row = cursor.fetchone()
    if row is None:
        raise PostgresReadOnlyVerificationError(
            "PostgreSQL privilege catalog returned no result"
        )
    columns = [description.name for description in cursor.description or ()]
    return {
        key: int(value or 0)
        for key, value in zip(columns, row, strict=True)
    }


def _role_settings(raw_settings: Any) -> dict[str, str]:
    settings: dict[str, str] = {}
    for raw_setting in raw_settings or ():
        key, separator, value = str(raw_setting).partition("=")
        if separator:
            settings[key.strip()] = value.strip().lower()
    return settings


def _read_properties(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PostgresReadOnlyVerificationError(
            "ontop.properties cannot be read"
        ) from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    if not values.get("jdbc.user"):
        raise PostgresReadOnlyVerificationError(
            "ontop.properties has no jdbc.user"
        )
    return values


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
