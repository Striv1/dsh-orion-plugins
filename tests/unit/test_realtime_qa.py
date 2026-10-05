from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.ontop_client.client import OntopClient, QueryTemplateError
from services.realtime_qa.binding import (
    OntopDeploymentVerifier,
    OntopRuntimeVerificationError,
    ReleaseBindingError,
    ReleaseBindingLoader,
)
from services.realtime_qa.deployment import (
    OntopDeploymentContractError,
    render_ontop_release_deployment,
    verify_ontop_deployment_binding,
)
from services.realtime_qa.document_index import EV, S0IncrementalDocumentIndexer
from services.realtime_qa.incremental_pipeline import (
    DocumentCurrentRegistry,
    DocumentPromotionConflict,
    S0IncrementalPipeline,
)
from services.realtime_qa.models import (
    CurrentDocumentPointer,
    DocumentPageEvidence,
    DocumentQueryRequest,
    OntologyReleaseBinding,
    OriginalSnapshot,
    RealtimeEvidenceRequest,
    ReasoningQueryRequest,
    S0DocumentVersion,
    StructuredQueryRequest,
)
from services.realtime_qa.s0_adapter import S0BundleAdapter, S0RealtimeAdapterError
from services.realtime_qa.service import HybridRealtimeEvidenceService

NOW = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)


def release_binding() -> OntologyReleaseBinding:
    fingerprint = "sha256:" + "1" * 64
    return OntologyReleaseBinding(
        project_id="supply-chain-live",
        release_version="1.0.0",
        release_fingerprint=fingerprint,
        ontology_iri="https://example.com/ontology/supply-chain",
        ontology_artifact="05-ontology-build/ontology.ttl",
        mapping_artifact="ontop/supply-chain.obda",
        source_mapping_sha256="sha256:" + "5" * 64,
        database_access_mode="READ_ONLY",
        ontop_identity_query_artifact=("05-运行时/queries/orion_deployment_identity.rq"),
        ontop_query_names={"supply_order_context"},
        ontop_query_artifacts={"supply_order_context": "05-运行时/queries/supply_order_context.rq"},
        document_query_capabilities=[
            "current_full_text_search",
            "reviewed_entity_evidence",
        ],
        artifact_checksums={
            "05-ontology-build/ontology.ttl": "sha256:" + "2" * 64,
            "ontop/supply-chain.obda": "sha256:" + "3" * 64,
            "05-运行时/queries/supply_order_context.rq": "sha256:" + "4" * 64,
            "05-运行时/queries/orion_deployment_identity.rq": ("sha256:" + "6" * 64),
        },
        package_path="/verified/package",
        integrity_status="verified",
        ontop_deployment_id="ontop-supply-chain-live-1.0.0",
        published_at=NOW,
    )


class FakeOntop:
    allowed_queries = frozenset({"supply_order_context"})

    def __init__(
        self,
        error: OSError | None = None,
        *,
        runtime_verified: bool = True,
    ) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.bound_release_fingerprint = release_binding().release_fingerprint
        self.runtime_verification_status = (
            "VERIFIED" if runtime_verified else "NOT_RUNTIME_VERIFIED"
        )

    def select(self, name: str, **parameters: str) -> list[dict[str, Any]]:
        self.calls.append((name, parameters))
        if self.error:
            raise self.error
        return [{"order": parameters["purchase_order_code"], "status": "DELAYED"}]


class FakeFuseki:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        rows_by_entity: dict[str, list[dict[str, Any]]] | None = None,
        errors: dict[str, OSError] | None = None,
    ) -> None:
        self.rows = rows or []
        self.rows_by_entity = rows_by_entity or {}
        self.errors = errors or {}
        self.puts: list[tuple[str, Any]] = []

    def put_graph(self, graph_uri: str, graph: Any) -> None:
        self.puts.append((graph_uri, graph))

    def documents_for_entity(self, entity_iri: str) -> list[dict[str, Any]]:
        if entity_iri in self.errors:
            raise self.errors[entity_iri]
        return self.rows_by_entity.get(entity_iri, self.rows)

    def search_documents(
        self,
        project_id: str,
        text: str,
        *,
        document_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        selected = self.rows
        if document_ids:
            selected = [row for row in selected if row.get("document_id") in document_ids]
        return selected[:limit]


class FakeSemantica:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, list[str]]] = []

    def run_forward(self, *, facts: list[str], rules: list[str]) -> dict[str, Any]:
        self.calls.append({"facts": facts, "rules": rules})
        if self.error:
            raise self.error
        return {
            "inferred_facts": ["NeedsExpediting(PO-202608-017)"],
            "rules_fired": 1,
            "added_edges": 0,
            "updated_properties": 0,
            "mutated": False,
            "trace": [
                {
                    "rule_id": "rule_1",
                    "rule_text": rules[0],
                    "premises": ["DelayedOrder(PO-202608-017)"],
                    "conclusion": "NeedsExpediting(PO-202608-017)",
                    "confidence": 1.0,
                }
            ],
            "warnings": [],
        }


def reasoning_binding() -> OntologyReleaseBinding:
    capability = {
        "description_zh": "根据延期订单事实推出需催交订单",
        "engine": "SEMANTICA_FORWARD",
        "evidence_query": "supply_order_context",
        "execution_scope": "FULL_QUERY_RESULT",
        "fact_bindings": [
            {
                "predicate": "DelayedOrder",
                "arguments": [{"field": "order"}],
                "when": {"field": "status", "equals": "DELAYED"},
            }
        ],
                "result_predicates": ["NeedsExpediting"],
                "source_rule_ids": ["RULE-ORDER-EXPEDITE-001"],
        "question_examples": ["哪些订单需要催交？"],
        "ontology_terms": {
            "DelayedOrder": "https://example.com/ontology/supply-chain/DelayedOrder",
            "NeedsExpediting": "https://example.com/ontology/supply-chain/NeedsExpediting",
        },
        "runtime_validation": {
            "parameters": {"purchase_order_code": "PO-202608-017"},
            "min_input_facts": 1,
            "min_result_facts": 1,
            "require_rules_fired": True,
        },
        "rule_artifact": "05-运行时/rules/order_expediting.json",
        "rule_sha256": "sha256:" + "7" * 64,
    }
    rules = {
        "schema_version": 1,
        "capability_name": "order_expediting",
        "rules": [
            {
                "rule_id": "RULE-ORDER-EXPEDITE-001",
                "description_zh": "延期订单需要催交",
                "expression": "IF DelayedOrder(?order) THEN NeedsExpediting(?order)",
                "confidence": 0.97,
            }
        ],
    }
    return release_binding().model_copy(
        update={
            "reasoning_capabilities": {"order_expediting": capability},
            "reasoning_rule_packages": {"order_expediting": rules},
        }
    )


