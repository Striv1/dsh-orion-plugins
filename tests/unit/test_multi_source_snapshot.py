from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.ontology_engineering.workflow import (
    OntologyWorkflowService,
    WorkflowGateError,
)
from services.structured_data.multi_source import (
    EntityResolutionContract,
    IdentityRowReference,
    MultiSourceContractError,
    SourceBinding,
    SourceColumnProfile,
    SourceMappingFragment,
    SourceMappingTerm,
    SourceProfileReceipt,
    SourceSnapshotManifest,
    SourceTableProfile,
    TableSnapshot,
    build_cross_source_snapshot_set,
    merge_source_mapping_fragments,
)
from services.structured_data.semantic_projection import (
    ProjectedDataProperty,
    SnapshotEntityProjection,
    SnapshotSemanticProjection,
    render_snapshot_semantic_artifacts,
)
from services.structured_data.snapshot_hub import SnapshotHub, resolve_connection_ref


def _binding(source_id: str, engine: str, table: str) -> SourceBinding:
    return SourceBinding(
        project_id="ontology-project-test",
        source_id=source_id,
        engine=engine,
        connection_ref=f"secret://orion/{source_id}",
        database=f"db_{source_id}",
        schemas=["public"],
        authorized_tables=[table],
        readonly_attested=True,
        pii_scope="STABLE_HASHED_IDENTIFIER_ONLY",
        owner="platform-test",
    )


def _snapshot(
    source_id: str,
    engine: str,
    table: str,
    *,
    captured_at: datetime,
    count: int = 2,
    complete: bool = True,
    dataset_type: str = "TEST_ONLY",
) -> SourceSnapshotManifest:
    return SourceSnapshotManifest(
        project_id="ontology-project-test",
        source_id=source_id,
        engine=engine,
        dataset_id=f"DS-{source_id.upper().replace('_', '-')}-00000001",
        snapshot_version="v1",
        captured_at=captured_at,
        snapshot_complete=complete,
        source_sha256="sha256:" + "a" * 64,
        pii_scope="STABLE_HASHED_IDENTIFIER_ONLY",
        dataset_type=dataset_type,
        production_evidence=dataset_type == "PRODUCTION",
        tables=[
            TableSnapshot(
                table=table,
                target_table=f"snap_{source_id}_{table}".replace("-", "_"),
                source_row_count=count,
                snapshot_row_count=count,
                schema_sha256="sha256:" + "b" * 64,
                source_locator=f"{source_id}:{table}@v1",
            )
        ],
    )


def test_postgres_and_mysql_snapshots_promote_atomically_as_test_only() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    bindings = [
        _binding("employee_pg", "POSTGRESQL", "employees"),
        _binding("application_mysql", "MYSQL", "applications"),
    ]
    snapshots = [
        _snapshot("employee_pg", "POSTGRESQL", "employees", captured_at=observed),
        _snapshot(
            "application_mysql",
            "MYSQL",
            "applications",
            captured_at=observed + timedelta(seconds=20),
        ),
    ]

    result = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=bindings,
        snapshots=snapshots,
    )

    assert result.snapshot_complete is True
    assert result.production_evidence is False
    assert [item.source_id for item in result.source_snapshots] == [
        "application_mysql",
        "employee_pg",
    ]


def test_single_source_snapshot_uses_the_same_atomic_version_binding() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    binding = _binding("fraud_pg", "POSTGRESQL", "transaction")
    snapshot = _snapshot(
        "fraud_pg",
        "POSTGRESQL",
        "transaction",
        captured_at=observed,
        dataset_type="PRODUCTION",
    )

    result = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=[binding],
        snapshots=[snapshot],
    )

    assert result.snapshot_complete is True
    assert result.production_evidence is True
    assert [item.source_id for item in result.source_snapshots] == ["fraud_pg"]


