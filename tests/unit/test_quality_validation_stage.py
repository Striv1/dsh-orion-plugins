import hashlib
import json

import pytest
from rdflib import RDF, Graph, Literal, URIRef

from scripts.run_quality_validation_stage import (
    _canonical_fingerprint,
    build_reasoning_probe,
    materialize_reasoning_result_graph,
    reconcile_profile_scope,
    require_s6_runnable_state,
    resolve_base_release_runtime,
    resolve_s6_source_binding,
    source_content_fingerprint,
    sync_and_verify_semantica_relationships,
    validate_release_reasoning_capabilities,
    validated_table_reference,
)
from services.ontology_engineering import OntologyWorkflowService, WorkflowGateError


def test_source_fingerprint_detects_equal_count_content_change_and_duplicate_rows():
    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def __enter__(self):
            return iter((row,) for row in self.rows)

        def __exit__(self, *_args):
            return False

    class Connection:
        def __init__(self, rows):
            self.rows = rows

        def execution_options(self, **options):
            assert options == {"stream_results": True}
            return self

        def execute(self, query):
            assert 'ORDER BY row_to_json(source_row)::text COLLATE "C"' in str(query)
            return Rows(self.rows)

    relations = {"facts": "source.facts"}
    original = source_content_fingerprint(Connection(['{"value":1}']), relations)
    changed = source_content_fingerprint(Connection(['{"value":2}']), relations)
    duplicated = source_content_fingerprint(Connection(['{"value":1}', '{"value":1}']), relations)
    assert len({original, changed, duplicated}) == 3
    with pytest.raises(ValueError, match="unsafe"):
        source_content_fingerprint(Connection([]), {"facts": "facts; DROP TABLE facts"})


def test_generated_schema_qualified_table_reference_is_safely_quoted() -> None:
    assert (
        validated_table_reference('orion_data."current_ba33ff7cc109_001_81e3c06e"')
        == '"orion_data"."current_ba33ff7cc109_001_81e3c06e"'
    )
    assert validated_table_reference("sc_purchase_orders") == '"sc_purchase_orders"'
    with pytest.raises(ValueError, match="unsafe"):
        validated_table_reference("sc_purchase_orders; DROP TABLE users")


def test_s6_fails_fast_until_failed_stage_is_explicitly_reopened() -> None:
    with pytest.raises(RuntimeError, match="S6_STAGE_NOT_RUNNABLE"):
        require_s6_runnable_state(
            {
                "current_stage": "S6",
                "stage_statuses": {"S6": "FAILED"},
            }
        )

    require_s6_runnable_state(
        {
            "current_stage": "S6",
            "stage_statuses": {"S6": "RUNNING"},
        }
    )


def test_semantica_relationship_sync_batches_and_paginates_full_readback(
    monkeypatch,
) -> None:
    predicate = "https://example.com/hasAccount"
    graph = Graph()
    for subject, target in (("customer-1", "account-1"), ("customer-2", "account-2")):
        graph.add((URIRef(subject), URIRef(predicate), URIRef(target)))
    relationships = [
        {"subject": "customer-1", "predicate": predicate, "object": "account-1"},
        {"subject": "customer-2", "predicate": predicate, "object": "account-2"},
    ]
    relationship_sha = _canonical_fingerprint(relationships)
    candidate = "sha256:candidate"

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _path, **_kwargs):
            return FakeResponse({"nodes_added": 2, "edges_added": 2})

        def get(self, _path, *, params):
            index = 1 if params.get("cursor") else 0
            item = relationships[index]
            return FakeResponse(
                {
                    "edges": [
                        {
                            "source": item["subject"],
                            "type": item["predicate"],
                            "target": item["object"],
                            "properties": {
                                "project_id": "project-1",
                                "release_candidate_fingerprint": candidate,
                                "relationship_set_sha256": relationship_sha,
                            },
                        }
                    ],
                    "next_cursor": "page-2" if index == 0 else None,
                }
            )

    monkeypatch.setattr(
        "scripts.run_quality_validation_stage.httpx.Client",
        lambda **_kwargs: FakeClient(),
    )

    imported, verification = sync_and_verify_semantica_relationships(
        graph=graph,
        object_property_iris=[predicate],
        project_id="project-1",
        release_candidate_fingerprint=candidate,
        semantica=object(),
        semantica_api_url="http://semantica.test",
    )

    assert imported["relationship_count"] == 2
    assert imported["batch_count"] == 2
    assert verification["status"] == "VERIFIED"
    assert verification["readback_pages"] == 2
    assert verification["verified_relationship_count"] == 2


