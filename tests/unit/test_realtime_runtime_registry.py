from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from services.config import Settings
from services.realtime_qa import api as api_module
from services.realtime_qa import registration as registration_module
from services.realtime_qa import runtime as runtime_module
from services.realtime_qa.api import CoreRealtimeRuntimeProvider
from services.realtime_qa.binding import ReleaseBindingLoader
from services.realtime_qa.models import OntologyReleaseBinding
from services.realtime_qa.runtime import (
    RealtimeRuntimeRegistryError,
    RealtimeRuntimeUnavailable,
    build_realtime_runtime_registry,
)


def binding(project_id: str, digit: str) -> OntologyReleaseBinding:
    return OntologyReleaseBinding(
        project_id=project_id,
        release_version="1.0.0",
        release_fingerprint="sha256:" + digit * 64,
        ontology_iri=f"urn:orion:ontology:{project_id}",
        ontology_artifact="01-本体模型/ontology.ttl",
        mapping_artifact="05-运行时/mapping.obda",
        source_mapping_sha256="sha256:" + "2" * 64,
        database_access_mode="READ_ONLY",
        ontop_identity_query_artifact="05-运行时/queries/identity.rq",
        ontop_query_names={"orders_live"},
        ontop_query_artifacts={"orders_live": "05-运行时/queries/orders_live.rq"},
        artifact_checksums={
            "01-本体模型/ontology.ttl": "sha256:" + "3" * 64,
            "05-运行时/mapping.obda": "sha256:" + "4" * 64,
            "05-运行时/queries/identity.rq": "sha256:" + "5" * 64,
            "05-运行时/queries/orders_live.rq": "sha256:" + "6" * 64,
        },
        package_path=f"/verified/{project_id}",
        integrity_status="verified",
        ontop_deployment_id=f"ontop-{project_id}-1.0.0",
        published_at="2026-09-02T12:00:00Z",
    )


def write_registry(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("query_timeout", [None, 17.0])
def test_runtime_preserves_short_health_budget_for_business_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query_timeout: float | None,
) -> None:
    from services.ontop_client.client import OntopClient

    release = binding("enterprise-a", "a").model_copy(
        update={"reasoning_capabilities": {"example": {}}}
    )
    query_path = tmp_path / "orders_live.rq"
    query_path.write_text("SELECT ?count WHERE {}", encoding="utf-8")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/reason":
            return httpx.Response(200, json={"inferred_facts": [], "trace": []})
        return httpx.Response(200, json={
            "head": {"vars": ["count"]},
            "results": {"bindings": [{"count": {"value": "1"}}]},
        })

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)}
    ))
    monkeypatch.setattr(ReleaseBindingLoader, "load", lambda *args, **kwargs: release)
    monkeypatch.setattr(runtime_module, "verify_ontop_deployment_binding", lambda *args: {
        "endpoint": "http://ontop.test/sparql",
    })
    monkeypatch.setattr(ReleaseBindingLoader, "build_ontop_client", lambda *args, **kwargs: OntopClient(
        "http://ontop.test/sparql", timeout=kwargs["timeout"],
        allowed_queries={"orders_live"}, query_files={"orders_live": query_path},
        bound_release_fingerprint=release.release_fingerprint,
    ))

    def verify(_self, client, *_args):
        # Identity probes use the client's initial timeout, before business activation.
        assert client.timeout == 2.0
        client.select("orders_live")
        client._mark_runtime_verified({})

    monkeypatch.setattr(runtime_module.OntopDeploymentVerifier, "verify_endpoint", verify)
    kwargs = {} if query_timeout is None else {"query_timeout": query_timeout}
    runtime = runtime_module.build_realtime_evidence_runtime(
        project_dir=tmp_path, deployment_binding_path=tmp_path / "deployment.json",
        database_url="postgresql://unused", fuseki_url="http://fuseki.test/data",
        semantica_url="http://semantica.test", timeout=2.0, **kwargs,
    )
    assert all(request.extensions["timeout"]["read"] == 2.0 for request in requests)
    assert len(requests) == 2  # Ontop identity verification and Semantica health.
    requests.clear()
    assert runtime.ontop.health()["status"] == "healthy"
    assert runtime.fuseki.health()["status"] == "healthy"
    assert runtime.semantica.health()["status"] == "healthy"
    assert all(request.extensions["timeout"]["read"] == 2.0 for request in requests)
    requests.clear()
    assert runtime.ontop.select("orders_live") == [{"count": "1"}]
    assert runtime.fuseki.select("SELECT ?count WHERE {}") == [{"count": "1"}]
    assert runtime.semantica.run_forward(facts=[], rules=[])["inferred_facts"] == []
    expected = 30.0 if query_timeout is None else query_timeout
    assert len(requests) == 3
    assert all(request.extensions["timeout"]["read"] == expected for request in requests)


