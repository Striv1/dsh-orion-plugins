from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.register_wren_source import authorized_registration
from services.structured_data import source_registration as registry


class Cursor:
    def __init__(self, *, privilege=False, deferred=False, validated=True, relation="r"):
        self.privilege, self.deferred, self.validated, self.relation = privilege, deferred, validated, relation
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if "SELECT relkind" in sql:
            self.result = [(self.relation,)]
        elif "information_schema.columns" in sql:
            self.result = [("id", "integer", "NO", "pg_catalog", "int4", None, None, None, 32, 2, 0, None, "NEVER", None),
                ("amount", "numeric", "YES", "pg_catalog", "numeric", None, None, None, 18, 10, 2, None, "NEVER", None)]
        elif "FROM pg_catalog.pg_constraint" in sql:
            self.result = [("orders_pkey", "p", ["id"], None, None, [None], self.validated, self.deferred)]
        elif "current_database()" in sql:
            self.result = [("erp", False, "on", "a" * 32)]
        elif "FROM pg_catalog.pg_roles" in sql:
            self.result = [(self.privilege, False, False, False, False)]
        elif "has_table_privilege" in sql:
            self.result = [(True, False)]
        elif "system_identifier" in sql:
            self.result = [("123456789",)]
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


def arguments(tmp_path):
    return {"project_id": "project-erp", "source_id": "sales-source", "connection_env": "ORION_ERP_SOURCE_URL",
        "database": "erp", "authorized_tables": ["public.orders"], "owner": "data-owner",
        "pii_scope": "none", "registry_root": tmp_path}


def mock_database(monkeypatch, cursor):
    import psycopg

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return cursor

    monkeypatch.setenv("ORION_ERP_SOURCE_URL", "postgresql://reader:never-print-me@127.0.0.1:5432/erp")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Connection())


def test_register_read_load_scope_and_private_storage(tmp_path, monkeypatch):
    cursor = Cursor()
    mock_database(monkeypatch, cursor)
    record = registry.register_service_source(**arguments(tmp_path))
    assert record == registry.load_service_source("project-erp", "sales-source", registry_root=tmp_path)
    assert record["source_tables"][0]["primary_key"] == ["id"]
    assert record["provider"] == "EXPLICIT_SERVICE" and record["chat2db_binding_status"] == "NOT_REQUESTED"
    path = tmp_path / "project-erp/sales-source.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "never-print-me" not in path.read_text()
    summary = registry.list_service_sources("project-erp", registry_root=tmp_path)[0]
    assert "connection_env" not in summary and "primary_connection_env" not in summary
    assert all("count(" not in sql.lower() for sql, _ in cursor.calls)
    assert cursor.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


def test_registry_missing_is_distinct_from_corrupt_and_never_falls_back(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        registry.load_service_source("project-erp", "sales-source", registry_root=tmp_path)
    mock_database(monkeypatch, Cursor())
    registry.register_service_source(**arguments(tmp_path))
    path = tmp_path / "project-erp/sales-source.json"
    data = json.loads(path.read_text())
    data["database"] = "other"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="完整性"):
        registry.load_service_source("project-erp", "sales-source", registry_root=tmp_path)
    path.write_text("[]")
    with pytest.raises(ValueError):
        registry.load_service_source("project-erp", "sales-source", registry_root=tmp_path)


