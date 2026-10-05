from __future__ import annotations

from copy import deepcopy

import pytest

from services.ontology_engineering.source_scope import (
    SourceScopeError,
    normalize_source_scope,
    validate_database_sources,
    validate_document_sources,
)

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


def scope(mode="DATABASE_ONLY", sources=None, **extra):
    return normalize_source_scope(
        intake_mode=mode, source_scope={"sources": sources or [], **extra}
    )


def database(source_id="erp", database="erp_db", tables=None, **extra):
    return {
        "kind": "DATABASE",
        "source_id": source_id,
        "database": database,
        "schemas": ["public"],
        "authorized_tables": tables or ["public.orders"],
        **extra,
    }


def snapshot_payload(sources=None):
    """The actual SnapshotHub handoff shape: storage database differs from origin."""
    sources = sources or [database()]
    bindings, profiles, tables, snapshots = [], [], [], []
    for index, source in enumerate(sources):
        source_id = source["source_id"]
        binding = {k: deepcopy(v) for k, v in source.items() if k != "kind"}
        bindings.append(binding)
        origin = []
        for j, table in enumerate(source["authorized_tables"]):
            target = f"snapshot_{index}_{j}"
            columns = ["id", "amount"]
            tables.append(
                {
                    "name": target,
                    "source_id": source_id,
                    "source_table": table,
                    "dataset_id": f"DS-{index}",
                    "columns": ["dataset_id", "row_ordinal", *columns],
                }
            )
            origin.append(
                {"table": table, "columns": [{"name": c} for c in columns], "row_count": 10}
            )
        profiles.append({"source_id": source_id, "database": source["database"], "tables": origin})
        snapshots.append(
            {
                "source_id": source_id,
                "dataset_id": f"DS-{index}",
                "status": "READY",
                "source_sha256": HASH,
            }
        )
    targets = [t["name"] for t in tables]
    return {
        "datasource_inventory": {
            "datasources": [
                {"source_id": b["source_id"], "database": "orion_source_data"} for b in bindings
            ],
            "source_bindings": bindings,
            "datasets": snapshots,
            "business_tables_scope": targets,
        },
        "schema_snapshot": {
            "database": "orion_source_data",
            "schemas": ["orion_data"],
            "tables": tables,
        },
        "data_profile": {"source_profiles": profiles},
        "evidence_sql": [{"source_tables": targets, "executed_via": "ORION_SNAPSHOT_HUB"}],
    }


def document(name="rules.pdf", **extra):
    return {
        "document_id": "DOC-001",
        "source_name": name,
        "source_path": f".orion-s0-uploads/REFERENCE-01/{name}",
        "source_sha256": HASH,
        **extra,
    }


def test_missing_scope_keeps_legacy_hints_unresolved_without_inventing_sources():
    result = normalize_source_scope(
        intake_mode="DATABASE_ONLY", datasource_label="ERP 与 CRM", table_scope=["orders"]
    )
    assert result["sources"] == []
    assert result["scope_status"] == "UNRESOLVED"
    assert result["legacy_hints"]["datasource_label"] == "ERP 与 CRM"
    observed = validate_database_sources(result, snapshot_payload())
    assert observed["scope_status"] == "OBSERVED"
    assert observed["observed_sources"][0]["database"] == "erp_db"
    assert result["scope_status"] == "UNRESOLVED"  # Pure validation does not rewrite authorization.


@pytest.mark.parametrize(
    "mode,source",
    [
        ("DATABASE_ONLY", {"kind": "DOCUMENT", "source_path": "file.pdf"}),
        ("DATABASE_ONLY", {"kind": "STRUCTURED_FILE", "source_path": "file.csv"}),
        ("DOCUMENT_ONLY", database()),
    ],
)
def test_intake_mode_rejects_incompatible_source_kinds(mode, source):
    with pytest.raises(SourceScopeError):
        scope(mode, [source])