@pytest.mark.parametrize("field", ["timeout", "query_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("-inf"), float("nan"), None, True, "30"])
def test_runtime_rejects_invalid_budgets_before_loading_a_release(
    tmp_path: Path, field: str, value: object,
) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a finite positive number"):
        runtime_module.build_realtime_evidence_runtime(
            project_dir=tmp_path, deployment_binding_path=None,
            database_url="unused", fuseki_url="unused", **{field: value},
        )
    with pytest.raises(ValueError, match=f"{field} must be a finite positive number"):
        build_realtime_runtime_registry(
            registry_path=tmp_path / "missing.json", database_url="unused",
            fuseki_url="unused", **{field: value},
        )


def test_registry_loader_resolves_relative_paths_and_keeps_failures_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_registry(
        tmp_path / "runtimes.json",
        {
            "schema_version": 1,
            "default_project_id": "enterprise-a",
            "runtimes": [
                {
                    "project_id": "enterprise-a",
                    "project_dir": "projects/a",
                    "deployment_binding_path": "deployments/a.json",
                },
                {
                    "project_id": "enterprise-b",
                    "project_dir": "projects/b",
                    "deployment_binding_path": "deployments/b.json",
                },
            ],
        },
    )
    calls: list[tuple[Path, Path]] = []
    budgets: list[tuple[float, float]] = []

    def fake_build(**kwargs):
        project_dir = kwargs["project_dir"]
        deployment_path = kwargs["deployment_binding_path"]
        calls.append((project_dir, deployment_path))
        budgets.append((kwargs["timeout"], kwargs["query_timeout"]))
        if project_dir.name == "b":
            raise RuntimeError("endpoint marker mismatch")
        return SimpleNamespace(
            binding=binding("enterprise-a", "a"),
            runtime_verified=True,
            runtime_verification_error=None,
        )

    monkeypatch.setattr(runtime_module, "build_realtime_evidence_runtime", fake_build)

    registry = build_realtime_runtime_registry(
        registry_path=config_path,
        database_url="postgresql://unused",
        fuseki_url="http://127.0.0.1:3030/supply",
        timeout=2.0,
        query_timeout=17.0,
    )

    assert registry.resolve().binding.project_id == "enterprise-a"
    assert budgets == [(2.0, 17.0), (2.0, 17.0)]
    assert registry.resolve("enterprise-a").binding.release_fingerprint.endswith("a" * 64)
    assert calls[0] == (
        (tmp_path / "projects/a").resolve(),
        (tmp_path / "deployments/a.json").resolve(),
    )
    with pytest.raises(RealtimeRuntimeUnavailable, match="endpoint marker mismatch"):
        registry.resolve("enterprise-b")
    catalog = registry.catalog()
    assert [item.project_id for item in catalog.runtimes] == ["enterprise-a"]
    assert catalog.unavailable_projects == {
        "enterprise-b": "endpoint marker mismatch"
    }


def test_registry_loader_rejects_duplicate_projects(tmp_path: Path) -> None:
    config_path = write_registry(
        tmp_path / "runtimes.json",
        {
            "schema_version": 1,
            "runtimes": [
                {
                    "project_id": "enterprise-a",
                    "project_dir": "a",
                    "deployment_binding_path": "a.json",
                },
                {
                    "project_id": "enterprise-a",
                    "project_dir": "other-a",
                    "deployment_binding_path": "other-a.json",
                },
            ],
        },
    )

    with pytest.raises(RealtimeRuntimeRegistryError, match="duplicate enabled project_id"):
        build_realtime_runtime_registry(
            registry_path=config_path,
            database_url="postgresql://unused",
            fuseki_url="http://127.0.0.1:3030/supply",
        )


def test_registry_loader_rejects_a_package_under_the_wrong_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_registry(
        tmp_path / "runtimes.json",
        {
            "schema_version": 1,
            "runtimes": [
                {
                    "project_id": "enterprise-a",
                    "project_dir": "projects/a",
                    "deployment_binding_path": "deployments/a.json",
                }
            ],
        },
    )
    monkeypatch.setattr(
        runtime_module,
        "build_realtime_evidence_runtime",
        lambda **_kwargs: SimpleNamespace(
            binding=binding("enterprise-b", "b"),
            runtime_verified=True,
            runtime_verification_error=None,
        ),
    )

    registry = build_realtime_runtime_registry(
        registry_path=config_path,
        database_url="postgresql://unused",
        fuseki_url="http://127.0.0.1:3030/supply",
    )

    with pytest.raises(RealtimeRuntimeUnavailable, match="does not match"):
        registry.resolve("enterprise-a")


def test_registration_cli_upserts_projects_without_storing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "runtime-registry.json"

    def fake_load(
        _loader,
        project_dir: Path,
        **_kwargs,
    ) -> OntologyReleaseBinding:
        project_id = project_dir.name
        return binding(project_id, "a" if project_id == "enterprise-a" else "b")

    monkeypatch.setattr(registration_module.ReleaseBindingLoader, "load", fake_load)
    monkeypatch.setattr(
        registration_module,
        "verify_ontop_deployment_binding",
        lambda _binding, _path: {"endpoint": "http://127.0.0.1:18080/sparql"},
    )
    monkeypatch.setattr(
        registration_module,
        "_verify_runtime_activation",
        lambda **_kwargs: {
            "runtime_endpoint_verification": "VERIFIED",
            "runtime_loader_verification": "VERIFIED",
        },
    )

    first = registration_module.register_runtime(
        registry_path=registry_path,
        project_dir=tmp_path / "enterprise-a",
        deployment_binding_path=tmp_path / "a-deployment.json",
        set_default=True,
    )
    second = registration_module.register_runtime(
        registry_path=registry_path,
        project_dir=tmp_path / "enterprise-b",
        deployment_binding_path=tmp_path / "b-deployment.json",
    )

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert first["runtime_endpoint_verification"] == "VERIFIED"
    assert first["runtime_loader_verification"] == "VERIFIED"
    assert second["project_id"] == "enterprise-b"
    assert payload["default_project_id"] == "enterprise-a"
    assert [item["project_id"] for item in payload["runtimes"]] == [
        "enterprise-a",
        "enterprise-b",
    ]
    serialized = registry_path.read_text(encoding="utf-8")
    assert "password" not in serialized.lower()
    assert "jdbc" not in serialized.lower()


@pytest.mark.parametrize('endpoint_verified', [True, False])
def test_pre_runtime_registration_uses_same_gate_scope_for_actual_activation_loader(
    tmp_path, monkeypatch, endpoint_verified,
):
    from services.realtime_qa.binding import ReleaseBindingError

    candidate = binding('enterprise-candidate', 'c')
    loads = []

    def load(_self, _path, *, allow_pre_runtime=False):
        loads.append(allow_pre_runtime)
        if not allow_pre_runtime:
            raise ReleaseBindingError('S7 release gate has not passed')
        return candidate

    monkeypatch.setattr(ReleaseBindingLoader, 'load', load)
    monkeypatch.setattr(ReleaseBindingLoader, 'build_ontop_client', lambda *args, **kwargs: SimpleNamespace(
        runtime_verification_status='VERIFIED' if endpoint_verified else 'UNVERIFIED',
    ))
    for module in [registration_module, runtime_module]:
        monkeypatch.setattr(module, 'verify_ontop_deployment_binding', lambda *args: {
            'endpoint': 'http://example.invalid/sparql',
        })
    monkeypatch.setattr(runtime_module.OntopDeploymentVerifier, 'verify_endpoint', lambda *args: None)
    monkeypatch.setattr(runtime_module, 'PostgresDocumentCurrentRegistry', lambda *args: SimpleNamespace())
    monkeypatch.setattr(runtime_module, 'FusekiClient', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(runtime_module, 'HybridRealtimeEvidenceService', lambda *args: SimpleNamespace())
    registry = tmp_path / 'registry.json'
    original = {'schema_version': 1, 'default_project_id': 'existing', 'runtimes': [
        {'project_id': 'existing', 'project_dir': str(tmp_path / 'existing'), 'enabled': True},
    ]}
    registry.write_text(json.dumps(original))
    kwargs = dict(registry_path=registry, project_dir=tmp_path / 'enterprise-candidate',
                  deployment_binding_path=tmp_path / 'deployment.json', allow_pre_runtime=True)
    if endpoint_verified:
        result = registration_module.register_runtime(**kwargs)
        assert result['runtime_loader_verification'] == 'VERIFIED'
        assert json.loads(registry.read_text())['runtimes'][0]['project_id'] == candidate.project_id
        assert json.loads(registry.read_text())['default_project_id'] == 'existing'
    else:
        with pytest.raises(ValueError, match='activation loader'):
            registration_module.register_runtime(**kwargs)
        assert json.loads(registry.read_text()) == original
    assert loads == [True, True]
    # Public question-answering loader stays closed until the S7 gate passes.
    with pytest.raises(ReleaseBindingError, match='S7 release gate'):
        runtime_module.build_realtime_evidence_runtime(
            project_dir=tmp_path / 'enterprise-candidate', deployment_binding_path=None,
            database_url='unused', fuseki_url='http://example.invalid',
        )
    assert loads[-1] is False


def test_registration_keeps_previous_registry_when_candidate_loader_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "runtime-registry.json"
    original = {
        "schema_version": 1,
        "default_project_id": "enterprise-a",
        "runtimes": [
            {
                "project_id": "enterprise-a",
                "project_dir": str(tmp_path / "enterprise-a"),
                "deployment_binding_path": str(tmp_path / "a-deployment.json"),
                "enabled": True,
            }
        ],
    }
    registry_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(
        registration_module.ReleaseBindingLoader,
        "load",
        lambda _loader, _path, **_kwargs: binding("enterprise-b", "b"),
    )
    monkeypatch.setattr(
        registration_module,
        "verify_ontop_deployment_binding",
        lambda _binding, _path: {"endpoint": "http://127.0.0.1:18080/sparql"},
    )
    monkeypatch.setattr(
        registration_module,
        "_verify_runtime_activation",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("endpoint mismatch")),
    )

    with pytest.raises(ValueError, match="endpoint mismatch"):
        registration_module.register_runtime(
            registry_path=registry_path,
            project_dir=tmp_path / "enterprise-b",
            deployment_binding_path=tmp_path / "b-deployment.json",
        )

    assert json.loads(registry_path.read_text(encoding="utf-8")) == original


def test_legacy_runtime_rule_ids_are_hydrated_from_protected_rule_package(
    tmp_path: Path,
) -> None:
    rule_path = tmp_path / "rules/capability.json"
    rule_path.parent.mkdir(parents=True)
    rule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_name": "legacy_capability",
                "rules": [
                    {"rule_id": "RULE-LEGACY-001"},
                    {"rule_id": "RULE-LEGACY-002"},
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = {
        "schema_version": 3,
        "reasoning_capabilities": {
            "legacy_capability": {"rule_artifact": "rules/capability.json"}
        },
    }
    migrations: list[str] = []

    migrated = ReleaseBindingLoader()._migrate_legacy_reasoning_capabilities(
        runtime, tmp_path, migrations
    )

    assert migrated["legacy_capability"]["source_rule_ids"] == [
        "RULE-LEGACY-001",
        "RULE-LEGACY-002",
    ]
    assert "source_rule_ids" not in runtime["reasoning_capabilities"]["legacy_capability"]
    assert migrations == [
        "RUNTIME_V1_V3_HYDRATE_SOURCE_RULE_IDS_FROM_PROTECTED_PACKAGE"
    ]


def test_core_provider_hot_reloads_registry_and_preserves_last_good_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "runtime-registry.json"
    registry_path.write_text("{}\n", encoding="utf-8")
    runtime = SimpleNamespace(binding=binding("enterprise-a", "a"))
    verified_registry = SimpleNamespace(resolve=lambda project_id=None: runtime)
    monkeypatch.setattr(api_module, "build_realtime_runtime_registry", lambda **kwargs: verified_registry)
    provider = CoreRealtimeRuntimeProvider(Settings(realtime_runtime_registry_path=str(registry_path)))
    assert provider.registry() is verified_registry
    assert provider.resolve("enterprise-a") is runtime
    assert provider.last_error is None

    monkeypatch.setattr(
        api_module, "build_realtime_runtime_registry",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("tampered registry")),
    )
    provider.refresh(force=True)
    assert provider.registry() is verified_registry
    assert provider.resolve("enterprise-a") is runtime
    assert "tampered registry" in provider.last_error
