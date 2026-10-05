from types import SimpleNamespace

import pytest

from services.realtime_qa.live_analytics import resolve_analysis_project


def fixture(monkeypatch):
    published = {"project_id": "test-project", "source_id": "orders-src", "status": "ACTIVE",
                 "access_mode": "READ_ONLY", "database": "sales", "schemas": ["public"],
                 "authorized_tables": ["orders"], "authorized_columns": {"orders": ["id", "amount"]}}
    binding = SimpleNamespace(project_id="test-project", source_bindings={"orders-src": published})
    descriptor = {"source_tables": [{"source_id": "orders-src", "source_table": "orders", "columns": ["id", "amount"]}]}
    record = {"state": "ACTIVE", "database": "sales", "connection_env": "ORION_TEST_SOURCE_URL",
              "source_tables": [{"source_table": "orders", "qualified_source_table": "public.orders", "physical_schema": "public"}]}
    monkeypatch.setattr("services.structured_data.source_registration.load_service_source", lambda *_: record)
    monkeypatch.setattr("services.realtime_qa.wren_project.build_live_project", lambda *args: {"execution_scope": "LIVE_SOURCE_DATABASE"})
    return binding, descriptor, record


def test_auto_uses_registered_live_scope_and_explicit_snapshot_stays_snapshot(monkeypatch):
    binding, descriptor, record = fixture(monkeypatch)
    live = resolve_analysis_project(binding, descriptor)
    assert live["execution_scope"] == "LIVE_SOURCE_DATABASE"
    assert live["_registrations"]["orders-src"] is record
    assert resolve_analysis_project(binding, descriptor, "SNAPSHOT") is descriptor


@pytest.mark.parametrize("defect", ["database", "status", "scope", "ambiguous", "schema", "missing_source"])
def test_live_intersection_and_identity_fail_closed(monkeypatch, defect):
    binding, descriptor, record = fixture(monkeypatch)
    if defect == "database":
        record["database"] = "other"
    elif defect == "status":
        record["state"] = "DISABLED"
    elif defect == "scope":
        descriptor["source_tables"][0]["columns"].append("secret")
    elif defect == "ambiguous":
        record["source_tables"].append(dict(record["source_tables"][0], qualified_source_table="other.orders"))
    elif defect == "schema":
        record["source_tables"][0]["physical_schema"] = "other"
    else:
        descriptor["source_tables"].append({"source_id": None})
    with pytest.raises(ValueError):
        resolve_analysis_project(binding, descriptor)


def test_missing_registration_and_corruption_have_different_behavior(monkeypatch):
    binding, descriptor, _ = fixture(monkeypatch)
    def missing(*_):
        raise FileNotFoundError
    monkeypatch.setattr("services.structured_data.source_registration.load_service_source", missing)
    assert resolve_analysis_project(binding, descriptor) is descriptor
    with pytest.raises(ValueError, match="最新"):
        resolve_analysis_project(binding, descriptor, "LIVE")
    def corrupt(*_):
        raise ValueError("invalid checksum")
    monkeypatch.setattr("services.structured_data.source_registration.load_service_source", corrupt)
    with pytest.raises(ValueError, match="checksum"):
        resolve_analysis_project(binding, descriptor)
