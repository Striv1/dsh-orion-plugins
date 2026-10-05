from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from services.structured_data.multi_source import (
    SourceColumnProfile,
    SourceProfileReceipt,
    SourceSnapshotManifest,
    SourceTableProfile,
    TableSnapshot,
    build_cross_source_snapshot_set,
)
from services.structured_data.snapshot_capture import (
    SnapshotCaptureError,
    capture_database_snapshot,
    list_source_connections,
)

ENV = "ORION_SYNTHTEST_SOURCE_URL"


class _Profiler:
    def profile(self, binding: Any) -> SourceProfileReceipt:
        return SourceProfileReceipt(
            project_id=binding.project_id,
            source_id=binding.source_id,
            engine=binding.engine,
            profiled_at=datetime(2026, 9, 24, tzinfo=UTC),
            database=binding.database,
            schema_fingerprint="sha256:" + "c" * 64,
            receipt_sha256="sha256:" + "d" * 64,
            tables=[
                SourceTableProfile(
                    table=table,
                    row_count=3,
                    schema_sha256="sha256:" + "e" * 64,
                    columns=[
                        SourceColumnProfile(name="id", data_type="integer", nullable=False, ordinal_position=1),
                        SourceColumnProfile(name="phone", data_type="text", nullable=True, ordinal_position=2),
                    ],
                )
                for table in binding.authorized_tables
            ],
        )


class _Hub:
    instances: list[_Hub] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.bindings: list[Any] = []
        _Hub.instances.append(self)

    def register_sources(self, bindings: list[Any]) -> list[Any]:
        self.bindings = bindings
        return bindings

    def profile_sources(self, bindings: list[Any]) -> list[Any]:
        return []

    def capture_and_promote(self, *, project_id, bindings, snapshot_version, dataset_type, production_evidence):
        binding = bindings[0]
        manifest = SourceSnapshotManifest(
            project_id=project_id,
            source_id=binding.source_id,
            engine=binding.engine,
            dataset_id="DS-" + hashlib.sha256(snapshot_version.encode()).hexdigest()[:24].upper(),
            snapshot_version=snapshot_version,
            captured_at=datetime.now(UTC),
            snapshot_complete=True,
            source_sha256="sha256:" + "a" * 64,
            pii_scope=binding.pii_scope,
            dataset_type=dataset_type,
            production_evidence=production_evidence,
            tables=[
                TableSnapshot(
                    table=table,
                    target_table=f"ms_{table}",
                    source_row_count=3,
                    snapshot_row_count=3,
                    schema_sha256="sha256:" + "b" * 64,
                    source_locator=f"pg://{table}@{snapshot_version}",
                )
                for table in binding.authorized_tables
            ],
        )
        return build_cross_source_snapshot_set(project_id=project_id, bindings=bindings, snapshots=[manifest])


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "postgresql://reader:secret-value@localhost/db")
    monkeypatch.setenv("ORION_SOURCE_DATA_URL", "postgresql://target/db")
    _Hub.instances.clear()


def _call(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "project_id": "ontology-project-synth",
        "source_id": "synth_mfg",
        "connection_env": ENV,
        "database": "orion_synth_mfg",
        "tables": ["equipment", "workorder"],
        "exclude_columns": ["equipment:phone"],
        "pii_scope": "NO_PERSONAL_DATA",
        "owner": "platform-test",
        "snapshot_version": "v1",
        "hub_factory": _Hub,
        "profiler": _Profiler(),
    }
    arguments.update(overrides)
    return capture_database_snapshot(**arguments)


def test_capture_applies_column_allowlist_and_never_returns_secret() -> None:
    receipt = _call()

    binding = _Hub.instances[0].bindings[0]
    assert binding.connection_ref == f"env://{ENV}"
    assert binding.authorized_columns == {"equipment": ["id"], "workorder": ["id", "phone"]}
    assert receipt["dataset_type"] == "TEST_ONLY"
    assert receipt["production_evidence"] is False
    assert len(receipt["dataset_ids"]) == 1
    assert "secret-value" not in repr(receipt)


def test_production_requires_explicit_basis() -> None:
    with pytest.raises(SnapshotCaptureError, match="production_evidence_basis"):
        _call(dataset_type="PRODUCTION")
    receipt = _call(dataset_type="PRODUCTION", production_evidence_basis="用户 2026-09-24 授权的合成验收数据")
    assert receipt["production_evidence"] is True
    with pytest.raises(SnapshotCaptureError, match="TEST_ONLY"):
        _call(production_evidence_basis="不应出现的依据说明")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"connection_env": "DATABASE_URL"}, "connection_env"),
        ({"connection_env": "ORION_MISSING_SOURCE_URL"}, "未配置"),
        ({"tables": []}, "tables"),
        ({"exclude_columns": ["supplier:phone"]}, "未授权表"),
        ({"exclude_columns": ["equipment:nope"]}, "不存在"),
        ({"exclude_columns": ["equipment:id", "equipment:phone"]}, "没有可导入字段"),
        ({"database": "db; drop"}, "database"),
    ],
)
def test_capture_fails_closed(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(SnapshotCaptureError, match=message):
        _call(**overrides)
    assert _Hub.instances == []


def test_connection_listing_exposes_names_only() -> None:
    result = list_source_connections()
    assert {"connection_ref": f"env://{ENV}", "connection_env": ENV} in result["connections"]
    assert "secret-value" not in repr(result)
    assert result["target_configured"] is True


def test_capture_reuses_chat2db_reference_without_connection_env() -> None:
    receipt = _call(connection_env=None, chat2db_datasource_id=23)
    assert _Hub.instances[0].bindings[0].connection_ref == "chat2db://community/23"
    assert receipt["provider"] == "chat2db"
    assert receipt["capture_limits"]["incremental_cdc"] is False
    assert receipt["capture_limits"]["page_size"] == 999


@pytest.mark.parametrize("overrides", [
    {"chat2db_datasource_id": 23}, {"connection_env": None},
    {"connection_env": None, "chat2db_datasource_id": 0},
    {"connection_env": None, "chat2db_datasource_id": True},
    {"connection_env": None, "chat2db_datasource_id": "23"},
])
def test_capture_provider_selection_is_explicit_and_exclusive(overrides) -> None:
    with pytest.raises(SnapshotCaptureError):
        _call(**overrides)
    assert _Hub.instances == []


def test_scope_preflight_runs_before_registration_and_after_column_exclusion():
    seen = []
    def preflight(binding, profile):
        assert not _Hub.instances
        seen.append(profile)
        if profile is not None:
            equipment = next(t for t in profile.tables if t.table == "equipment")
            assert [c.name for c in equipment.columns] == ["id"]
            assert binding.authorized_columns["equipment"] == ["id"]
    _call(scope_preflight=preflight)
    assert seen[0] is None and len(seen) == 2


def test_failed_scope_preflight_never_registers_or_captures():
    def preflight(*_):
        raise ValueError("scope rejected")
    with pytest.raises(ValueError, match="scope rejected"):
        _call(scope_preflight=preflight)
    assert not _Hub.instances
