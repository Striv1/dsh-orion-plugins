from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from services.realtime_qa.api import SERVICE_ID, create_core_realtime_app
from services.realtime_qa.models import (
    EvidenceBundle,
    EvidenceRecord,
    OntologyReleaseBinding,
    RealtimeEvidenceRequest,
    RealtimeRuntimeCatalog,
    SourceStatus,
)
from services.realtime_qa.runtime import RealtimeRuntimeRegistry, runtime_binding


def _binding() -> OntologyReleaseBinding:
    return OntologyReleaseBinding(
        project_id="core-api-project",
        release_version="1.0.0",
        release_fingerprint="sha256:" + "1" * 64,
        ontology_iri="urn:orion:core-api-project",
        ontology_artifact="01-本体模型/ontology.ttl",
        runtime_mode="DOCUMENT_ONLY",
        structured_query_enabled=False,
        database_access_mode="READ_ONLY",
        artifact_checksums={"01-本体模型/ontology.ttl": "sha256:" + "2" * 64},
        package_path="/verified/core-api-project",
        integrity_status="verified",
        published_at=datetime(2026, 9, 3, tzinfo=UTC),
    )


class _Runtime:
    def __init__(self) -> None:
        self.binding = _binding()
        self.runtime_verified = True
        self.runtime_verification_error = None
        self.reasoning_runtime_verified = False
        self.reasoning_runtime_verification_error = None
        self.current_registry = SimpleNamespace(count_current=lambda project_id: 0)

    def collect(self, request: RealtimeEvidenceRequest) -> EvidenceBundle:
        return EvidenceBundle(
            query_id=request.query_id,
            mode="documents",
            release=self.binding,
            generated_at=datetime(2026, 9, 3, tzinfo=UTC),
            complete=True,
            evidence=[
                EvidenceRecord(
                    evidence_id="EVD-CORE-001",
                    source_kind="ontology_release",
                    source_ref="orion:core-api-project@1.0.0",
                    observed_at=datetime(2026, 9, 3, tzinfo=UTC),
                    payload={"artifact_verification": "verified"},
                )
            ],
            source_status={
                "structured_db": SourceStatus(
                    source_kind="structured_db",
                    status="not_requested",
                    source_ref="ontop",
                    queried_at=None,
                    version="1.0.0",
                ),
                "document": SourceStatus(
                    source_kind="document",
                    status="empty",
                    source_ref="fuseki",
                    queried_at=datetime(2026, 9, 3, tzinfo=UTC),
                    version=None,
                ),
            },
        )


class _Provider:
    def __init__(self) -> None:
        self.runtime = _Runtime()
        self._registry = RealtimeRuntimeRegistry.from_single(self.runtime)  # type: ignore[arg-type]

    def resolve(self, project_id: str | None):
        return self._registry.resolve(project_id)

    def catalog(self) -> RealtimeRuntimeCatalog:
        return self._registry.catalog()

    def health(self):
        return {
            "service": SERVICE_ID,
            "status": "READY",
            "registered_runtime_count": 1,
            "runtime_verified_count": 1,
            "reasoning_runtime_declared_count": 0,
            "reasoning_runtime_verified_count": 0,
        }


def test_core_realtime_api_is_supplyguard_independent_and_release_bound() -> None:
    provider = _Provider()
    client = TestClient(create_core_realtime_app(provider))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == SERVICE_ID
    assert health.json()["status"] == "READY"
    assert health.json()["code_fingerprint"].startswith("sha256:")

    binding = client.get(
        "/ontology/realtime/binding", params={"project_id": "core-api-project"}
    )
    assert binding.status_code == 200
    assert binding.json() == runtime_binding(provider.runtime).model_dump(mode="json")

    assert client.get("/project-overview").status_code == 404
    assert client.post("/qa", json={"question": "供应链入口不应存在"}).status_code == 404


def test_core_realtime_session_rejects_release_mismatch() -> None:
    client = TestClient(create_core_realtime_app(_Provider()))
    response = client.post(
        "/ontology/realtime/session-answer",
        json={
            "session_id": "session-11111111-1111-1111-1111-111111111111",
            "expected_release": {
                "project_id": "core-api-project",
                "release_version": "1.0.0",
                "release_fingerprint": "sha256:" + "f" * 64,
            },
            "answer_request": {
                "question": "当前版本能支持什么结论？",
                "evidence_request": {
                    "project_id": "core-api-project",
                    "query_id": "Q-CORE-MISMATCH",
                    "entity_iris": ["urn:orion:entity:1"],
                },
            },
        },
    )

    assert response.status_code == 409