def test_multi_database_identity_and_original_table_coverage_are_preserved():
    declared = [database(), database("crm", "crm_db", ["public.customers"])]
    normalized = scope(sources=declared)
    report = validate_database_sources(normalized, snapshot_payload(declared))
    assert {s["database"] for s in report["observed_sources"]} == {"erp_db", "crm_db"}
    assert len(report["observed_sources"]) == 2
    with pytest.raises(SourceScopeError, match="全部闭合"):
        validate_database_sources(normalized, snapshot_payload(declared[:1]))
    leaked = [*declared, database("hr", "hr_db", ["public.staff"])]
    with pytest.raises(SourceScopeError, match="未被唯一授权"):
        validate_database_sources(normalized, snapshot_payload(leaked))


@pytest.mark.parametrize("mutation", ["database", "table", "schema", "sql", "unowned"])
def test_explicit_scope_rejects_source_and_table_leakage(mutation):
    payload = snapshot_payload()
    if mutation == "database":
        payload["datasource_inventory"]["source_bindings"][0]["database"] = "other_db"
    elif mutation == "table":
        payload["schema_snapshot"]["tables"][0]["source_table"] = "public.payroll"
    elif mutation == "schema":
        payload["datasource_inventory"]["source_bindings"][0]["schemas"] = ["private"]
    elif mutation == "sql":
        payload["evidence_sql"].append({"source_tables": ["payroll"]})
    else:
        payload["schema_snapshot"]["tables"].append({"name": "hidden", "source_id": "unowned"})
    with pytest.raises(SourceScopeError):
        validate_database_sources(scope(sources=[database()]), payload)


def test_excluded_table_is_not_required_but_cannot_be_read():
    declared = database(
        tables=["public.orders", "public.payroll"], excluded_tables=["public.payroll"]
    )
    validate_database_sources(scope(sources=[declared]), snapshot_payload())
    with pytest.raises(SourceScopeError, match="排除项"):
        validate_database_sources(scope(sources=[declared]), snapshot_payload([declared]))


def test_original_column_allowlist_ignores_generated_snapshot_metadata():
    allowed = database(authorized_columns={"public.orders": ["id", "amount"]})
    validate_database_sources(scope(sources=[allowed]), snapshot_payload())
    narrowed = database(authorized_columns={"public.orders": ["id"]})
    with pytest.raises(SourceScopeError, match="来源列超出"):
        validate_database_sources(scope(sources=[narrowed]), snapshot_payload())
    forbidden = database(excluded_columns={"public.orders": ["amount"]})
    with pytest.raises(SourceScopeError, match="来源列命中"):
        validate_database_sources(scope(sources=[forbidden]), snapshot_payload())


def test_unqualified_table_cannot_cross_schema_boundary():
    payload = {
        "datasource_inventory": {"datasources": [{"source_id": "erp", "database": "erp_db"}]},
        "schema_snapshot": {"tables": [{"name": "orders", "schema": "private", "columns": ["id"]}]},
    }
    with pytest.raises(SourceScopeError, match="schema"):
        validate_database_sources(scope(sources=[database(tables=["orders"])]), payload)


def test_planning_without_tables_is_allowed_but_s1_does_not_grant_all_tables():
    normalized = scope(sources=[{"kind": "DATABASE", "source_id": "erp"}])
    assert normalized["scope_status"] == "PARTIAL"
    with pytest.raises(SourceScopeError, match="空列表不代表全部授权"):
        validate_database_sources(normalized, snapshot_payload())
    with pytest.raises(SourceScopeError, match="未被唯一授权"):
        validate_database_sources(scope(), snapshot_payload())


def test_document_directory_authorization_has_path_boundaries_and_exclusions():
    declared = {
        "kind": "DOCUMENT",
        "source_path": ".orion-s0-uploads/REFERENCE-01",
        "path_scope": "DIRECTORY",
    }
    normalized = scope(
        "DOCUMENT_ONLY",
        [declared],
        excluded_source_refs=[".orion-s0-uploads/REFERENCE-01/private/"],
    )
    validate_document_sources(normalized, [document()])
    with pytest.raises(SourceScopeError, match="未被唯一授权"):
        validate_document_sources(
            normalized, [document(source_path=".orion-s0-uploads/REFERENCE-012/rules.pdf")]
        )
    with pytest.raises(SourceScopeError, match="排除项"):
        validate_document_sources(
            normalized, [document(source_path=".orion-s0-uploads/REFERENCE-01/private/rules.pdf")]
        )
    with pytest.raises(SourceScopeError, match="上级目录"):
        validate_document_sources(
            normalized, [document(source_path=".orion-s0-uploads/REFERENCE-01/../other.pdf")]
        )