def test_s6_current_inventory_resolves_audited_snapshot_hub_binding() -> None:
    binding = resolve_s6_source_binding(
        {
            "datasources": [
                {
                    "database": "orion_source_data",
                    "database_principal": "orion_source_reader",
                    "access_mode": "READ_ONLY_AFTER_IMPORT",
                }
            ],
            "business_tables_scope": ["current_dataset_001"],
        },
        "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
    )

    assert binding["contract_shape"] == "CURRENT"
    assert binding["schema"] == "orion_data"
    assert binding["relations"] == {"current_dataset_001": "orion_data.current_dataset_001"}



def _snapshot_hub_inventory_without_principal() -> dict:
    return {
        "datasources": [
            {
                "source_id": "src",
                "database": "orion_source_data",
                "schema": "orion_data",
                "connection_env": "ORION_SOURCE_DATA_READER_URL",
                "access_mode": "READ_ONLY_AFTER_IMPORT",
                "via": "ORION Snapshot Hub",
            }
        ],
        "business_tables_scope": ["ms_src_0001"],
    }


def test_s6_snapshot_hub_import_without_principal_uses_catalog_readback() -> None:
    seen: list[str] = []
    binding = resolve_s6_source_binding(
        _snapshot_hub_inventory_without_principal(),
        "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
        catalog_readonly_verifier=seen.append,
    )
    assert seen == ["orion_source_reader"]
    assert binding["principal_evidence"] == "CATALOG_READBACK"
    assert binding["relations"] == {"ms_src_0001": "orion_data.ms_src_0001"}


def test_s6_snapshot_hub_catalog_readback_failure_fails_closed() -> None:
    def reject(_principal: str) -> None:
        raise RuntimeError("writable")

    with pytest.raises(RuntimeError, match="S6_SOURCE_NOT_READ_ONLY"):
        resolve_s6_source_binding(
            _snapshot_hub_inventory_without_principal(),
            "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
            catalog_readonly_verifier=reject,
        )


def test_s6_external_jdbc_without_principal_still_requires_s1_declaration() -> None:
    inventory = _snapshot_hub_inventory_without_principal()
    inventory["datasources"][0]["access_mode"] = "READ_ONLY"
    with pytest.raises(RuntimeError, match="S6_SOURCE_BINDING_MISSING"):
        resolve_s6_source_binding(
            inventory,
            "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
            catalog_readonly_verifier=lambda _p: None,
        )


def test_s6_reconciles_immutable_dataset_aliases_without_reprofiling() -> None:
    profile = {
        "tables": [
            {"table": "ds_a", "source_sheet": "orders", "row_count": 11},
            {"table": "ds_b", "source_sheet": "items", "row_count": 23},
        ]
    }
    inventory = {
        "datasets": [
            {
                "dataset_id": "DS-AAAAAAAAAAAAAAAAAAAA",
                "source_name": "orders.xlsx",
                "source_sha256": "sha256:" + "a" * 64,
                "row_count": 11,
            },
            {
                "dataset_id": "DS-BBBBBBBBBBBBBBBBBBBB",
                "source_name": "items.xlsx",
                "source_sha256": "sha256:" + "b" * 64,
                "row_count": 23,
            },
        ]
    }

    expected, mode = reconcile_profile_scope(
        profile=profile,
        inventory=inventory,
        scope=["current_orders", "current_items"],
        actual_by_table={"current_orders": 11, "current_items": 23},
    )

    assert expected == {"current_orders": 11, "current_items": 23}
    assert mode == "IMMUTABLE_DATASET_LINEAGE_ALIAS"


def test_s6_rejects_alias_reconciliation_when_content_counts_drift() -> None:
    with pytest.raises(RuntimeError, match="不可变数据集谱系"):
        reconcile_profile_scope(
            profile={"tables": [{"table": "ds_a", "source_sheet": "orders", "row_count": 11}]},
            inventory={
                "datasets": [
                    {
                        "dataset_id": "DS-AAAAAAAAAAAAAAAAAAAA",
                        "source_name": "orders.xlsx",
                        "source_sha256": "sha256:" + "a" * 64,
                        "row_count": 11,
                    }
                ]
            },
            scope=["current_orders"],
            actual_by_table={"current_orders": 12},
        )


