from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from services.ontology_engineering import storage as storage_module
from services.ontology_engineering.storage import (
    ORION_WORKFLOW_SCHEMA_VERSION,
    PostgresWorkflowMetadataStore,
    WorkflowSchemaCompatibilityError,
)


@dataclass
class FakeDatabase:
    migration_history: list[tuple[int, str, str, datetime]] = field(default_factory=list)
    baseline_execution_count: int = 0


class FakeCursor:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str, parameters: Any = None) -> None:
        normalized = " ".join(statement.split())
        if normalized.startswith(
            "SELECT version, name, checksum_sha256, applied_at "
            "FROM orion_workflow.schema_migrations"
        ):
            self._rows = list(self.database.migration_history)
            return
        if normalized.startswith("INSERT INTO orion_workflow.schema_migrations"):
            version, name, checksum = parameters
            self.database.migration_history.append(
                (int(version), str(name), str(checksum), datetime(2026, 8, 31, tzinfo=UTC))
            )
            return
        if "CREATE TABLE IF NOT EXISTS orion_workflow.projects" in statement:
            self.database.baseline_execution_count += 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database)


def _use_fake_database(monkeypatch: pytest.MonkeyPatch, database: FakeDatabase) -> None:
    monkeypatch.setattr(
        storage_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(database),
    )


def test_ensure_schema_applies_baseline_and_records_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    _use_fake_database(monkeypatch, database)
    store = PostgresWorkflowMetadataStore("postgresql://unused")

    store.ensure_schema()

    assert database.baseline_execution_count == 1
    assert len(database.migration_history) == 2
    version, name, checksum, _applied_at = database.migration_history[-1]
    assert version == ORION_WORKFLOW_SCHEMA_VERSION
    assert name == "002_realtime_document_current.sql"
    assert len(checksum) == 64


def test_ensure_schema_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    database = FakeDatabase()
    _use_fake_database(monkeypatch, database)
    store = PostgresWorkflowMetadataStore("postgresql://unused")

    store.ensure_schema()
    store.ensure_schema()

    assert database.baseline_execution_count == 1
    assert [row[0] for row in database.migration_history] == [1, 2]


def test_ensure_schema_rejects_database_newer_than_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase(
        migration_history=[
            (
                ORION_WORKFLOW_SCHEMA_VERSION + 1,
                "003_future.sql",
                "future-checksum",
                datetime(2026, 8, 31, tzinfo=UTC),
            )
        ]
    )
    _use_fake_database(monkeypatch, database)
    store = PostgresWorkflowMetadataStore("postgresql://unused")

    with pytest.raises(WorkflowSchemaCompatibilityError, match="高于当前应用支持范围"):
        store.ensure_schema()

    assert database.baseline_execution_count == 0
    assert len(database.migration_history) == 1


def test_migration_history_can_be_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    database = FakeDatabase()
    _use_fake_database(monkeypatch, database)
    store = PostgresWorkflowMetadataStore("postgresql://unused")
    store.ensure_schema()

    history = store.migration_history()

    assert history == [
        {
            "version": version,
            "name": name,
            "checksum_sha256": checksum,
            "applied_at": "2026-08-31T00:00:00+00:00",
        }
        for version, name, checksum, _applied_at in database.migration_history
    ]