def test_document_hash_and_complete_declared_set_are_checked():
    declared = [
        {"kind": "DOCUMENT", "source_path": document()["source_path"], "source_sha256": HASH},
        {"kind": "DOCUMENT", "source_path": ".orion-s0-uploads/REFERENCE-01/missing.pdf"},
    ]
    with pytest.raises(SourceScopeError, match="全部处理"):
        validate_document_sources(scope("DOCUMENT_ONLY", declared), [document()])
    with pytest.raises(SourceScopeError, match="未被唯一授权"):
        validate_document_sources(
            scope("DOCUMENT_ONLY", declared[:1]), [document(source_sha256=OTHER_HASH)]
        )
    with pytest.raises(SourceScopeError, match="禁止提交文档"):
        validate_document_sources(normalize_source_scope(intake_mode="DATABASE_ONLY"), [document()])


def file_handoff():
    doc = document("facts.csv")
    payload = {
        "datasource_inventory": {
            "datasources": [
                {
                    "id": "orion_source_data",
                    "database": "orion_source_data",
                    "label": "ORION 文件结构化数据区",
                }
            ],
            "datasets": [
                {
                    "dataset_id": "DS-FILE",
                    "document_id": "logical-file-id",
                    "s0_document_id": doc["document_id"],
                    "source_name": doc["source_name"],
                    "source_sha256": doc["source_sha256"],
                    "status": "READY",
                }
            ],
            "business_tables_scope": ["current_sheet_1"],
        },
        "schema_snapshot": {
            "database": "orion_source_data",
            "tables": [
                {
                    "table": "current_sheet_1",
                    "source_ref": "dataset:DS-FILE:sheet:订单",
                    "source_sheet": "订单",
                    "columns": ["id"],
                }
            ],
        },
        "evidence_sql": [
            {"source_tables": ["current_sheet_1"], "executed_via": "ORION_STRUCTURED_DATA_PIPELINE"}
        ],
    }
    return doc, payload


def test_file_import_is_validated_as_file_lineage_not_an_external_database():
    doc, payload = file_handoff()
    normalized = scope(
        "DOCUMENT_ONLY",
        [
            {
                "kind": "STRUCTURED_FILE",
                "source_path": doc["source_path"],
                "authorized_tables": ["订单"],
            }
        ],
    )
    report = validate_database_sources(normalized, payload, documents=[doc])
    assert report["observed_sources"][0]["kind"] == "STRUCTURED_FILE"
    with pytest.raises(SourceScopeError, match="未被唯一授权"):
        validate_database_sources(
            normalized, payload
        )  # Inventory alone lacks the declared source path.
    with pytest.raises(SourceScopeError, match="文件导入"):
        validate_database_sources(
            normalize_source_scope(intake_mode="DATABASE_ONLY"), payload, documents=[doc]
        )
    payload["datasource_inventory"]["datasources"].append({"id": "hidden", "database": "hr_db"})
    with pytest.raises(SourceScopeError, match="外部数据库"):
        validate_database_sources(normalized, payload, documents=[doc])


def test_hybrid_can_plan_both_chains_but_each_stage_requires_real_coverage():
    normalized = scope(
        "HYBRID", [database(), {"kind": "DOCUMENT", "source_path": document()["source_path"]}]
    )
    validate_document_sources(normalized, [document()])
    validate_database_sources(normalized, snapshot_payload())
    with pytest.raises(SourceScopeError, match="实际闭合"):
        validate_document_sources(normalized, [])
    with pytest.raises(SourceScopeError, match="实际闭合"):
        validate_database_sources(normalized, {})


def test_source_id_cannot_be_reused_for_two_databases():
    with pytest.raises(SourceScopeError, match="source_id 重复"):
        scope(sources=[database(), database(database="crm_db")])


