from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harness.orion_workflow_action import ALLOWED_TOOLS
from harness.workflow_action_contract import load_workflow_action_contract


def test_workflow_ui_actions_have_one_shared_contract() -> None:
    actions = load_workflow_action_contract()

    assert frozenset(actions) == ALLOWED_TOOLS
    assert actions["archive_ontology_project"].actor_field == "archived_by"
    assert actions["restore_ontology_project"].actor_field == "restored_by"
    assert actions["preview_stage_rollback"].actor_field == "requested_by"
    assert actions["create_ontology_project"].endpoint == "workflow-create"
    assert actions["resolve_mapping_option"].endpoint == "mapping-option"
    assert actions["start_document_ingestion_job"].endpoint == "document-job"
    assert actions["preflight_workspace_snapshot"].endpoint == "document-job"
    assert actions["record_document_understanding"].endpoint == "workflow-action"
    assert actions["reconcile_document_source_identities"].actor_field == "actor"
    assert {action.name for action in actions.values() if action.endpoint == "workflow-action"} == {
        "resolve_competency_question_review",
        "record_s0_scope_decision",
        "reconcile_document_source_identities",
        "replace_document_sources",
        "record_data_understanding",
        "record_document_understanding",
        "record_semantic_candidates",
        "prepare_mapping_review",
        "prepare_ontology_design_review",
        "record_ontology_build",
        "preflight_stage_submission",
        "commit_preflight_stage_submission",
        "record_quality_validation",
        "retry_failed_stage",
        "retry_release_runtime_deployment",
        "resume_active_revision",
        "preview_stage_rollback",
        "reopen_stage_for_correction",
        "defer_ontology_publication",
        "resume_ontology_publication",
        "publish_ontology_package",
        "revoke_ontology_release",
        "create_revision_from_release",
        "archive_ontology_project",
        "restore_ontology_project",
    }


def test_workflow_action_contract_allows_explicit_actorless_action(tmp_path: Path) -> None:
    contract = tmp_path / "invalid-actions.json"
    contract.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "actions": {
                    "archive_ontology_project": {
                        "endpoint": "workflow-action",
                        "actorField": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_workflow_action_contract(contract)
    assert loaded["archive_ontology_project"].actor_field is None


def test_core_harness_profile_uses_the_shared_workflow_contract() -> None:
    root = Path(__file__).parents[2]
    expected = "harness/contracts/workflow-ui-actions.json"

    manifest = json.loads((root / "contracts/runtime-source-manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    assert expected in manifest["required"] and expected in declared
    assert "harness/orion_workflow_action.py" in manifest["required"]
    body = (root / expected).read_bytes()
    assert len(body) == declared[expected]["bytes"]
    assert hashlib.sha256(body).hexdigest() == declared[expected]["sha256"]
    shipped = load_workflow_action_contract(root / expected)
    assert frozenset(shipped) == ALLOWED_TOOLS
    assert shipped["publish_ontology_package"].actor_field == "approved_by"
    assert shipped["revoke_ontology_release"].actor_field == "revoked_by"