@pytest.mark.parametrize(
    ("mutation", "gate"),
    [
        ("missing_source", "G-S1-SNAPSHOT-COMPLETE"),
        ("unauthorized_table", "G-S1-SOURCE-SCOPE"),
        ("time_skew", "G-S1-CROSS-SOURCE-TIME-CONSISTENCY"),
    ],
)
def test_snapshot_set_fails_closed(mutation: str, gate: str) -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    bindings = [
        _binding("employee_pg", "POSTGRESQL", "employees"),
        _binding("application_mysql", "MYSQL", "applications"),
    ]
    snapshots = [
        _snapshot("employee_pg", "POSTGRESQL", "employees", captured_at=observed),
        _snapshot("application_mysql", "MYSQL", "applications", captured_at=observed),
    ]
    if mutation == "missing_source":
        snapshots.pop()
    elif mutation == "unauthorized_table":
        snapshots[1] = _snapshot("application_mysql", "MYSQL", "secret_table", captured_at=observed)
    else:
        snapshots[1] = _snapshot(
            "application_mysql",
            "MYSQL",
            "applications",
            captured_at=observed + timedelta(minutes=30),
        )

    with pytest.raises(MultiSourceContractError, match=gate):
        build_cross_source_snapshot_set(
            project_id="ontology-project-test",
            bindings=bindings,
            snapshots=snapshots,
        )


def test_incomplete_source_never_constructs_a_snapshot_manifest() -> None:
    with pytest.raises(ValueError, match="G-S1-SNAPSHOT-COMPLETE"):
        _snapshot(
            "employee_pg",
            "POSTGRESQL",
            "employees",
            captured_at=datetime(2026, 9, 4, tzinfo=UTC),
            complete=False,
        )


def test_readonly_and_production_evidence_are_hard_contracts() -> None:
    with pytest.raises(ValueError, match="G-S1-SOURCE-READONLY"):
        SourceBinding(
            project_id="ontology-project-test",
            source_id="unsafe_pg",
            engine="POSTGRESQL",
            connection_ref="env://UNSAFE_URL",
            database="unsafe",
            schemas=["public"],
            authorized_tables=["employees"],
            readonly_attested=False,
            pii_scope="NONE",
            owner="platform-test",
        )


def test_source_binding_rejects_columns_outside_table_scope() -> None:
    with pytest.raises(ValueError, match="G-S1-SOURCE-SCOPE"):
        SourceBinding(
            project_id="ontology-project-test",
            source_id="employee_pg",
            engine="POSTGRESQL",
            connection_ref="env://PG_SOURCE_URL",
            database="employees",
            schemas=["public"],
            authorized_tables=["employees"],
            authorized_columns={"secret_table": ["id"]},
            readonly_attested=True,
            pii_scope="STABLE_HASHED_IDENTIFIER_ONLY",
            owner="platform-test",
        )


def test_entity_resolution_forbids_name_based_identity() -> None:
    with pytest.raises(ValueError, match="G-S3-CROSS-SOURCE-IDENTITY"):
        EntityResolutionContract(
            contract_id="ER-EMPLOYEE-0001",
            project_id="ontology-project-test",
            canonical_entity="Employee",
            source_tables={
                "employee_pg": "employees",
                "application_mysql": "applications",
            },
            source_join_keys={
                "employee_pg": ["full_name"],
                "application_mysql": ["applicant_name"],
            },
            normalization="TRIM_UPPER",
            cardinality="ONE_TO_ONE",
            collision_policy="FAIL",
            source_authority=["employee_pg"],
            freshness_policy="SNAPSHOT_SET",
            pii_policy="STABLE_HASHED_IDENTIFIER_ONLY",
        )


def test_explicit_hashed_identity_contract_is_accepted() -> None:
    contract = EntityResolutionContract(
        contract_id="ER-EMPLOYEE-0002",
        project_id="ontology-project-test",
        canonical_entity="Employee",
        source_tables={
            "employee_pg": "employees",
            "application_mysql": "applications",
        },
        source_join_keys={
            "employee_pg": ["employee_id_hash"],
            "application_mysql": ["applicant_id_hash"],
        },
        normalization="STABLE_HASH",
        cardinality="ONE_TO_ONE",
        collision_policy="FAIL",
        source_authority=["employee_pg"],
        freshness_policy="SNAPSHOT_SET",
        pii_policy="STABLE_HASHED_IDENTIFIER_ONLY",
    )
    assert contract.normalization == "STABLE_HASH"