def test_mcp_scope_schema_and_create_argument_passthrough(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator

    from harness.orion_workflow_mcp import TOOLS, OrionWorkflowTools
    from services.ontology_engineering import OntologyWorkflowService

    create_schema = next(t["inputSchema"] for t in TOOLS if t["name"] == "create_ontology_project")
    raw_scope = {"business_goal": "识别影响交付的订单", "sources": [database()]}
    arguments = {
        "project_name": "范围验收",
        "domain": "scope-test",
        "intake_mode": "DATABASE_ONLY",
        "source_scope": raw_scope,
    }
    Draft202012Validator(create_schema).validate(arguments)
    received = {}
    service = OntologyWorkflowService(tmp_path / "workflows")

    def capture(**kwargs):
        received.update(kwargs)
        return {
            "project_id": "scope-test",
            "current_stage": "S0",
            "project_status": "IN_PROGRESS",
            "revision": 1,
        }

    monkeypatch.setattr(service, "create_project", capture)
    monkeypatch.setattr(service, "record_document_understanding", capture, raising=False)
    monkeypatch.setenv("ORION_REQUIRE_UI_CREATE_CONFIRMATION", "0")
    OrionWorkflowTools(service).call("create_ontology_project", arguments)
    assert received["source_scope"] == raw_scope
    assert received["source_scope"]["sources"][0]["database"] == "erp_db"


def test_business_goal_is_preserved_without_fabricating_scope():
    normalized = scope("HYBRID", business_goal="回答哪些订单受供应商影响")
    assert normalized["business_goal"] == "回答哪些订单受供应商影响"
    assert normalized["scope_status"] == "UNRESOLVED"
    assert normalize_source_scope(intake_mode="HYBRID", source_scope=normalized) == normalized


def test_document_s1_mcp_requires_revision_and_does_not_accept_dataset_input(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator

    from harness.orion_workflow_mcp import TOOLS, OrionWorkflowTools
    from services.ontology_engineering import OntologyWorkflowService

    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == "record_document_understanding")
    args = {"project_id": "document-scope", "expected_revision": 3}
    validator = Draft202012Validator(schema)
    validator.validate(args)
    assert list(validator.iter_errors({"project_id": "document-scope"}))
    assert list(validator.iter_errors({**args, "dataset_ids": ["DS-FAKE"]}))
    service = OntologyWorkflowService(tmp_path / "workflows")
    received = {}

    def capture(**kwargs):
        received.update(kwargs)
        return {"project_id": "document-scope", "current_stage": "S2", "revision": 4}

    monkeypatch.setattr(service, "record_document_understanding", capture, raising=False)
    message, result = OrionWorkflowTools(service).call("record_document_understanding", args)
    assert received == args
    assert result["current_stage"] == "S2"
    assert "结构化数据库子任务不适用" in message


def test_scope_status_text_does_not_treat_omitted_scope_as_authorization():
    from harness.orion_workflow_mcp import OrionWorkflowTools

    message = OrionWorkflowTools._summary(
        "get_ontology_workflow_status",
        {
            "project_id": "scope",
            "current_stage": "S0",
            "project_status": "IN_PROGRESS",
            "source_scope": normalize_source_scope(intake_mode="DATABASE_ONLY"),
        },
    )
    assert "待明确，不能视为全部授权" in message


def test_deduplicated_file_paths_require_hash_bound_execution_aliases():
    original = document()
    alias_path = ".orion-s0-uploads/REFERENCE-01/copy.pdf"
    normalized = scope(
        "DOCUMENT_ONLY",
        [
            {"kind": "DOCUMENT", "source_path": original["source_path"], "source_sha256": HASH},
            {"kind": "DOCUMENT", "source_path": alias_path, "source_sha256": HASH},
        ],
    )
    trace = {
        "tool_invocations": [
            {
                "tool": "orion__batch__sha256_deduplicate",
                "status": "SUCCEEDED",
                "input": alias_path,
                "reused_from": original["source_path"],
                "source_sha256": HASH,
            }
        ]
    }
    report = validate_document_sources(normalized, [original], processing_trace=trace)
    assert len(report["observed_sources"]) == 2
    assert report["observed_sources"][1]["alias_of"] == original["source_path"]
    assert "alias_of" not in original
    with pytest.raises(SourceScopeError, match="全部处理"):
        validate_document_sources(normalized, [original])
    bad_trace = deepcopy(trace)
    bad_trace["tool_invocations"][0]["source_sha256"] = OTHER_HASH
    with pytest.raises(SourceScopeError, match="同哈希"):
        validate_document_sources(normalized, [original], processing_trace=bad_trace)
    bad_trace = deepcopy(trace)
    bad_trace["tool_invocations"][0]["reused_from"] = ".orion-s0-uploads/another/rules.pdf"
    with pytest.raises(SourceScopeError, match="同哈希"):
        validate_document_sources(normalized, [original], processing_trace=bad_trace)


def test_real_file_handoff_preserves_source_sheet_and_column_scope_after_normalization():
    from services.ontology_engineering import OntologyWorkflowService
    from services.structured_data.pipeline import StructuredDataPipeline

    doc = document("facts.xlsx")
    receipt = {
        "dataset_id": "DS-1234567890ABCDEF",
        "project_id": "scope-test",
        "document_id": "FILE-001",
        "s0_document_id": doc["document_id"],
        "source_name": doc["source_name"],
        "source_type": "XLSX",
        "source_sha256": HASH,
        "version": "version-1",
        "status": "READY",
        "row_count": 10,
        "sheet_count": 1,
        "imported_at": "2026-09-05T00:00:00+00:00",
        "sheets": [
            {
                "sheet_id": "SH-01",
                "sheet_index": 1,
                "source_name": "订单",
                "view_name": "current_orders",
                "table_name": "ds_orders",
                "row_count": 10,
                "column_count": 1,
                "columns": [
                    {
                        "column_index": 1,
                        "source_name": "参数值",
                        "column_name": "param_value",
                        "inferred_type": "numeric",
                        "nullable": False,
                        "non_null_count": 10,
                        "distinct_count": 10,
                        "minimum_value": "1",
                        "maximum_value": "10",
                    }
                ],
            }
        ],
    }
    handoff = StructuredDataPipeline("postgresql://unused").build_handoff(
        project_id="scope-test", receipts=[receipt]
    )
    payload = handoff["s1"]
    payload["schema_snapshot"] = OntologyWorkflowService._normalize_s1_schema_snapshot(
        payload["schema_snapshot"]
    )
    normalized = scope(
        "HYBRID",
        [
            {
                "kind": "STRUCTURED_FILE",
                "source_path": doc["source_path"],
                "authorized_tables": ["订单"],
                "authorized_columns": {"订单": ["参数值"]},
            }
        ],
    )
    report = validate_database_sources(normalized, payload, documents=[doc])
    assert report["observed_sources"][0]["kind"] == "STRUCTURED_FILE"
    payload["data_profile"]["tables"][0]["columns"].append(
        {"source_name": "工资", "column_name": "salary"}
    )
    with pytest.raises(SourceScopeError, match="列超出"):
        validate_database_sources(normalized, payload, documents=[doc])


def test_unique_sourcebinding_schema_resolves_bare_table_without_cross_schema_guessing():
    normalized = scope(
        sources=[{"kind": "DATABASE", "source_id": "erp", "authorized_tables": ["orders"]}]
    )
    validate_database_sources(normalized, snapshot_payload())
    payload = snapshot_payload()
    payload["datasource_inventory"]["source_bindings"][0]["schemas"] = ["public", "private"]
    with pytest.raises(SourceScopeError, match="超出授权"):
        validate_database_sources(normalized, payload)


def test_queue_checks_real_reference_manifest_before_content_preflight(tmp_path, monkeypatch):
    import json

    from harness.orion_workflow_mcp import OrionWorkflowTools
    from services.ingestion import document_jobs
    from services.ontology_engineering import OntologyWorkflowService, WorkflowGateError

    workspace = tmp_path / "workspace"
    (workspace / "资料").mkdir(parents=True)
    (workspace / "资料/规则.md").write_text("# 规则\n订单必须具备真实来源。")
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(tmp_path / "intake"))
    service = OntologyWorkflowService(tmp_path / "workflows")
    _, reference = OrionWorkflowTools(service).call(
        "snapshot_workspace_sources",
        {
            "workspace_root": str(workspace),
            "references": ["资料"],
        },
    )
    batch = reference["source_path"]
    source_path = f"{batch}/资料/规则.md"
    allowed_scope = {
        "business_goal": "依据规则回答订单要求",
        "sources": [
            {
                "kind": "DOCUMENT",
                "source_path": f"{batch}/资料",
                "path_scope": "DIRECTORY",
            }
        ],
    }
    excluded = service.create_project(
        project_name="排除来源验收",
        domain="scope-test",
        intake_mode="DOCUMENT_ONLY",
        source_scope={**allowed_scope, "excluded_source_refs": [source_path]},
    )
    preflight_calls = []

    def content_preflight(*args):
        preflight_calls.append(args)
        return {"requires_structured_import": False, "file_count": 1}

    monkeypatch.setattr(document_jobs, "inspect_source", content_preflight)
    with pytest.raises(WorkflowGateError, match="排除项"):
        document_jobs.enqueue_document_job(
            service,
            project_id=excluded["project_id"],
            source_path=batch,
            expected_revision=excluded["revision"],
            actor="tester",
        )
    assert preflight_calls == []
    assert not (tmp_path / "intake/.orion-s0-jobs").exists()
    allowed = service.create_project(
        project_name="允许来源验收",
        domain="scope-test",
        intake_mode="DOCUMENT_ONLY",
        source_scope=allowed_scope,
    )
    first = document_jobs.enqueue_document_job(
        service,
        project_id=allowed["project_id"],
        source_path=batch,
        expected_revision=allowed["revision"],
        actor="tester",
    )
    assert first["status"] == "QUEUED"
    assert len(preflight_calls) == 1
    request = json.loads(
        (tmp_path / "intake/.orion-s0-jobs" / first["job_id"] / "request.json").read_text()
    )
    assert request["source_scope_preflight"]["validation_scope"] == "REFERENCE_MANIFEST_ONLY"
    assert request["source_scope_preflight"]["observed_sources"][0]["source_path"] == source_path

    from services.ontology_engineering.stage_contracts import LEGACY_STAGE_CONTRACT_VERSION

    legacy = service.create_project(
        project_name="旧合同资料验收",
        domain="scope-test",
        intake_mode="DOCUMENT_ONLY",
        source_scope={**allowed_scope, "excluded_source_refs": [source_path]},
        _stage_contract_version=LEGACY_STAGE_CONTRACT_VERSION,
    )
    historical = document_jobs.enqueue_document_job(
        service,
        project_id=legacy["project_id"],
        source_path=batch,
        expected_revision=legacy["revision"],
        actor="tester",
    )
    assert historical["status"] == "QUEUED"
    legacy_request = json.loads(
        (tmp_path / "intake/.orion-s0-jobs" / historical["job_id"] / "request.json").read_text()
    )
    assert legacy_request["source_scope_preflight"] is None


