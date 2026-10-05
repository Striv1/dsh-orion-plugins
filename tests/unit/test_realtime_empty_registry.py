from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.config import Settings
from services.realtime_qa import registration as registration_module
from services.realtime_qa import runtime as runtime_module
from services.realtime_qa.api import CoreRealtimeRuntimeProvider, create_core_realtime_app
from services.realtime_qa.evidence_receipts import EvidenceReceiptStore
from services.realtime_qa.models import RealtimeRuntimeRegistryConfig
from services.realtime_qa.runtime import RealtimeRuntimeNotFound
from tests.unit.test_core_realtime_api import _Runtime
from tests.unit.test_realtime_runtime_registry import binding


def write_registry(path: Path, runtimes: list[dict], default: str | None = None) -> None:
    path.write_text(json.dumps({
        "schema_version": 1, "default_project_id": default, "runtimes": runtimes,
    }), encoding="utf-8")


def provider_for(path: Path) -> CoreRealtimeRuntimeProvider:
    return CoreRealtimeRuntimeProvider(Settings(
        realtime_runtime_registry_path=str(path), database_url="postgresql://unused",
        fuseki_url="http://fuseki.invalid/data", semantica_url="http://semantica.invalid",
    ))


@pytest.mark.parametrize("disabled", [False, True])
def test_empty_registry_has_a_readable_catalog_but_cannot_issue_binding_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool,
) -> None:
    path = tmp_path / "registry.json"
    entries = ([{"project_id": "enterprise-a", "project_dir": "unused", "enabled": False}]
               if disabled else [])
    write_registry(path, entries)

    def forbidden(**_kwargs):
        pytest.fail("an empty catalog must not construct or probe a business runtime")

    monkeypatch.setattr(runtime_module, "build_realtime_evidence_runtime", forbidden)
    provider = provider_for(path)
    receipts = EvidenceReceiptStore(tmp_path / "receipts")
    client = TestClient(create_core_realtime_app(provider, evidence_receipts=receipts))
    catalog = client.get("/ontology/realtime/runtimes")
    assert catalog.status_code == 200
    assert catalog.json() == {
        "default_project_id": None, "runtimes": [], "unavailable_projects": {},
    }
    health = client.get("/health").json()
    assert health["status"] == "DEGRADED"
    assert health["readiness_reason"] == "NO_PUBLISHED_RUNTIME"
    assert health["registered_runtime_count"] == health["runtime_verified_count"] == 0
    assert health["reload_error"] is None
    assert "error" not in health
    for params in [{}, {"project_id": "enterprise-a"}]:
        assert client.get("/ontology/realtime/binding", params=params).status_code == 404
    response = client.post("/ontology/realtime/session-answer", json={
        "session_id": "session-11111111-1111-1111-1111-111111111111",
        "expected_release": {
            "project_id": "enterprise-a", "release_version": "1.0.0",
            "release_fingerprint": "sha256:" + "a" * 64,
        },
        "answer_request": {
            "question": "当前正式版本有哪些依据？",
            "evidence_request": {
                "project_id": "enterprise-a", "query_id": "Q-EMPTY",
                "entity_iris": ["urn:orion:record:1"],
            },
        },
    })
    assert response.status_code == 404
    assert not receipts.root.exists()


@pytest.mark.parametrize("runtimes", [
    [],
    [{"project_id": "enterprise-a", "project_dir": "unused", "enabled": False}],
    [{"project_id": "enterprise-b", "project_dir": "unused", "enabled": True}],
])
def test_default_must_still_reference_an_enabled_registered_project(runtimes: list[dict]) -> None:
    with pytest.raises(ValidationError, match="default_project_id is not an enabled runtime"):
        RealtimeRuntimeRegistryConfig.model_validate({
            "schema_version": 1, "default_project_id": "enterprise-a", "runtimes": runtimes,
        })