def test_identity_resolution_rejects_duplicate_one_to_one_key() -> None:
    contract = EntityResolutionContract(
        contract_id="ER-EMPLOYEE-0004",
        project_id="ontology-project-test",
        canonical_entity="Employee",
        source_tables={"employee_pg": "employees", "application_mysql": "applications"},
        source_join_keys={
            "employee_pg": ["employee_id_hash"],
            "application_mysql": ["applicant_id_hash"],
        },
        normalization="STABLE_HASH",
        cardinality="ONE_TO_ONE",
        collision_policy="FAIL",
        source_authority=["employee_pg", "application_mysql"],
        freshness_policy="SNAPSHOT_SET",
        pii_policy="STABLE_HASHED_IDENTIFIER_ONLY",
    )
    reference = IdentityRowReference(
        source_id="employee_pg",
        dataset_id="DS-EMPLOYEE-0004",
        table="employees",
        row_ordinal=1,
        row_sha256="sha256:" + "a" * 64,
    )
    with pytest.raises(MultiSourceContractError, match="duplicate key"):
        SnapshotHub._validate_identity_cardinality(
            {
                "employee_pg": {"HASH-A001": [reference, reference.model_copy()]},
                "application_mysql": {},
            },
            contract,
        )


def test_mapping_fragments_are_bound_to_every_snapshot_source() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    snapshot_set = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=[
            _binding("employee_pg", "POSTGRESQL", "employees"),
            _binding("application_mysql", "MYSQL", "applications"),
        ],
        snapshots=[
            _snapshot("employee_pg", "POSTGRESQL", "employees", captured_at=observed),
            _snapshot("application_mysql", "MYSQL", "applications", captured_at=observed),
        ],
        now=observed,
    )
    fragments = []
    for source_id, table in (
        ("employee_pg", "employees"),
        ("application_mysql", "applications"),
    ):
        fragments.append(
            SourceMappingFragment(
                project_id="ontology-project-test",
                source_id=source_id,
                snapshot_version="v1",
                terms=[
                    SourceMappingTerm(
                        term_iri=f"https://orion.local/ontology/{source_id}",
                        kind="CLASS",
                        source_id=source_id,
                        table=table,
                        columns=["id"],
                        source_refs=[f"{source_id}:{table}"],
                    )
                ],
                obda_mappings=[f"mappingId MAP-{source_id}"],
            )
        )
    unified = merge_source_mapping_fragments(
        snapshot_set=snapshot_set,
        fragments=fragments,
    )
    assert unified.snapshot_set_id == snapshot_set.snapshot_set_id
    assert unified.source_ids == ["application_mysql", "employee_pg"]


def test_mapping_property_requires_domain_and_range() -> None:
    with pytest.raises(ValueError, match="G-S3-SOURCE-MAPPING-COVERAGE"):
        SourceMappingTerm(
            term_iri="https://orion.local/ontology/hasApplication",
            kind="OBJECT_PROPERTY",
            source_id="employee_pg",
            table="employees",
            columns=["employee_id"],
            source_refs=["employee_pg:employees.employee_id"],
        )


def test_snapshot_semantic_projection_binds_identity_relationship_and_sources() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    snapshot_set = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=[
            _binding("employee_pg", "POSTGRESQL", "employees"),
            _binding("application_mysql", "MYSQL", "applications"),
        ],
        snapshots=[
            _snapshot("employee_pg", "POSTGRESQL", "employees", captured_at=observed),
            _snapshot("application_mysql", "MYSQL", "applications", captured_at=observed),
        ],
        now=observed,
    )
    identity = EntityResolutionContract(
        contract_id="ER-EMPLOYEE-0003",
        project_id="ontology-project-test",
        canonical_entity="Employee",
        source_tables={
            "employee_pg": "employees",
            "application_mysql": "applications",
        },
        source_join_keys={
            "employee_pg": ["employee_id_hash"],
            "application_mysql": ["applicant_id"],
        },
        normalization="STABLE_HASH",
        cardinality="ONE_TO_ONE",
        collision_policy="FAIL",
        source_authority=["employee_pg", "application_mysql"],
        freshness_policy="SNAPSHOT_SET",
        pii_policy="STABLE_HASHED_IDENTIFIER_ONLY",
    )
    projection = SnapshotSemanticProjection(
        project_id="ontology-project-test",
        snapshot_set_id=snapshot_set.snapshot_set_id,
        identity_contract_id=identity.contract_id,
        ontology_iri="https://orion.local/ontology/multi-source-test",
        projections=[
            SnapshotEntityProjection(
                source_id="employee_pg",
                class_iri="https://orion.local/ontology/multi-source-test#Employee",
                role="CANONICAL",
                data_properties=[
                    ProjectedDataProperty(
                        column="department_code",
                        property_iri=(
                            "https://orion.local/ontology/multi-source-test#departmentCode"
                        ),
                    )
                ],
            ),
            SnapshotEntityProjection(
                source_id="application_mysql",
                class_iri="https://orion.local/ontology/multi-source-test#Application",
                role="RELATED",
                iri_key_column="applicant_id",
                resource_iri_prefix="https://orion.local/resource/application-",
                relation_from_canonical_iri=(
                    "https://orion.local/ontology/multi-source-test#hasApplication"
                ),
                data_properties=[
                    ProjectedDataProperty(
                        column="service_item_id",
                        property_iri=(
                            "https://orion.local/ontology/multi-source-test#serviceItemId"
                        ),
                    )
                ],
            ),
        ],
    )
    artifacts = render_snapshot_semantic_artifacts(
        snapshot_set=snapshot_set,
        identity_contract=identity,
        projection=projection,
    )
    assert artifacts.source_ids == ["application_mysql", "employee_pg"]
    assert artifacts.object_property_count == 1
    assert f"link.snapshot_set_id='{snapshot_set.snapshot_set_id}'" in artifacts.mapping_obda
    assert "hasApplication" in artifacts.mapping_obda
    assert "owl:ObjectProperty" in artifacts.ontology_ttl