def test_database_identity_wins_over_display_label():
    """A differing Chat2DB display label must not block a matching database identity."""
    declared = database(datasource_label="Chat2DB @127.0.0.1 POSTGRESQL · erp_db")
    declared.pop("source_id")
    observed = database(datasource_label="erp connection")
    report = validate_database_sources(scope(sources=[declared]), snapshot_payload([observed]))
    assert report["observed_sources"][0]["database"] == "erp_db"
    wrong = database(database="other_db", datasource_label=declared["datasource_label"])
    with pytest.raises(SourceScopeError):
        validate_database_sources(scope(sources=[declared]), snapshot_payload([wrong]))
    label_only = {"kind": "DATABASE", "datasource_label": "erp connection",
                  "schemas": ["public"], "authorized_tables": ["public.orders"]}
    validate_database_sources(scope(sources=[label_only]), snapshot_payload([observed]))


def test_connection_variable_declared_as_source_id_matches_its_binding():
    """S0 may name the registered read-only connection variable as source_id."""
    declared = database(source_id="ORION_ERP_SOURCE_URL")
    observed = database(connection_ref="env://ORION_ERP_SOURCE_URL")
    report = validate_database_sources(scope(sources=[declared]), snapshot_payload([observed]))
    assert report["observed_sources"][0]["source_id"] == "erp"
    other = database(connection_ref="env://ORION_CRM_SOURCE_URL")
    with pytest.raises(SourceScopeError, match="已声明=.*实际="):
        validate_database_sources(scope(sources=[declared]), snapshot_payload([other]))
    lowercase = database(source_id="orion_erp_source_url")
    with pytest.raises(SourceScopeError):
        validate_database_sources(scope(sources=[lowercase]), snapshot_payload([observed]))