def document_reasoning_binding() -> OntologyReleaseBinding:
    binding = reasoning_binding()
    capability = dict(binding.reasoning_capabilities["order_expediting"])
    capability["evidence_query"] = "document_order_facts"
    capability["fact_bindings"] = [
        {
            "predicate": "DelayedOrder",
            "arguments": [{"field": 0}],
        }
    ]
    return binding.model_copy(
        update={
            "runtime_mode": "DOCUMENT_ONLY",
            "structured_query_enabled": False,
            "mapping_artifact": None,
            "source_mapping_sha256": None,
            "ontop_identity_query_artifact": None,
            "ontop_query_names": frozenset(),
            "ontop_query_artifacts": {},
            "ontop_deployment_id": None,
            "reasoning_capabilities": {"order_expediting": capability},
            "document_fact_queries": {
                "document_order_facts": {
                    "fact_source": "document_evidence",
                    "facts": [
                        {
                            "fact": "DelayedOrder(PO-202608-017)",
                            "evidence_id": "EVD-001",
                            "document_id": "DOC-001",
                            "source_locator": "Word 段落 3",
                            "document_version": "sha256:" + "8" * 64,
                            "evidence_sha256": "sha256:" + "9" * 64,
                        }
                    ],
                }
            },
        }
    )


class FakeSnapshotStore:
    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path

    def snapshot(self, path: Path, file_sha256: str) -> OriginalSnapshot:
        return OriginalSnapshot(
            backend="minio",
            bucket="evidence",
            object_key=f"sha256/{file_sha256}",
            source_uri=f"minio://evidence/sha256/{file_sha256}",
            file_sha256=file_sha256,
            size_bytes=path.stat().st_size,
        )

    @contextmanager
    def materialize(self, snapshot: OriginalSnapshot):
        yield self.source_path


