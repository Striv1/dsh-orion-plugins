from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.config import Settings
from services.realtime_qa.api import CoreRealtimeRuntimeProvider
from services.realtime_qa.postgres_registry import PostgresDocumentCurrentRegistry


def test_database_access_is_unconfigured_without_explicit_environment(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings().database_url == ""
    assert Settings.from_env().database_url == ""


def test_explicit_database_configuration_is_preserved(monkeypatch):
    configured = "postgresql+psycopg://database.example.invalid/ontology?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", configured)
    settings = Settings.from_env()
    assert settings.database_url == configured
    registry = PostgresDocumentCurrentRegistry(settings.database_url)
    assert registry.database_url == "postgresql://database.example.invalid/ontology?sslmode=require"


@pytest.mark.parametrize("database_url", ["", " ", "\t\n"])
def test_unconfigured_registry_cannot_fall_back_to_ambient_postgres(monkeypatch, database_url):
    monkeypatch.setenv("PGHOST", "ambient.example.invalid")
    monkeypatch.setenv("PGDATABASE", "ambient_database")

    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("missing database configuration must fail before connecting")

    monkeypatch.setattr("services.realtime_qa.postgres_registry.psycopg.connect", forbidden_connect)
    with pytest.raises(ValueError, match="Configure DATABASE_URL explicitly"):
        PostgresDocumentCurrentRegistry(database_url).count_current("project-a")


def test_empty_desktop_registry_still_loads_without_database(tmp_path: Path, monkeypatch):
    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("an empty desktop catalog must not connect to PostgreSQL")

    monkeypatch.setattr("services.realtime_qa.postgres_registry.psycopg.connect", forbidden_connect)
    path = tmp_path / "runtime-registry.json"
    path.write_text(json.dumps({"schema_version": 1, "runtimes": [], "default_project_id": None}))
    provider = CoreRealtimeRuntimeProvider(Settings(realtime_runtime_registry_path=str(path)))
    health = provider.health()
    assert health["status"] == "DEGRADED"
    assert health["readiness_reason"] == "NO_PUBLISHED_RUNTIME"
    assert health["reload_error"] is None