def test_registry_symlink_and_cross_project_rejected(tmp_path, monkeypatch):
    mock_database(monkeypatch, Cursor())
    registry.register_service_source(**arguments(tmp_path))
    (tmp_path / "other-project").symlink_to(tmp_path / "project-erp", target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        registry.load_service_source("other-project", "sales-source", registry_root=tmp_path)
    with pytest.raises(ValueError):
        registry.load_service_source("../project-erp", "sales-source", registry_root=tmp_path)


@pytest.mark.parametrize("options", [{"deferred": True}, {"validated": False}, {"relation": "v"}])
def test_untrusted_catalog_fails_before_recording(options, tmp_path, monkeypatch):
    mock_database(monkeypatch, Cursor(**options))
    with pytest.raises(ValueError):
        registry.register_service_source(**arguments(tmp_path))
    assert not list(tmp_path.rglob("*.json"))


def test_high_privilege_source_rejected_without_permission_changes(tmp_path, monkeypatch):
    cursor = Cursor(privilege=True)
    mock_database(monkeypatch, cursor)
    with pytest.raises(ValueError, match="高权限"):
        registry.register_service_source(**arguments(tmp_path))
    assert not any("GRANT " in sql or "ALTER " in sql for sql, _ in cursor.calls)


def test_explicit_column_scope_and_catalog_hash():
    table = registry.read_postgres_source_contract(Cursor(), source_id="sales-source",
        authorized_tables=["public.orders"], authorized_columns={"public.orders": ["amount"]})[0]
    assert table["columns"] == ["amount"] and table["primary_key"] == []
    changed = deepcopy(table)
    changed["nullable"]["amount"] = False
    assert registry.schema_contract_signature(changed) != table["schema_signature"]
    for key, value in (("numeric_precision", 20), ("numeric_scale", 4), ("character_maximum_length", 32),
                       ("generation_expression", "amount * 2")):
        changed = deepcopy(table)
        changed["column_details"]["amount"][key] = value
        assert registry.schema_contract_signature(changed) != table["schema_signature"]
    for columns in ({"public.orders": ["missing"]}, {"public.other": ["id"]}, {"public.orders": []}):
        with pytest.raises(ValueError):
            registry.read_postgres_source_contract(Cursor(), source_id="sales-source",
                authorized_tables=["public.orders"], authorized_columns=columns)


def test_connection_identity_tracks_node_principal_not_password(monkeypatch):
    monkeypatch.setenv("ORION_ERP_SOURCE_URL", "postgresql://reader:a@LOCALHOST/erp")
    original = registry.connection_identity_sha256("ORION_ERP_SOURCE_URL")
    monkeypatch.setenv("ORION_ERP_SOURCE_URL", "postgresql://reader:b@localhost:5432/erp")
    assert registry.connection_identity_sha256("ORION_ERP_SOURCE_URL") == original
    monkeypatch.setenv("ORION_ERP_SOURCE_URL", "postgresql://reader:b@other:5432/erp")
    assert registry.connection_identity_sha256("ORION_ERP_SOURCE_URL") != original


@pytest.mark.parametrize("identity, expected", [("a" * 32, "CHAT2DB_VERIFIED"), ("b" * 32, None)])
def test_chat2db_mismatch_is_not_hidden_as_unavailable(identity, expected, tmp_path, monkeypatch):
    from services.structured_data.chat2db_source import Chat2DBCLI

    mock_database(monkeypatch, Cursor())
    monkeypatch.setattr(Chat2DBCLI, "database_identity", lambda *a: {"database_identity": identity})
    args = arguments(tmp_path) | {"chat2db_datasource_id": 23}
    if expected:
        assert registry.register_service_source(**args)["provider"] == expected
    else:
        with pytest.raises(ValueError, match="不是同一来源"):
            registry.register_service_source(**args)
        assert not list(tmp_path.rglob("*.json"))


def test_module_loads_by_fixed_path_without_application_imports():
    spec = importlib.util.spec_from_file_location("isolated_source_catalog", Path(registry.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.read_postgres_source_contract)


def approved():
    return SimpleNamespace(project_id="project-erp", release_fingerprint="sha256:approved", source_bindings={
        "sales-source": {"project_id": "project-erp", "source_id": "sales-source", "status": "ACTIVE",
        "access_mode": "READ_ONLY", "database": "erp", "schemas": ["public"], "authorized_tables": ["orders"],
        "authorized_columns": {"orders": ["id", "amount"]}, "owner": "owner", "pii_scope": "none"}})


def config():
    return {"project_id": "project-erp", "source_id": "sales-source", "database": "erp",
        "connection_env": "ORION_ERP_SOURCE_URL", "authorized_tables": ["public.orders"],
        "authorized_columns": {"public.orders": ["id", "amount"]}}


def test_admin_registration_preserves_published_scope_and_responsibility():
    value = authorized_registration(config(), approved(), "sha256:approved")
    assert value["owner"] == "owner" and value["dataset_type"] == "PRODUCTION"


@pytest.mark.parametrize("update", [{"password": "secret"}, {"source_id": "other"}, {"database": "other"},
    {"authorized_tables": ["private.orders"]}, {"authorized_columns": {"public.orders": ["secret"]}}])
def test_admin_scope_expansion_or_credentials_rejected(update):
    with pytest.raises(ValueError):
        authorized_registration(config() | update, approved(), "sha256:approved")


def test_admin_release_drift_rejected():
    with pytest.raises(ValueError, match="指纹"):
        authorized_registration(config(), approved(), "sha256:old")


def test_admin_ambiguous_unqualified_published_table_rejected():
    binding = approved()
    binding.source_bindings["sales-source"]["schemas"] = ["public", "private"]
    with pytest.raises(ValueError, match="歧义"):
        authorized_registration(config(), binding, "sha256:approved")