def checksum(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def build_verified_release(tmp_path: Path) -> tuple[Path, Path]:
    release_dir = tmp_path / "07-release"
    release_dir.mkdir()
    package_dir = release_dir / "package-1.0.0"
    artifacts = {
        "01-本体模型/ontology.ttl": (
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "<https://example.com/ontology/supply-chain/DelayedOrder> a owl:Class .\n"
            "<https://example.com/ontology/supply-chain/NeedsExpediting> a owl:Class .\n"
        ),
        "04-发布信息/release-snapshot.json": json.dumps({"integrity_status": "PASSED"}),
        "05-运行时/mapping.obda": "[PrefixDeclaration]\n: http://example.com/",
        "05-运行时/queries/supply_order_context.rq": "SELECT * WHERE { ?s ?p ?o }",
        "05-运行时/queries/orion_deployment_identity.rq": (
            "SELECT ?deployment_id ?source_mapping_sha256 ?access_mode WHERE { ?s ?p ?o }"
        ),
        "05-运行时/realtime-runtime.json": json.dumps(
            {
                "project_id": "supply-chain-live",
                "release_version": "1.0.0",
                "ontology_iri": "https://example.com/ontology/supply-chain",
                "ontology_artifact": "01-本体模型/ontology.ttl",
                "mapping_artifact": "05-运行时/mapping.obda",
                "source_mapping_sha256": "sha256:" + "5" * 64,
                "database_access_mode": "READ_ONLY",
                "ontop_deployment_id": "ontop-supply-chain-live-1.0.0",
                "identity_query_artifact": ("05-运行时/queries/orion_deployment_identity.rq"),
                "ontop_queries": {
                    "supply_order_context": ("05-运行时/queries/supply_order_context.rq")
                },
            }
        ),
    }
    for relative, content in artifacts.items():
        path = package_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = {
        "project_id": "supply-chain-live",
        "release_version": "1.0.0",
        "approval_decision": "APPROVED",
        "files": [
            {"path": relative, "sha256": checksum(package_dir / relative)} for relative in artifacts
        ],
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (release_dir / "gate-results.json").write_text(
        json.dumps({"stage": "S7", "status": "PASSED"}),
        encoding="utf-8",
    )
    (release_dir / "publication.json").write_text(
        json.dumps(
            {
                "project_id": "supply-chain-live",
                "release_version": "1.0.0",
                "approval_decision": "APPROVED",
                "published_at": NOW.isoformat(),
                "integrity_verification_status": "PASSED",
                "package_path": "07-release/package-1.0.0",
                "package_manifest_sha256": checksum(package_dir / "manifest.json"),
                "release_snapshot_sha256": checksum(
                    package_dir / "04-发布信息/release-snapshot.json"
                ),
            }
        ),
        encoding="utf-8",
    )
    return release_dir, package_dir


def build_deployment_binding(
    tmp_path: Path,
    binding: OntologyReleaseBinding,
    endpoint: str,
) -> Path:
    properties = tmp_path / "ontop.properties"
    properties.write_text(
        "jdbc.url=jdbc:postgresql://database/example\n"
        "jdbc.user=ontology_reader\n"
        "jdbc.password=test-only\n",
        encoding="utf-8",
    )
    attestation = tmp_path / "read-only-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "database_access_mode": "READ_ONLY",
                "write_privileges": False,
                "verification_method": "DATABASE_CATALOG_READBACK",
                "properties_sha256": checksum(properties),
                "database_principal": "ontology_reader",
                "verified_by": "database-owner",
                "verified_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    deployment = render_ontop_release_deployment(
        binding,
        output_dir=tmp_path / "deployment",
        properties_path=properties,
        read_only_attestation_path=attestation,
        endpoint=endpoint,
    )
    return Path(str(deployment["deployment_binding_path"]))


def test_release_binding_requires_verified_package_artifacts(tmp_path: Path) -> None:
    release_dir, package_dir = build_verified_release(tmp_path)

    binding = ReleaseBindingLoader().load(tmp_path)

    assert binding.project_id == "supply-chain-live"
    assert binding.ontop_query_names == {"supply_order_context"}
    assert binding.integrity_status == "verified"
    assert binding.document_query_capabilities == ["reviewed_entity_evidence"]

    gate_path = release_dir / "gate-results.json"
    passed_gate = gate_path.read_text(encoding="utf-8")
    gate_path.write_text(
        json.dumps(
            {
                "stage": "S7",
                "status": "PENDING",
                "gates": [
                    {"id": gate_id, "status": "PASSED"}
                    for gate_id in (
                        "G-S7-HUMAN-APPROVAL",
                        "G-S7-INTEGRITY",
                        "G-S7-PACKAGE-COMPLETE",
                        "G-S7-MANIFEST",
                        "G-S7-CQ-LINEAGE",
                        "G-S7-RECOVERY-SNAPSHOT",
                    )
                ]
                + [{"id": "G-S7-RUNTIME", "status": "PENDING"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReleaseBindingError, match="gate has not passed"):
        ReleaseBindingLoader().load(tmp_path)
    deployment_binding = ReleaseBindingLoader().load(
        tmp_path,
        allow_pre_runtime=True,
    )
    assert deployment_binding.release_version == "1.0.0"
    gate_path.write_text(passed_gate, encoding="utf-8")

    query_path = package_dir / "05-运行时/queries/supply_order_context.rq"
    query_path.write_text("SELECT * WHERE { ?changed ?p ?o }", encoding="utf-8")
    with pytest.raises(ReleaseBindingError, match="artifact checksum mismatch"):
        ReleaseBindingLoader().load(tmp_path)

    query_path.write_text("SELECT * WHERE { ?s ?p ?o }", encoding="utf-8")
    mapping_path = package_dir / "05-运行时/mapping.obda"
    mapping_original = mapping_path.read_text(encoding="utf-8")
    mapping_path.write_text(f"{mapping_original}\n# tampered", encoding="utf-8")
    with pytest.raises(ReleaseBindingError, match="artifact checksum mismatch"):
        ReleaseBindingLoader().load(tmp_path)
    mapping_path.write_text(mapping_original, encoding="utf-8")

    publication_path = release_dir / "publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    publication["integrity_verification_status"] = "NOT_VERIFIED"
    publication_path.write_text(json.dumps(publication), encoding="utf-8")
    with pytest.raises(ReleaseBindingError, match="integrity is not verified"):
        ReleaseBindingLoader().load(tmp_path)
    publication["integrity_verification_status"] = "PASSED"
    publication_path.write_text(json.dumps(publication), encoding="utf-8")

    (release_dir / "release-revocation.json").write_text(
        json.dumps({"status": "REVOKED"}),
        encoding="utf-8",
    )
    with pytest.raises(ReleaseBindingError, match="has been revoked"):
        ReleaseBindingLoader().load(tmp_path)


def test_release_binding_loads_only_manifest_protected_reasoning_rules(
    tmp_path: Path,
) -> None:
    release_dir, package_dir = build_verified_release(tmp_path)
    rule_relative = "05-运行时/rules/order_expediting.json"
    rule_path = package_dir / rule_relative
    rule_path.parent.mkdir(parents=True)
    rule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_name": "order_expediting",
                "rules": [
                    {
                        "rule_id": "RULE-ORDER-EXPEDITE-001",
                        "description_zh": "延期订单需要催交",
                        "expression": (
                            "IF DelayedOrder(?order) "
                            "THEN NeedsExpediting(?order)"
                        ),
                        "confidence": 0.97,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_path = package_dir / "05-运行时/realtime-runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["schema_version"] = 3
    runtime["reasoning_capabilities"] = {
        "order_expediting": {
            "description_zh": "根据延期订单事实推出需催交订单",
            "engine": "SEMANTICA_FORWARD",
            "evidence_query": "supply_order_context",
            "execution_scope": "FULL_QUERY_RESULT",
            "fact_bindings": [
                {
                    "predicate": "DelayedOrder",
                    "arguments": [{"field": "order"}],
                    "when": {"field": "status", "equals": "DELAYED"},
                }
            ],
            "result_predicates": ["NeedsExpediting"],
            "source_rule_ids": ["RULE-ORDER-EXPEDITE-001"],
            "question_examples": ["哪些订单需要催交？"],
            "ontology_terms": {
                "DelayedOrder": "https://example.com/ontology/supply-chain/DelayedOrder",
                "NeedsExpediting": "https://example.com/ontology/supply-chain/NeedsExpediting",
            },
            "runtime_validation": {
                "parameters": {"purchase_order_code": "PO-202608-017"},
                "min_input_facts": 1,
                "min_result_facts": 1,
                "require_rules_fired": True,
            },
            "rule_artifact": rule_relative,
            "rule_sha256": checksum(rule_path),
        }
    }
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in manifest["files"]]
    paths.append(rule_relative)
    manifest["files"] = [
        {"path": relative, "sha256": checksum(package_dir / relative)}
        for relative in paths
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    publication_path = release_dir / "publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    publication["package_manifest_sha256"] = checksum(manifest_path)
    publication_path.write_text(json.dumps(publication), encoding="utf-8")

    binding = ReleaseBindingLoader().load(tmp_path)

    assert binding.reasoning_capabilities["order_expediting"]["rule_sha256"] == (
        checksum(rule_path)
    )
    assert binding.reasoning_rule_packages["order_expediting"]["rules"][0][
        "rule_id"
    ] == "RULE-ORDER-EXPEDITE-001"

    rule_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseBindingError, match="artifact checksum mismatch"):
        ReleaseBindingLoader().load(tmp_path)


def test_bound_ontop_rechecks_query_hash_on_every_render(tmp_path: Path) -> None:
    _, package_dir = build_verified_release(tmp_path)
    loader = ReleaseBindingLoader()
    binding = loader.load(tmp_path)
    client = loader.build_ontop_client(binding, "http://localhost:8080/sparql")

    assert "SELECT" in client.render("supply_order_context")

    query_path = package_dir / "05-运行时/queries/supply_order_context.rq"
    query_path.write_text("SELECT * WHERE { ?tampered ?p ?o }", encoding="utf-8")
    with pytest.raises(QueryTemplateError, match="checksum mismatch"):
        client.render("supply_order_context")


def test_ontop_runtime_endpoint_probe_requires_the_packaged_mapping_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_verified_release(tmp_path)
    loader = ReleaseBindingLoader()
    binding = loader.load(tmp_path)
    client = loader.build_ontop_client(binding, "http://localhost:8080/sparql")
    deployment_binding_path = build_deployment_binding(
        tmp_path,
        binding,
        client.endpoint,
    )

    def matching_marker(
        probe: OntopClient,
        name: str,
        **parameters: str,
    ) -> list[dict[str, str]]:
        assert name == "orion_deployment_identity"
        assert not parameters
        return [
            {
                "deployment_id": binding.ontop_deployment_id,
                "source_mapping_sha256": binding.source_mapping_sha256,
                "access_mode": "READ_ONLY",
            }
        ]

    monkeypatch.setattr(OntopClient, "select", matching_marker)
    identity = OntopDeploymentVerifier().verify_endpoint(
        client,
        binding,
        deployment_binding_path,
    )

    assert identity.mapping_sha256 == binding.artifact_checksums[binding.mapping_artifact]
    assert client.runtime_verification_status == "VERIFIED"

    def stale_marker(
        probe: OntopClient,
        name: str,
        **parameters: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "deployment_id": "old-deployment",
                "source_mapping_sha256": binding.source_mapping_sha256,
                "access_mode": "READ_ONLY",
            }
        ]

    stale_client = loader.build_ontop_client(
        binding,
        "http://localhost:8080/sparql",
    )
    monkeypatch.setattr(OntopClient, "select", stale_marker)
    with pytest.raises(OntopRuntimeVerificationError, match="approved release mapping"):
        OntopDeploymentVerifier().verify_endpoint(
            stale_client,
            binding,
            deployment_binding_path,
        )
    assert stale_client.runtime_verification_status == "NOT_RUNTIME_VERIFIED"


def test_deployment_binding_rejects_properties_changed_after_render(
    tmp_path: Path,
) -> None:
    build_verified_release(tmp_path)
    binding = ReleaseBindingLoader().load(tmp_path)
    deployment_binding_path = build_deployment_binding(
        tmp_path,
        binding,
        "http://localhost:18080/sparql",
    )
    deployment = json.loads(deployment_binding_path.read_text(encoding="utf-8"))
    properties_path = Path(deployment["properties_path"])
    properties_path.write_text(
        properties_path.read_text(encoding="utf-8") + "jdbc.user=changed\n",
        encoding="utf-8",
    )

    with pytest.raises(OntopDeploymentContractError, match="changed after render"):
        verify_ontop_deployment_binding(binding, deployment_binding_path)


def test_ontop_supports_release_specific_query_allowlist(tmp_path: Path) -> None:
    (tmp_path / "current_inventory.rq").write_text(
        'SELECT ?value WHERE { BIND("{{material_code}}" AS ?value) }',
        encoding="utf-8",
    )
    client = OntopClient(
        "http://localhost:8080/sparql",
        query_dir=tmp_path,
        allowed_queries={"current_inventory"},
    )

    rendered = client.render("current_inventory", material_code="MAT-001")

    assert "MAT-001" in rendered
    with pytest.raises(QueryTemplateError, match="not allowed"):
        client.render("supply_orders")


def test_ontop_select_rejects_write_operations_in_an_allowlisted_template(
    tmp_path: Path,
) -> None:
    query_path = tmp_path / "unsafe.rq"
    query_path.write_text(
        "DELETE WHERE { ?s ?p ?o }; SELECT * WHERE { ?s ?p ?o }",
        encoding="utf-8",
    )
    client = OntopClient(
        "http://localhost:8080/sparql",
        query_dir=tmp_path,
        allowed_queries={"unsafe"},
    )

    with pytest.raises(QueryTemplateError, match="read-only SELECT"):
        client.select("unsafe")


def test_s0_indexer_writes_an_idempotent_version_graph() -> None:
    fuseki = FakeFuseki()
    document = S0DocumentVersion(
        project_id="supply-chain-live",
        document_id="DOC-001",
        version="2",
        title="供应商延期说明",
        document_type="supplier_notice",
        source_uri="s3://evidence/DOC-001.pdf",
        file_sha256="a" * 64,
        processed_at=NOW,
        full_text="供应商确认订单将延期三天。",
        mentions_entities=["https://example.com/resource/supply-chain/order-PO-202608-017"],
        pages=[
            DocumentPageEvidence(
                page_number=1,
                text="延期三天",
                confidence=0.96,
                source_locator="Word 段落 1",
            ),
            DocumentPageEvidence(
                page_number=1,
                text="补充说明",
                confidence=0.91,
                source_locator="Word 段落 2",
            ),
        ],
    )
    indexer = S0IncrementalDocumentIndexer(fuseki, now=lambda: NOW)

    first = indexer.index(document)
    second = indexer.index(document)

    assert first.graph_uri == second.graph_uri
    assert first.graph_uri.endswith("a" * 64)
    assert first.triple_count >= 16
    assert len(fuseki.puts) == 2
    assert len(fuseki.puts[0][1]) == first.triple_count
    graph = fuseki.puts[0][1]
    page_nodes = set(graph.objects(None, EV.hasPageEvidence))
    assert len(page_nodes) == 2
    assert {str(value) for value in graph.objects(None, EV.sourceLocator)} == {
        "Word 段落 1",
        "Word 段落 2",
    }


def test_incremental_pipeline_promotes_only_after_successful_index(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notice.txt"
    source.write_text("version one", encoding="utf-8")
    fuseki = FakeFuseki()
    registry = DocumentCurrentRegistry(tmp_path / "current")
    indexer = S0IncrementalDocumentIndexer(fuseki, now=lambda: NOW)
    snapshot_store = FakeSnapshotStore(source)
    pipeline = S0IncrementalPipeline(
        snapshot_store,  # type: ignore[arg-type]
        indexer,
        registry,
        now=lambda: NOW,
    )

    def parser(path: Path, snapshot: OriginalSnapshot, version: str) -> S0DocumentVersion:
        return S0DocumentVersion(
            project_id="supply-chain-live",
            document_id="DOC-001",
            version=version,
            title="延期说明",
            document_type="notice",
            source_uri=snapshot.source_uri,
            file_sha256=snapshot.file_sha256,
            processed_at=NOW,
            full_text=path.read_text(encoding="utf-8"),
            mentions_entities=["urn:orion:order:001"],
        )

    first = pipeline.ingest(
        source,
        "supply-chain-live",
        "DOC-001",
        parser,
    )
    assert first.status == "promoted"

    unchanged = pipeline.ingest(
        source,
        "supply-chain-live",
        "DOC-001",
        parser,
    )
    assert unchanged.status == "unchanged"

    reindexed = pipeline.ingest(
        source,
        "supply-chain-live",
        "DOC-001",
        parser,
        force_reindex=True,
    )
    assert reindexed.status == "promoted"
    assert reindexed.current.file_sha256 == first.current.file_sha256
    assert len(fuseki.puts) == 2

    source.write_text("version two", encoding="utf-8")

    def failed_index(graph_uri: str, graph: Any) -> None:
        raise OSError("Fuseki unavailable")

    fuseki.put_graph = failed_index  # type: ignore[method-assign]
    with pytest.raises(OSError, match="Fuseki unavailable"):
        pipeline.ingest(source, "supply-chain-live", "DOC-001", parser)

    current = registry.current("supply-chain-live", "DOC-001")
    assert current is not None
    assert current.file_sha256 == first.current.file_sha256


def test_local_current_registry_rejects_stale_cas(tmp_path: Path) -> None:
    registry = DocumentCurrentRegistry(tmp_path / "current")
    first = CurrentDocumentPointer(
        project_id="supply-chain-live",
        document_id="DOC-001",
        version="v1",
        file_sha256="a" * 64,
        source_uri="minio://evidence/a",
        graph_uri="urn:graph:documents:supply-chain-live:DOC-001:" + "a" * 64,
        indexed_at=NOW,
        promoted_at=NOW,
    )
    second = first.model_copy(
        update={
            "version": "v2",
            "file_sha256": "b" * 64,
            "source_uri": "minio://evidence/b",
            "graph_uri": "urn:graph:documents:supply-chain-live:DOC-001:" + "b" * 64,
        }
    )
    registry.promote(first, expected_current_sha256=None)

    with pytest.raises(DocumentPromotionConflict):
        registry.promote(second, expected_current_sha256=None)

    assert registry.current("supply-chain-live", "DOC-001") == first
    assert registry.count_current("supply-chain-live") == 1
    assert registry.count_current("another-project") == 0


def test_incremental_pipeline_rejects_corrupted_minio_readback(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("trusted original", encoding="utf-8")
    materialized = tmp_path / "downloaded.txt"
    materialized.write_text("corrupted object", encoding="utf-8")
    snapshot_store = FakeSnapshotStore(materialized)
    fuseki = FakeFuseki()
    registry = DocumentCurrentRegistry(tmp_path / "current")
    pipeline = S0IncrementalPipeline(
        snapshot_store,  # type: ignore[arg-type]
        S0IncrementalDocumentIndexer(fuseki, now=lambda: NOW),
        registry,
        now=lambda: NOW,
    )

    with pytest.raises(ValueError, match="does not match its SHA-256"):
        pipeline.ingest(
            source,
            "supply-chain-live",
            "DOC-001",
            lambda path, snapshot, version: pytest.fail("parser must not run"),
        )

    assert fuseki.puts == []
    assert registry.current("supply-chain-live", "DOC-001") is None


def promote_current(
    registry: DocumentCurrentRegistry,
    *,
    version: str,
    file_sha256: str,
    graph_uri: str,
) -> None:
    registry.promote(
        CurrentDocumentPointer(
            project_id="supply-chain-live",
            document_id="DOC-001",
            version=version,
            file_sha256=file_sha256,
            source_uri=f"minio://evidence/{file_sha256}",
            graph_uri=graph_uri,
            indexed_at=NOW,
            promoted_at=NOW,
        )
    )


def build_committed_s0_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_dir = tmp_path / "supply-chain-live"
    stage_dir = project_dir / "00-document-evidence"
    markdown_dir = stage_dir / "structured-markdown"
    input_root = tmp_path / "inputs"
    markdown_dir.mkdir(parents=True)
    input_root.mkdir()
    source = input_root / "notice.pdf"
    source.write_bytes(b"immutable-original")
    markdown = "## 第 1 页\n\n订单 PO-001 延期三天。\n\n## 第 2 页\n\n补充说明。\n"
    markdown_path = markdown_dir / "notice.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (stage_dir / "gate-results.json").write_text(
        json.dumps(
            {
                "stage": "S0",
                "status": "PASSED",
                "checked_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (stage_dir / "ingestion-quality-report.json").write_text(
        json.dumps({"status": "PASSED", "reviewed_at": NOW.isoformat()}),
        encoding="utf-8",
    )
    (stage_dir / "document-register.json").write_text(
        json.dumps(
            [
                {
                    "document_id": "DOC-001",
                    "source_name": "供应商延期通知.pdf",
                    "source_path": "notice.pdf",
                    "source_type": "PDF",
                    "source_sha256": checksum(source),
                    "original_storage_backend": "minio",
                    "original_bucket": "orion-workflow-artifacts",
                    "original_object_key": (
                        "sha256/00/" + hashlib.sha256(source.read_bytes()).hexdigest()
                    ),
                    "original_source_uri": (
                        "minio://orion-workflow-artifacts/sha256/00/"
                        + hashlib.sha256(source.read_bytes()).hexdigest()
                    ),
                    "original_snapshot_sha256": checksum(source),
                    "original_snapshot_size_bytes": source.stat().st_size,
                    "structured_markdown_path": "structured-markdown/notice.md",
                    "structured_markdown_sha256": checksum(markdown_path),
                }
            ]
        ),
        encoding="utf-8",
    )
    (stage_dir / "evidence-index.json").write_text(
        json.dumps(
            [
                {
                    "evidence_id": "EVD-001",
                    "document_id": "DOC-001",
                    "source_locator": "PDF 第 1 页；文字层",
                    "markdown_section": "第 1 页",
                },
                {
                    "evidence_id": "EVD-002",
                    "document_id": "DOC-001",
                    "source_locator": "PDF 第 2 页；文字层",
                    "markdown_section": "第 2 页",
                },
            ]
        ),
        encoding="utf-8",
    )
    links_path = tmp_path / "entity-links.json"
    links_path.write_text(
        json.dumps(
            {
                "project_id": "supply-chain-live",
                "release_fingerprint": release_binding().release_fingerprint,
                "reviewed_by": "reviewer",
                "reviewed_at": NOW.isoformat(),
                "documents": {
                    "DOC-001": ["https://example.com/resource/supply-chain/order-PO-001"]
                },
            }
        ),
        encoding="utf-8",
    )
    return project_dir, input_root, links_path


def test_s0_adapter_requires_release_bound_entity_links_and_preserves_pages(
    tmp_path: Path,
) -> None:
    project_dir, input_root, links_path = build_committed_s0_fixture(tmp_path)

    documents = S0BundleAdapter().prepare(
        project_dir=project_dir,
        input_root=input_root,
        binding=release_binding(),
        entity_links_path=links_path,
    )

    assert len(documents) == 1
    assert documents[0].source_sha256 == hashlib.sha256(b"immutable-original").hexdigest()
    assert documents[0].snapshot.backend == "minio"
    assert documents[0].snapshot.file_sha256 == documents[0].source_sha256
    assert [page.page_number for page in documents[0].pages] == [1, 2]
    assert [page.source_locator for page in documents[0].pages] == [
        "PDF 第 1 页；文字层",
        "PDF 第 2 页；文字层",
    ]
    assert documents[0].pages[0].text == "订单 PO-001 延期三天。"
    assert documents[0].pages[0].confidence is None

    links = json.loads(links_path.read_text(encoding="utf-8"))
    links["release_fingerprint"] = "sha256:" + "f" * 64
    links_path.write_text(json.dumps(links), encoding="utf-8")
    with pytest.raises(S0RealtimeAdapterError, match="not bound to this release"):
        S0BundleAdapter().prepare(
            project_dir=project_dir,
            input_root=input_root,
            binding=release_binding(),
            entity_links_path=links_path,
        )


def test_s0_adapter_allows_full_text_publication_without_entity_links(
    tmp_path: Path,
) -> None:
    project_dir, input_root, _links_path = build_committed_s0_fixture(tmp_path)

    documents = S0BundleAdapter().prepare(
        project_dir=project_dir,
        input_root=input_root,
        binding=release_binding(),
    )

    assert len(documents) == 1
    assert documents[0].mentions_entities == ()
    assert "订单 PO-001 延期三天" in documents[0].full_text


def test_s0_adapter_rejects_unlinked_publication_for_legacy_release(
    tmp_path: Path,
) -> None:
    project_dir, input_root, _links_path = build_committed_s0_fixture(tmp_path)
    legacy_binding = release_binding().model_copy(
        update={"document_query_capabilities": ["reviewed_entity_evidence"]}
    )

    with pytest.raises(S0RealtimeAdapterError, match="does not allow full-text"):
        S0BundleAdapter().prepare(
            project_dir=project_dir,
            input_root=input_root,
            binding=legacy_binding,
        )


def test_s0_adapter_keeps_word_paragraph_locator_excerpt_bounded(tmp_path: Path) -> None:
    project_dir, input_root, links_path = build_committed_s0_fixture(tmp_path)
    stage_dir = project_dir / "00-document-evidence"
    markdown_path = stage_dir / "structured-markdown/notice.md"
    markdown_path.write_text(
        "# 标题\n\n第一段业务说明。\n\n第二段业务说明。\n",
        encoding="utf-8",
    )
    register_path = stage_dir / "document-register.json"
    register = json.loads(register_path.read_text(encoding="utf-8"))
    register[0]["structured_markdown_sha256"] = checksum(markdown_path)
    register_path.write_text(json.dumps(register), encoding="utf-8")
    (stage_dir / "evidence-index.json").write_text(
        json.dumps(
            [
                {
                    "evidence_id": "EVD-WORD-002",
                    "document_id": "DOC-001",
                    "source_locator": "Word 段落 2",
                    "markdown_section": "第一段业务说明。",
                }
            ]
        ),
        encoding="utf-8",
    )

    document = S0BundleAdapter().prepare(
        project_dir=project_dir,
        input_root=input_root,
        binding=release_binding(),
        entity_links_path=links_path,
    )[0]

    assert document.pages[0].source_locator == "Word 段落 2"
    assert document.pages[0].text == "第一段业务说明。"
    assert "第二段业务说明" not in document.pages[0].text

    evidence_path = stage_dir / "evidence-index.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence[0]["markdown_section"] = "图片说明 2"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    unresolved = S0BundleAdapter().prepare(
        project_dir=project_dir,
        input_root=input_root,
        binding=release_binding(),
        entity_links_path=links_path,
    )[0]
    assert unresolved.pages[0].text == "图片说明 2"
    assert "第二段业务说明" not in unresolved.pages[0].text


def test_s0_adapter_rejects_a_passed_bundle_without_verified_minio_original(
    tmp_path: Path,
) -> None:
    project_dir, input_root, links_path = build_committed_s0_fixture(tmp_path)
    register_path = project_dir / "00-document-evidence/document-register.json"
    register = json.loads(register_path.read_text(encoding="utf-8"))
    register[0].pop("original_object_key")
    register_path.write_text(json.dumps(register), encoding="utf-8")

    with pytest.raises(S0RealtimeAdapterError, match="verified MinIO original"):
        S0BundleAdapter().prepare(
            project_dir=project_dir,
            input_root=input_root,
            binding=release_binding(),
            entity_links_path=links_path,
        )


def test_hybrid_bundle_uses_live_db_and_only_promoted_document_version(
    tmp_path: Path,
) -> None:
    ontop = FakeOntop()
    registry = DocumentCurrentRegistry(tmp_path / "current")
    current_graph = "urn:graph:documents:supply-chain-live:DOC-001:" + "a" * 64
    promote_current(
        registry,
        version="1",
        file_sha256="a" * 64,
        graph_uri=current_graph,
    )
    fuseki = FakeFuseki(
        [
            {
                "graph": current_graph,
                "document_id": "DOC-001",
                "version": "1",
                "sha256": "a" * 64,
                "processed_at": "2026-09-01T01:00:00+00:00",
                "page": "1",
            },
            {
                "graph": "urn:graph:documents:supply-chain-live:DOC-001:" + "b" * 64,
                "document_id": "DOC-001",
                "version": "2",
                "sha256": "b" * 64,
                "processed_at": "2026-09-02T01:00:00+00:00",
                "page": "1",
            },
            {
                "graph": "urn:graph:documents:supply-chain-live:DOC-001:" + "b" * 64,
                "document_id": "DOC-001",
                "version": "2",
                "sha256": "b" * 64,
                "processed_at": "2026-09-02T01:00:00+00:00",
                "page": "2",
            },
        ]
    )
    service = HybridRealtimeEvidenceService(
        release_binding(),
        ontop,  # type: ignore[arg-type]
        fuseki,  # type: ignore[arg-type]
        registry,
        now=lambda: NOW,
    )
    entity_iri = "https://example.com/resource/supply-chain/order-PO-202608-017"

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-001",
            structured_query=StructuredQueryRequest(
                name="supply_order_context",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
            entity_iris=[entity_iri],
        )
    )

    assert bundle.mode == "hybrid"
    assert bundle.complete is True
    assert bundle.source_status["structured_db"].status == "fresh"
    assert bundle.source_status["document"].status == "fresh"
    assert bundle.source_status["document"].version == "1"
    assert [item.source_kind for item in bundle.evidence] == [
        "ontology_release",
        "structured_db",
        "document",
    ]
    document_rows = bundle.evidence[-1].payload["rows"]
    assert len(document_rows) == 1
    assert {row["version"] for row in document_rows} == {"1"}


def test_published_query_retains_rdf_type_evidence(tmp_path: Path) -> None:
    class TypedOntop(FakeOntop):
        def select_with_metadata(self, name, **parameters):
            self.calls.append((name, parameters))
            return {"rows": [{"days": "7"}], "variables": ["days"], "row_terms": [{
                "days": {"type": "literal", "value": "7", "datatype": "http://www.w3.org/2001/XMLSchema#integer"}}]}

    ontop = TypedOntop()
    service = HybridRealtimeEvidenceService(release_binding(), ontop, FakeFuseki(),
        DocumentCurrentRegistry(tmp_path / "current"), now=lambda: NOW)
    bundle = service.collect(RealtimeEvidenceRequest(query_id="Q-typed", structured_query=StructuredQueryRequest(
        name="supply_order_context", parameters={"purchase_order_code": "PO-202608-017"})))
    assert bundle.complete
    payload = next(item.payload for item in bundle.evidence if item.source_kind == "structured_db")
    assert payload["rows"] == [{"days": "7"}]
    assert payload["row_terms"][0]["days"]["datatype"].endswith("#integer")
    assert payload["variables"] == ["days"]
    assert len(ontop.calls) == 1


def test_hybrid_bundle_reports_source_degradation_without_inventing_rows(
    tmp_path: Path,
) -> None:
    service = HybridRealtimeEvidenceService(
        release_binding(),
        FakeOntop(OSError("Ontop unavailable")),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-002",
            structured_query=StructuredQueryRequest(
                name="supply_order_context",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
        )
    )

    assert bundle.complete is False
    assert len(bundle.evidence) == 1
    assert bundle.degraded_sources[0].source_kind == "structured_db"
    assert "Ontop unavailable" in bundle.degraded_sources[0].reason
    assert bundle.source_status["structured_db"].status == "degraded"
    assert bundle.source_status["document"].status == "not_requested"


def test_snapshot_bound_query_emits_per_source_evidence_v2(tmp_path: Path) -> None:
    snapshot_set_id = "SS-" + "A" * 24
    binding = release_binding().model_copy(
        update={
            "snapshot_set_id": snapshot_set_id,
            "source_bindings": {
                "employee_pg": {
                    "engine": "POSTGRESQL",
                    "database": "orion_source_data",
                    "schemas": ["orion_data"],
                    "pii_scope": "STABLE_HASHED_IDENTIFIER_ONLY",
                    "masking": "HASHED",
                },
                "application_mysql": {
                    "engine": "MYSQL",
                    "database": "orion_mysql_test",
                    "schemas": ["orion_mysql_test"],
                    "pii_scope": "STABLE_HASHED_IDENTIFIER_ONLY",
                    "masking": "HASHED",
                },
            },
            "snapshot_manifests": {
                "employee_pg": {
                    "dataset_id": "DS-EMPLOYEE-00000001",
                    "snapshot_version": "v1",
                    "snapshot_complete": True,
                    "source_sha256": "sha256:" + "a" * 64,
                },
                "application_mysql": {
                    "dataset_id": "DS-APPLICATION-0001",
                    "snapshot_version": "v1",
                    "snapshot_complete": True,
                    "source_sha256": "sha256:" + "b" * 64,
                },
            },
            "ontop_query_capabilities": {
                "supply_order_context": {
                    "source_ids": ["employee_pg", "application_mysql"],
                    "source_tables": ["employees", "applications"],
                    "source_columns": ["employee_id_hash", "applicant_id"],
                }
            },
        }
    )
    service = HybridRealtimeEvidenceService(
        binding,
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )
    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-MULTI-SOURCE",
            structured_query=StructuredQueryRequest(
                name="supply_order_context",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
        )
    )
    assert bundle.snapshot_set_id == snapshot_set_id
    assert set(bundle.source_status_by_id) == {"employee_pg", "application_mysql"}
    assert len(bundle.source_evidence_v2) == 2
    assert all(item.freshness == "UNKNOWN" for item in bundle.source_evidence_v2)
    assert all(item.observed_at is None and item.queried_at == NOW for item in bundle.source_evidence_v2)
    assert all(
        item.release_fingerprint == binding.release_fingerprint
        for item in bundle.source_evidence_v2
    )


def test_realtime_required_is_not_silently_served_by_snapshot_runtime(
    tmp_path: Path,
) -> None:
    service = HybridRealtimeEvidenceService(
        release_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )
    with pytest.raises(ValueError, match="SourceQueryGateway"):
        service.collect(
            RealtimeEvidenceRequest(
                query_id="Q-REALTIME",
                execution_mode="REALTIME_REQUIRED",
                structured_query=StructuredQueryRequest(
                    name="supply_order_context",
                    parameters={"purchase_order_code": "PO-202608-017"},
                ),
            )
        )


def test_document_full_text_query_returns_only_current_version(tmp_path: Path) -> None:
    current_graph = "urn:graph:documents:supply-chain-live:DOC-001:" + "a" * 64
    registry = DocumentCurrentRegistry(tmp_path / "current")
    registry.promote(
        CurrentDocumentPointer(
            project_id="supply-chain-live",
            document_id="DOC-001",
            version="1",
            file_sha256="a" * 64,
            source_uri="minio://documents/current",
            graph_uri=current_graph,
            indexed_at=NOW,
            promoted_at=NOW,
        ),
        expected_current_sha256=None,
    )
    fuseki = FakeFuseki(
        [
            {
                "graph": current_graph,
                "document_id": "DOC-001",
                "version": "1",
                "sha256": "a" * 64,
                "title": "会议纪要",
                "page": "2",
                "page_text": "异常率超过0.2%后继续追溯",
            },
            {
                "graph": "urn:graph:documents:supply-chain-live:DOC-001:" + "b" * 64,
                "document_id": "DOC-001",
                "version": "2",
                "sha256": "b" * 64,
                "title": "旧版会议纪要",
                "page": "2",
                "page_text": "旧阈值",
            },
        ]
    )
    service = HybridRealtimeEvidenceService(
        release_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        fuseki,  # type: ignore[arg-type]
        registry,
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-DOCUMENT-SEARCH",
            document_query=DocumentQueryRequest(text="异常率", limit=10),
        )
    )

    assert bundle.mode == "documents"
    assert bundle.complete is True
    assert bundle.source_status["document"].status == "fresh"
    rows = bundle.evidence[-1].payload["rows"]
    assert len(rows) == 1
    assert rows[0]["title"] == "会议纪要"


def test_document_full_text_query_fails_closed_when_release_does_not_allow_it(
    tmp_path: Path,
) -> None:
    binding = release_binding().model_copy(
        update={"document_query_capabilities": ["reviewed_entity_evidence"]}
    )
    service = HybridRealtimeEvidenceService(
        binding,
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )

    with pytest.raises(ValueError, match="not allowed by this release"):
        service.collect(
            RealtimeEvidenceRequest(
                query_id="Q-DOCUMENT-NOT-ALLOWED",
                document_query=DocumentQueryRequest(text="电压测试VD1"),
            )
        )


def test_structured_query_fails_closed_before_runtime_verification(
    tmp_path: Path,
) -> None:
    ontop = FakeOntop(runtime_verified=False)
    service = HybridRealtimeEvidenceService(
        release_binding(),
        ontop,  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-UNVERIFIED",
            structured_query=StructuredQueryRequest(
                name="supply_order_context",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
        )
    )

    assert ontop.calls == []
    assert bundle.complete is False
    assert bundle.source_status["structured_db"].status == "degraded"
    assert bundle.source_status["structured_db"].details == {
        "artifact_verification": "verified",
        "runtime_verification": "NOT_RUNTIME_VERIFIED",
        "query_modes": {"supply_order_context": "UNKNOWN"},
        "freshness_scope": "QUERY_EXECUTION_ONLY",
        "upstream_freshness": "UNKNOWN",
    }


def test_document_status_aggregates_multiple_entities_and_keeps_any_failure(
    tmp_path: Path,
) -> None:
    good = "urn:orion:order:good"
    bad = "urn:orion:order:bad"
    service = HybridRealtimeEvidenceService(
        release_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(
            rows_by_entity={good: []},
            errors={bad: OSError("Fuseki entity lookup failed")},
        ),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )

    bundle = service.collect(RealtimeEvidenceRequest(query_id="Q-003", entity_iris=[bad, good]))

    assert bundle.complete is False
    assert bundle.source_status["document"].status == "degraded"
    assert len(bundle.source_status["document"].details["entities"]) == 2
    assert "Fuseki entity lookup failed" in (bundle.source_status["document"].degraded_reason or "")


def test_document_rows_with_empty_document_id_fail_closed(tmp_path: Path) -> None:
    service = HybridRealtimeEvidenceService(
        release_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki([{"document_id": "", "graph": "urn:graph:documents:x"}]),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(query_id="Q-004", entity_iris=["urn:orion:order:1"])
    )

    assert bundle.complete is False
    assert bundle.source_status["document"].status == "degraded"
    assert "empty document_id" in (bundle.source_status["document"].degraded_reason or "")


def test_release_bound_reasoning_returns_one_auditable_evidence_bundle(
    tmp_path: Path,
) -> None:
    semantica = FakeSemantica()
    service = HybridRealtimeEvidenceService(
        reasoning_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        semantica=semantica,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-REASONING-001",
            reasoning_query=ReasoningQueryRequest(
                name="order_expediting",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
        )
    )

    assert bundle.mode == "reasoning"
    assert bundle.complete is True
    assert [item.source_kind for item in bundle.evidence] == [
        "ontology_release",
        "structured_db",
        "reasoning",
    ]
    assert semantica.calls == [
        {
            "facts": ["DelayedOrder(PO-202608-017)"],
            "rules": [
                "IF DelayedOrder(?order) THEN NeedsExpediting(?order)"
            ],
        }
    ]
    reasoning = bundle.evidence[-1].payload
    assert reasoning["read_only"] is True
    assert bundle.evidence[1].payload["evidence_role"] == "RULE_PREMISES_NOT_CONCLUSIONS"
    assert reasoning["conclusion_contract"]["failed_premise_entails_opposite"] is False
    assert reasoning["conclusion_contract"]["closed_world_input_predicates"] == []
    assert reasoning["result_facts"] == ["NeedsExpediting(PO-202608-017)"]
    assert reasoning["trace"][0]["rule_id"] == "RULE-ORDER-EXPEDITE-001"
    assert reasoning["trace"][0]["declared_rule_confidence"] == 0.97
    assert reasoning["rule_sha256"] == "sha256:" + "7" * 64
    assert reasoning["decision_record"]["decision_id"].startswith("DEC-")
    assert reasoning["decision_record"]["confidence"] == 0.97
    assert reasoning["decision_record"]["version_anchor"] == {
        "project_id": "supply-chain-live",
        "release_version": "1.0.0",
        "release_fingerprint": "sha256:" + "1" * 64,
    }
    assert bundle.source_status["reasoning"].status == "fresh"


def test_document_only_reasoning_uses_document_facts_without_ontop_rows(
    tmp_path: Path,
) -> None:
    ontop = FakeOntop()
    semantica = FakeSemantica()
    service = HybridRealtimeEvidenceService(
        document_reasoning_binding(),
        ontop,  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        semantica=semantica,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-DOCUMENT-REASONING-001",
            reasoning_query=ReasoningQueryRequest(name="order_expediting"),
        )
    )

    assert bundle.complete is True
    assert ontop.calls == []
    assert [item.source_kind for item in bundle.evidence] == [
        "ontology_release",
        "document",
        "reasoning",
    ]
    assert bundle.source_status["document_facts"].source_kind == "document"
    assert bundle.source_status["document_facts"].status == "fresh"
    assert semantica.calls == [
        {
            "facts": ["DelayedOrder(PO-202608-017)"],
            "rules": [
                "IF DelayedOrder(?order) THEN NeedsExpediting(?order)"
            ],
        }
    ]
    assert bundle.evidence[-1].payload["result_facts"] == [
        "NeedsExpediting(PO-202608-017)"
    ]


def test_reasoning_failure_is_visible_and_never_invents_a_conclusion(
    tmp_path: Path,
) -> None:
    service = HybridRealtimeEvidenceService(
        reasoning_binding(),
        FakeOntop(),  # type: ignore[arg-type]
        FakeFuseki(),  # type: ignore[arg-type]
        DocumentCurrentRegistry(tmp_path / "current"),
        semantica=FakeSemantica(OSError("Semantica unavailable")),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    bundle = service.collect(
        RealtimeEvidenceRequest(
            query_id="Q-REASONING-FAIL",
            reasoning_query=ReasoningQueryRequest(
                name="order_expediting",
                parameters={"purchase_order_code": "PO-202608-017"},
            ),
        )
    )

    assert bundle.complete is False
    assert [item.source_kind for item in bundle.evidence] == [
        "ontology_release",
        "structured_db",
    ]
    assert bundle.degraded_sources[-1].source_kind == "reasoning"
    assert bundle.source_status["reasoning"].status == "degraded"
    assert "Semantica unavailable" in (
        bundle.source_status["reasoning"].degraded_reason or ""
    )