@pytest.mark.parametrize("set_default", [False, True])
def test_first_verified_s7_registration_can_extend_a_preexisting_empty_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_default: bool,
) -> None:
    path = tmp_path / "registry.json"
    write_registry(path, [])
    candidate = binding("enterprise-a", "a")
    checks = []

    def load(_self, project_dir, *, allow_pre_runtime=False):
        assert project_dir == (tmp_path / "enterprise-a").resolve()
        assert allow_pre_runtime is False
        checks.append("approved-package")
        return candidate

    def deployment(release, deployment_path):
        assert release is candidate
        assert deployment_path == (tmp_path / "deployment.json").resolve()
        checks.append("deployment-contract")
        return {"endpoint": "http://ontop.invalid/sparql"}

    def activation(**kwargs):
        assert kwargs["binding"] is candidate
        assert kwargs["allow_pre_runtime"] is False
        checks.append("verified-activation")
        return {"runtime_endpoint_verification": "VERIFIED", "runtime_loader_verification": "VERIFIED"}

    monkeypatch.setattr(registration_module.ReleaseBindingLoader, "load", load)
    monkeypatch.setattr(registration_module, "verify_ontop_deployment_binding", deployment)
    monkeypatch.setattr(registration_module, "_verify_runtime_activation", activation)
    result = registration_module.register_runtime(
        registry_path=path, project_dir=tmp_path / "enterprise-a",
        deployment_binding_path=tmp_path / "deployment.json", set_default=set_default,
    )
    committed = RealtimeRuntimeRegistryConfig.model_validate_json(path.read_text())
    assert [item.project_id for item in committed.runtimes] == [candidate.project_id]
    assert committed.runtimes[0].enabled is True
    assert committed.default_project_id == (candidate.project_id if set_default else None)
    assert result["default_project_id"] == committed.default_project_id
    assert result["release_fingerprint"] == candidate.release_fingerprint
    assert checks == ["approved-package", "deployment-contract", "verified-activation"]


@pytest.mark.parametrize("failure", ["approval", "activation"])
def test_empty_config_is_preserved_when_first_registration_is_not_authorized_or_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    path = tmp_path / "registry.json"
    write_registry(path, [])
    original = path.read_bytes()
    candidate = binding("enterprise-a", "a")

    def load(*_args, **_kwargs):
        if failure == "approval":
            raise ValueError("S7 publication has not been approved")
        return candidate

    def activation(**_kwargs):
        raise ValueError("runtime activation loader did not verify the candidate release")

    monkeypatch.setattr(registration_module.ReleaseBindingLoader, "load", load)
    monkeypatch.setattr(registration_module, "verify_ontop_deployment_binding", lambda *_args: {})
    monkeypatch.setattr(registration_module, "_verify_runtime_activation", activation)
    with pytest.raises(ValueError, match="approved|activation loader"):
        registration_module.register_runtime(
            registry_path=path, project_dir=tmp_path / "enterprise-a",
            deployment_binding_path=tmp_path / "deployment.json",
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize("disabled", [False, True])
def test_removing_all_runtimes_replaces_the_cache_and_becomes_the_last_verified_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool,
) -> None:
    path = tmp_path / "registry.json"
    runtime = _Runtime()
    entry = {"project_id": runtime.binding.project_id, "project_dir": str(tmp_path / "project")}
    builds = []

    def build(**kwargs):
        builds.append(kwargs["project_dir"])
        return runtime

    monkeypatch.setattr(runtime_module, "build_realtime_evidence_runtime", build)
    write_registry(path, [entry], default=runtime.binding.project_id)
    provider = provider_for(path)
    previous = provider.registry()
    assert provider.resolve(None) is runtime
    write_registry(path, [{**entry, "enabled": False}] if disabled else [])
    provider.refresh(force=True)
    cleared = provider.registry()
    assert cleared is not previous
    assert cleared.catalog().runtimes == []
    assert provider.last_error is None
    assert provider.health()["readiness_reason"] == "NO_PUBLISHED_RUNTIME"
    assert len(builds) == 1
    for project_id in [None, runtime.binding.project_id]:
        with pytest.raises(RealtimeRuntimeNotFound):
            provider.resolve(project_id)
    # A later corrupt replacement keeps the empty verified version, not the
    # older populated registry and its retired project.
    write_registry(path, [], default=runtime.binding.project_id)
    provider.refresh(force=True)
    assert provider.registry() is cleared
    assert "default_project_id" in provider.last_error
    with pytest.raises(RealtimeRuntimeNotFound):
        provider.resolve(runtime.binding.project_id)


def test_a_broken_enabled_runtime_is_not_reported_as_an_expected_empty_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "registry.json"
    write_registry(path, [{"project_id": "enterprise-a", "project_dir": "unused"}])

    def rejected(**_kwargs):
        raise ValueError("S7 publication has not been approved")

    monkeypatch.setattr(runtime_module, "build_realtime_evidence_runtime", rejected)
    provider = provider_for(path)
    health = provider.health()
    assert health["status"] == "DEGRADED"
    assert health["readiness_reason"] is None
    assert health["unavailable_projects"] == {"enterprise-a": "S7 publication has not been approved"}