def test_s6_legacy_inventory_uses_explicit_principal_and_schema() -> None:
    binding = resolve_s6_source_binding(
        {
            "database": "fraud_db",
            "schema": "public",
            "database_principal": "fraud_reader",
            "access_mode": "READ_ONLY",
            "business_tables_scope": ["account", "public.alert"],
        },
        "postgresql://fraud_reader:secret@db.example/fraud_db",
    )

    assert binding["contract_shape"] == "LEGACY"
    assert binding["relations"] == {
        "account": "public.account",
        "public.alert": "public.alert",
    }


def test_s6_legacy_inventory_without_principal_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="S6_SOURCE_BINDING_MISSING"):
        resolve_s6_source_binding(
            {
                "database": "fraud_db",
                "schema": "public",
                "access_mode": "READ_ONLY",
                "business_tables_scope": ["account"],
            },
            "postgresql://fraud_reader:secret@db.example/fraud_db",
        )


def test_s6_inventory_rejects_principal_and_database_mismatch() -> None:
    inventory = {
        "database": "fraud_db",
        "schema": "public",
        "database_principal": "fraud_reader",
        "access_mode": "READ_ONLY",
        "business_tables_scope": ["account"],
    }
    with pytest.raises(RuntimeError, match="S6_SOURCE_PRINCIPAL_MISMATCH"):
        resolve_s6_source_binding(
            inventory,
            "postgresql://other_reader:secret@db.example/fraud_db",
        )
    with pytest.raises(RuntimeError, match="S6_SOURCE_DATABASE_MISMATCH"):
        resolve_s6_source_binding(
            inventory,
            "postgresql://fraud_reader:secret@db.example/other_db",
        )


def test_s6_inventory_rejects_unsafe_schema_or_table() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        resolve_s6_source_binding(
            {
                "database": "fraud_db",
                "schema": "public;drop",
                "database_principal": "fraud_reader",
                "access_mode": "READ_ONLY",
                "business_tables_scope": ["account"],
            },
            "postgresql://fraud_reader:secret@db.example/fraud_db",
        )


def test_s6_new_project_does_not_require_a_parent_release_runtime(tmp_path) -> None:
    project_dir = tmp_path / "new-project"
    project_dir.mkdir()

    assert (
        resolve_base_release_runtime(
            workflow_home=tmp_path,
            project_dir=project_dir,
        )
        is None
    )