def test_unresolved_secret_reference_fails_closed() -> None:
    with pytest.raises(MultiSourceContractError, match="SOURCE_NOT_BOUND"):
        resolve_connection_ref("secret://vault/mysql/customer")


def _profile(binding: SourceBinding, table: str, observed: datetime) -> SourceProfileReceipt:
    return SourceProfileReceipt(
        project_id=binding.project_id,
        source_id=binding.source_id,
        engine=binding.engine,
        profiled_at=observed,
        database=binding.database,
        readonly_verified=True,
        tables=[
            SourceTableProfile(
                table=table,
                row_count=2,
                columns=[
                    SourceColumnProfile(
                        name="id",
                        data_type="text",
                        nullable=False,
                        ordinal_position=1,
                    )
                ],
                schema_sha256="sha256:" + "c" * 64,
            )
        ],
        schema_fingerprint="sha256:" + "d" * 64,
        receipt_sha256="sha256:" + "e" * 64,
    )


def test_workflow_multi_source_gate_accepts_verified_production_snapshot() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    bindings = [
        _binding("employee_pg", "POSTGRESQL", "employees"),
        _binding("application_mysql", "MYSQL", "applications"),
    ]
    snapshots = [
        _snapshot(
            "employee_pg",
            "POSTGRESQL",
            "employees",
            captured_at=observed,
            dataset_type="PRODUCTION",
        ),
        _snapshot(
            "application_mysql",
            "MYSQL",
            "applications",
            captured_at=observed,
            dataset_type="PRODUCTION",
        ),
    ]
    snapshot_set = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=bindings,
        snapshots=snapshots,
    )
    OntologyWorkflowService._validate_multi_source_s1(
        inventory={
            "datasource_count": 2,
            "source_bindings": [item.model_dump(mode="json") for item in bindings],
            "cross_source_snapshot_set": snapshot_set.model_dump(mode="json"),
        },
        profile={
            "source_profiles": [
                _profile(binding, table, observed).model_dump(mode="json")
                for binding, table in zip(bindings, ["employees", "applications"], strict=True)
            ]
        },
        project_id="ontology-project-test",
    )


def test_workflow_multi_source_gate_rejects_test_only_snapshot() -> None:
    observed = datetime(2026, 9, 4, tzinfo=UTC)
    bindings = [
        _binding("employee_pg", "POSTGRESQL", "employees"),
        _binding("application_mysql", "MYSQL", "applications"),
    ]
    snapshot_set = build_cross_source_snapshot_set(
        project_id="ontology-project-test",
        bindings=bindings,
        snapshots=[
            _snapshot("employee_pg", "POSTGRESQL", "employees", captured_at=observed),
            _snapshot("application_mysql", "MYSQL", "applications", captured_at=observed),
        ],
    )
    with pytest.raises(WorkflowGateError) as exc_info:
        OntologyWorkflowService._validate_multi_source_s1(
            inventory={
                "datasource_count": 2,
                "source_bindings": [item.model_dump(mode="json") for item in bindings],
                "cross_source_snapshot_set": snapshot_set.model_dump(mode="json"),
            },
            profile={
                "source_profiles": [
                    _profile(binding, table, observed).model_dump(mode="json")
                    for binding, table in zip(bindings, ["employees", "applications"], strict=True)
                ]
            },
            project_id="ontology-project-test",
        )
    assert exc_info.value.gate_id == "G-S7-PRODUCTION-EVIDENCE"
