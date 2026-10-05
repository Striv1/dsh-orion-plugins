from types import SimpleNamespace

import pytest

from services.realtime_qa import registration, runtime
from services.realtime_qa.deployment_automation import S7DeploymentAutomation
from tests.unit.test_realtime_runtime_registry import binding


def prepare(monkeypatch, *, rules=True):
    release = binding("documents", "a").model_copy(update={
        "structured_query_enabled": False,
        "reasoning_capabilities": {"review": {}} if rules else {},
    })
    loaded = SimpleNamespace(binding=release, runtime_verified=True, reasoning_runtime_verified=True)
    monkeypatch.setattr(registration.ReleaseBindingLoader, "load", lambda *a, **kw: release)
    monkeypatch.setattr(runtime, "build_realtime_evidence_runtime", lambda **kw: loaded)
    return release, loaded


def test_document_registration_executes_cases_before_registry_commit(tmp_path, monkeypatch):
    release, _ = prepare(monkeypatch)
    registry = tmp_path / "registry.json"
    receipt = {"status": "PASSED", "case_count": 1, "results": [{"cq": "passed"}]}
    calls = []

    def verify(candidate, endpoint, semantica_url, **kwargs):
        assert candidate is release and endpoint == ""
        assert not registry.exists()
        calls.append(candidate.release_fingerprint)
        return receipt

    monkeypatch.setattr(S7DeploymentAutomation, "_verify_reasoning_validation_cases", staticmethod(verify))
    result = registration.register_runtime(registry_path=registry, project_dir=tmp_path)
    assert calls == [release.release_fingerprint]
    assert result["reasoning_validation"] == receipt
    assert registry.exists()


@pytest.mark.parametrize("failure", ["exception", "failed_status"])
def test_health_success_cannot_register_failed_document_rules_or_cqs(tmp_path, monkeypatch, failure):
    prepare(monkeypatch)
    registry = tmp_path / "registry.json"
    registry.write_text('{"existing":"must survive"}')

    def verify(*args, **kwargs):
        if failure == "exception":
            raise ValueError("CQ row assertion failed")
        return {"status": "FAILED"}

    monkeypatch.setattr(S7DeploymentAutomation, "_verify_reasoning_validation_cases", staticmethod(verify))
    with pytest.raises(ValueError):
        registration.register_runtime(registry_path=registry, project_dir=tmp_path)
    assert registry.read_text() == '{"existing":"must survive"}'


def test_no_rules_does_not_add_reasoning_requirement(tmp_path, monkeypatch):
    prepare(monkeypatch, rules=False)
    monkeypatch.setattr(S7DeploymentAutomation, "_verify_reasoning_validation_cases", staticmethod(
        lambda *a, **kw: pytest.fail("no rule capabilities must not trigger rules")
    ))
    result = registration.register_runtime(registry_path=tmp_path / "registry.json", project_dir=tmp_path)
    assert "reasoning_validation" not in result


def test_activation_rejects_changed_release_before_cases(tmp_path, monkeypatch):
    _, loaded = prepare(monkeypatch)
    loaded.binding = loaded.binding.model_copy(update={"release_fingerprint": "sha256:" + "b" * 64})
    with pytest.raises(ValueError, match="candidate release"):
        registration.register_runtime(registry_path=tmp_path / "registry.json", project_dir=tmp_path)
    assert not (tmp_path / "registry.json").exists()