@pytest.mark.parametrize("configured_root", [None, "", " \t "])
def test_s6_revision_resolves_only_the_anchored_read_only_parent_runtime(
    tmp_path, monkeypatch, configured_root,
) -> None:
    if configured_root is None:
        monkeypatch.delenv("ORION_S7_AUTO_DEPLOY_ROOT", raising=False)
    else:
        monkeypatch.setenv("ORION_S7_AUTO_DEPLOY_ROOT", configured_root)
    workflow_home = tmp_path / "workflows"
    project_dir = workflow_home / "revision-project"
    source_dir = workflow_home / "source-project" / "07-release"
    binding_dir = workflow_home.parent / ".orion-runtime/realtime-business/source-project/0.1.1"
    project_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    binding_dir.mkdir(parents=True)
    fingerprint = "sha256:" + "a" * 64
    (project_dir / "based-on-release.json").write_text(
        json.dumps(
            {
                "source_project_id": "source-project",
                "source_release_version": "0.1.1",
            }
        ),
        encoding="utf-8",
    )
    (source_dir / "publication.json").write_text(
        json.dumps({"package_manifest_sha256": fingerprint}),
        encoding="utf-8",
    )
    (binding_dir / "deployment-binding.json").write_text(
        json.dumps(
            {
                "project_id": "source-project",
                "release_version": "0.1.1",
                "release_fingerprint": fingerprint,
                "database_access_mode": "READ_ONLY",
                "endpoint": "http://127.0.0.1:18081/sparql",
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_base_release_runtime(
        workflow_home=workflow_home,
        project_dir=project_dir,
    )

    assert resolved == {
        "project_id": "source-project",
        "release_version": "0.1.1",
        "release_fingerprint": fingerprint,
        "endpoint": "http://127.0.0.1:18081/sparql",
    }


@pytest.fixture
def managed_parent_release(tmp_path, monkeypatch):
    workflow_home = tmp_path / "Profile with spaces" / "state" / "workflows"
    project_dir = workflow_home / "revision-project"
    publication_path = workflow_home / "source-project/07-release/publication.json"
    deployment_root = workflow_home.parent / "realtime-business"
    binding_path = deployment_root / "source-project/0.1.1/deployment-binding.json"
    fingerprint = "sha256:" + "a" * 64
    binding = {
        "project_id": "source-project",
        "release_version": "0.1.1",
        "release_fingerprint": fingerprint,
        "database_access_mode": "READ_ONLY",
        "endpoint": "http://127.0.0.1:18082/sparql",
    }
    project_dir.mkdir(parents=True)
    publication_path.parent.mkdir(parents=True)
    binding_path.parent.mkdir(parents=True)
    (project_dir / "based-on-release.json").write_text(
        json.dumps(
            {"source_project_id": "source-project", "source_release_version": "0.1.1"}
        ),
        encoding="utf-8",
    )
    publication_path.write_text(
        json.dumps({"package_manifest_sha256": fingerprint}), encoding="utf-8"
    )
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    legacy_path = (
        workflow_home.parent
        / ".orion-runtime/realtime-business/source-project/0.1.1/deployment-binding.json"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps({**binding, "endpoint": "http://127.0.0.1:18081/sparql"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORION_S7_AUTO_DEPLOY_ROOT", str(deployment_root))
    return workflow_home, project_dir, binding_path, binding


def test_s6_managed_revision_uses_profile_binding_and_leaves_records_unchanged(
    managed_parent_release,
) -> None:
    workflow_home, project_dir, binding_path, binding = managed_parent_release
    records = {
        path: path.read_bytes()
        for path in workflow_home.parent.rglob("*.json")
    }

    resolved = resolve_base_release_runtime(
        workflow_home=workflow_home, project_dir=project_dir
    )

    assert resolved == {
        key: binding[key]
        for key in ("project_id", "release_version", "release_fingerprint", "endpoint")
    }
    assert binding_path in records
    assert {path: path.read_bytes() for path in records} == records


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_id", "another-project", "不可变发布记录"),
        ("release_version", "9.0.0", "不可变发布记录"),
        ("release_fingerprint", "sha256:" + "b" * 64, "不可变发布记录"),
        ("database_access_mode", "READ_WRITE", "不可变发布记录"),
        ("database_access_mode", None, "不可变发布记录"),
        ("endpoint", " \t ", "只读 Ontop endpoint"),
    ],
)
def test_s6_managed_revision_rejects_unbound_or_writable_parent_runtime(
    managed_parent_release, field, value, message,
) -> None:
    workflow_home, project_dir, binding_path, binding = managed_parent_release
    binding_path.write_text(json.dumps({**binding, field: value}), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        resolve_base_release_runtime(workflow_home=workflow_home, project_dir=project_dir)


def test_s6_explicit_profile_root_never_falls_back_to_a_different_runtime(
    managed_parent_release,
) -> None:
    workflow_home, project_dir, binding_path, _binding = managed_parent_release
    binding_path.unlink()

    with pytest.raises(FileNotFoundError):
        resolve_base_release_runtime(workflow_home=workflow_home, project_dir=project_dir)


def test_empty_source_uses_an_explicit_non_business_reasoning_probe() -> None:
    facts, rules, mode = build_reasoning_probe([])

    assert mode == "EMPTY_SOURCE_BOUNDARY"
    assert facts == ["RuntimeSourceEmpty(supply-chain-current)"]
    assert rules == ["IF RuntimeSourceEmpty(?x) THEN NoBusinessConclusion(?x)"]
    assert all("PurchaseOrder(" not in fact for fact in facts)


def test_populated_source_keeps_the_business_instance_probe() -> None:
    facts, rules, mode = build_reasoning_probe(
        [
            {"id": 7, "risk_level": "HIGH"},
            {"id": 8, "risk_level": "NORMAL"},
        ]
    )

    assert mode == "BUSINESS_INSTANCE_PROBE"
    assert facts == [
        "PurchaseOrder(po-7)",
        "PurchaseOrder(po-8)",
        "HighRiskOrder(po-7)",
    ]
    assert rules == [
        "IF HighRiskOrder(?x) THEN NeedsSupplyReview(?x)",
        "IF NeedsSupplyReview(?x) THEN TraceRequired(?x)",
    ]


def test_s6_requires_release_reasoning_rules_to_have_positive_negative_trace(
    tmp_path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "reasoning-project"
    runtime_dir = project_dir / "03-mapping-review/runtime"
    rules_dir = runtime_dir / "rules"
    rules_dir.mkdir(parents=True)
    rule_path = rules_dir / "concentration_analysis.json"
    rule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_name": "concentration_analysis",
                "rules": [
                    {
                        "rule_id": "RULE-CONCENTRATION-001",
                        "expression": "IF ThresholdExceeded(?x) THEN Concentrated(?x)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rule_sha256 = "sha256:" + hashlib.sha256(rule_path.read_bytes()).hexdigest()
    (runtime_dir / "runtime-source.json").write_text(
        json.dumps(
            {
                "reasoning_capabilities": {
                    "concentration_analysis": {
                        "execution_scope": "FULL_QUERY_RESULT",
                        "rule_artifact": "rules/concentration_analysis.json",
                        "rule_sha256": rule_sha256,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    valid = {
        "capability_name": "concentration_analysis",
        "status": "PASSED",
        "validation_scope": "FULL_SOURCE_VALIDATION",
        "rule_sha256": rule_sha256,
        "input_fact_count": 10,
        "result_fact_count": 2,
        "rules_fired": 1,
        "live_outcome_verified": True,
        "positive_case_count": 1,
        "negative_case_count": 1,
        "trace": [{"rule_id": "RULE-CONCENTRATION-001"}],
    }

    result = service._validate_s6_reasoning_capabilities(
        project_dir,
        {"reasoning_capability_results": [valid]},
    )

    assert result["declared"] == result["validated"] == 1
    assert result["capabilities"][0]["validation_scope"] == ("FULL_SOURCE_VALIDATION")

    representative = {**valid, "validation_scope": "REPRESENTATIVE_INSTANCE_PROBE"}
    with pytest.raises(WorkflowGateError, match="正例、反例和轨迹"):
        service._validate_s6_reasoning_capabilities(
            project_dir,
            {"reasoning_capability_results": [representative]},
        )

    invalid = {**valid, "negative_case_count": 0}
    with pytest.raises(WorkflowGateError, match="正例、反例和轨迹"):
        service._validate_s6_reasoning_capabilities(
            project_dir,
            {"reasoning_capability_results": [invalid]},
        )


def test_s6_runner_executes_versioned_rule_positive_and_negative_probes(
    tmp_path,
) -> None:
    project_dir = tmp_path / "reasoning-runner"
    runtime_dir = project_dir / "03-mapping-review/runtime"
    query_dir = runtime_dir / "queries"
    rules_dir = runtime_dir / "rules"
    query_dir.mkdir(parents=True)
    rules_dir.mkdir()
    (query_dir / "batch_status.rq").write_text(
        "SELECT ?batch ?status WHERE { ?batch <urn:status> ?status . }",
        encoding="utf-8",
    )
    (rules_dir / "concentration_analysis.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_id": "RULE-CONCENTRATION-001",
                        "expression": (
                            "IF AbnormalBatch(?batch) THEN NeedsConcentrationAnalysis(?batch)"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "runtime-source.json").write_text(
        json.dumps(
            {
                "ontop_queries": {"batch_status": {"path": "queries/batch_status.rq"}},
                "query_capabilities": {
                    "batch_status": {
                        "parameters": {},
                        "validation_cases": [{"parameters": {}}],
                    }
                },
                "reasoning_capabilities": {
                    "concentration_analysis": {
                        "evidence_query": "batch_status",
                        "fact_bindings": [
                            {
                                "predicate": "AbnormalBatch",
                                "arguments": [{"field": "batch"}],
                                "when": {"field": "status", "equals": "NG"},
                            }
                        ],
                        "result_predicates": ["NeedsConcentrationAnalysis"],
                        "rule_artifact": "rules/concentration_analysis.json",
                        "rule_sha256": "sha256:" + "7" * 64,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    graph = Graph()
    graph.add((URIRef("urn:batch:1"), URIRef("urn:status"), Literal("NG")))

    class FakeReasoner:
        def run_forward(self, *, facts, rules):
            if facts:
                return {
                    "inferred_facts": ["NeedsConcentrationAnalysis(urn:batch:1)"],
                    "rules_fired": 1,
                    "trace": [
                        {
                            "rule_id": "rule_1",
                            "premises": ["AbnormalBatch(urn:batch:1)"],
                            "conclusion": ("NeedsConcentrationAnalysis(urn:batch:1)"),
                        }
                    ],
                }
            return {"inferred_facts": [], "rules_fired": 0, "trace": []}

    results = validate_release_reasoning_capabilities(
        project_dir=project_dir,
        graph=graph,
        semantica=FakeReasoner(),  # type: ignore[arg-type]
    )

    assert results[0]["status"] == "PASSED"
    assert results[0]["positive_case_count"] == 1
    assert results[0]["negative_case_count"] == 1
    assert results[0]["validation_scope"] == "FULL_SOURCE_VALIDATION"
    assert results[0]["derived_facts"] == ["NeedsConcentrationAnalysis(urn:batch:1)"]


def test_s6_materializes_rule_conclusions_for_cq_replay(tmp_path) -> None:
    project_dir = tmp_path / "reasoning-materialization"
    runtime_dir = project_dir / "03-mapping-review/runtime"
    runtime_dir.mkdir(parents=True)
    result_iri = "https://example.com/ontology/NeedsReview"
    (runtime_dir / "runtime-source.json").write_text(
        json.dumps(
            {"reasoning_capabilities": {"review": {"ontology_terms": {"NeedsReview": result_iri}}}}
        ),
        encoding="utf-8",
    )

    graph, count = materialize_reasoning_result_graph(
        project_dir,
        [
            {
                "capability_name": "review",
                "derived_facts": ["NeedsReview(case-001)"],
            }
        ],
    )

    subject = URIRef("urn:orion:document-fact:" + hashlib.sha256(b"case-001").hexdigest())
    assert count == 1
    assert (subject, RDF.type, URIRef(result_iri)) in graph


def test_s6_materializes_database_rule_conclusion_on_original_ontop_iri(
    tmp_path,
) -> None:
    project_dir = tmp_path / "reasoning-materialization"
    runtime_dir = project_dir / "03-mapping-review/runtime"
    runtime_dir.mkdir(parents=True)
    result_iri = "https://example.com/ontology/HighRiskTransaction"
    source_iri = "https://example.com/ontology/transaction-10000"
    safe_symbol = "https:__example.com_ontology_transaction-10000"
    (runtime_dir / "runtime-source.json").write_text(
        json.dumps(
            {
                "reasoning_capabilities": {
                    "high_risk": {"ontology_terms": {"HighRiskTransaction": result_iri}}
                }
            }
        ),
        encoding="utf-8",
    )

    graph, count = materialize_reasoning_result_graph(
        project_dir,
        [
            {
                "capability_name": "high_risk",
                "derived_facts": [f"HighRiskTransaction({safe_symbol})"],
                "_symbol_table": {safe_symbol: source_iri},
            }
        ],
    )

    assert count == 1
    assert (URIRef(source_iri), RDF.type, URIRef(result_iri)) in graph


def test_s6_accepts_empty_semantica_only_when_s1_proves_the_source_is_empty(
    tmp_path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "empty-source-project"
    stage_dir = project_dir / "01-data-understanding"
    stage_dir.mkdir(parents=True)
    (stage_dir / "data-profile.json").write_text(
        json.dumps(
            {
                "status": "EMPTY_SOURCE",
                "table_count": 2,
                "empty_table_count": 2,
                "total_row_count": 0,
            }
        ),
        encoding="utf-8",
    )

    mode = service._validate_s6_semantica_source_mode(
        project_dir,
        {
            "engine": "SEMANTICA_MCP",
            "run_id": "semantica-empty-1",
            "instance_count": 0,
            "source_row_count": 0,
            "relationship_count": 0,
            "reasoning_probe_mode": "EMPTY_SOURCE_BOUNDARY",
        },
    )

    assert mode == "EMPTY_SOURCE_BOUNDARY"


def test_s6_accepts_production_profile_with_exact_zero_rows(tmp_path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "production-empty-source-project"
    stage_dir = project_dir / "01-data-understanding"
    stage_dir.mkdir(parents=True)
    (stage_dir / "data-profile.json").write_text(
        json.dumps(
            {
                "profile_mode": "FULL_IMPORT_WITH_EXACT_COUNTS",
                "table_count": 2,
                "empty_table_count": 2,
                "total_rows": 0,
            }
        ),
        encoding="utf-8",
    )

    mode = service._validate_s6_semantica_source_mode(
        project_dir,
        {
            "engine": "SEMANTICA_MCP",
            "run_id": "semantica-empty-production-1",
            "instance_count": 0,
            "source_row_count": 0,
            "relationship_count": 0,
            "reasoning_probe_mode": "FULL_SOURCE_RULE_PACKAGE",
            "materialization_scope": "FULL_SOURCE_VALIDATION",
        },
    )

    assert mode == "EMPTY_SOURCE_BOUNDARY"


def test_s6_rejects_empty_semantica_when_s1_contains_business_rows(tmp_path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "non-empty-source-project"
    stage_dir = project_dir / "01-data-understanding"
    stage_dir.mkdir(parents=True)
    (stage_dir / "data-profile.json").write_text(
        json.dumps(
            {
                "status": "PROFILED",
                "table_count": 2,
                "empty_table_count": 1,
                "total_row_count": 4,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowGateError, match="缺少真实实例验证"):
        service._validate_s6_semantica_source_mode(
            project_dir,
            {
                "engine": "SEMANTICA_MCP",
                "run_id": "semantica-empty-2",
                "instance_count": 0,
                "source_row_count": 0,
                "relationship_count": 0,
                "reasoning_probe_mode": "EMPTY_SOURCE_BOUNDARY",
            },
        )


def test_s6_snapshot_hub_binding_scopes_shared_tables_to_promoted_dataset() -> None:
    inventory = _snapshot_hub_inventory_without_principal()
    inventory["cross_source_snapshot_set"] = {
        "source_snapshots": [
            {
                "dataset_id": "DS-52AA004FD81C53E47CB31BC9",
                "tables": [{"table": "plant", "target_table": "ms_src_0001"}],
            }
        ]
    }
    binding = resolve_s6_source_binding(
        inventory,
        "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
        catalog_readonly_verifier=lambda _principal: None,
    )
    assert binding["dataset_filters"] == {"ms_src_0001": "DS-52AA004FD81C53E47CB31BC9"}

    queries: list[str] = []

    class Rows:
        def __enter__(self):
            return iter(())

        def __exit__(self, *_args):
            return False

    class Connection:
        def execution_options(self, **_options):
            return self

        def execute(self, query):
            queries.append(str(query))
            return Rows()

    scoped = source_content_fingerprint(
        Connection(), binding["relations"], binding["dataset_filters"]
    )
    unscoped = source_content_fingerprint(Connection(), binding["relations"])
    assert "WHERE dataset_id = 'DS-52AA004FD81C53E47CB31BC9'" in queries[0]
    assert "WHERE" not in queries[1]
    assert scoped != unscoped
    with pytest.raises(ValueError, match="unsafe snapshot dataset id"):
        source_content_fingerprint(
            Connection(), binding["relations"], {"ms_src_0001": "DS-1' OR '1'='1"}
        )


def test_s6_snapshot_hub_binding_rejects_scope_without_promoted_dataset() -> None:
    inventory = _snapshot_hub_inventory_without_principal()
    inventory["business_tables_scope"] = ["ms_src_0001", "ms_src_0002"]
    inventory["cross_source_snapshot_set"] = {
        "source_snapshots": [
            {
                "dataset_id": "DS-52AA004FD81C53E47CB31BC9",
                "tables": [{"table": "plant", "target_table": "ms_src_0001"}],
            }
        ]
    }
    with pytest.raises(RuntimeError, match="S6_SOURCE_DATASET_MISSING"):
        resolve_s6_source_binding(
            inventory,
            "postgresql://orion_source_reader:secret@127.0.0.1/orion_source_data",
            catalog_readonly_verifier=lambda _principal: None,
        )
