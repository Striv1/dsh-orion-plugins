"""Keep unfinished S3 work across explicit review/decision revisions under the project lock."""
from __future__ import annotations

import yaml


def carry_review_checkpoint(tools, project_id, payload, result):
    project_dir = tools.service._resolve_project(project_id)
    state = tools.service._read_state(project_dir)
    # A rejection to S2, legacy freeze or completed S3 must never rebase a draft.
    if (not tools.service._joint_design_enabled(state) or state.get("current_stage") != "S3"
            or state.get("stage_statuses", {}).get("S3") not in {"RUNNING", "BLOCKED_HUMAN"}):
        return result
    with tools._open_workspace_source(project_dir, "03-mapping-review/mapping-draft.yaml") as stream:
        mapping = yaml.safe_load(stream)
    # Decision tools can apply approved mapping updates. Carry those, not the old mapping.
    saved = tools._save_stage_submission(
        project_id=project_id, stage="S3", payload={**payload, "mapping_draft": mapping},
        expected_revision=state["revision"], validation_mode="CHECKPOINT",
        _allow_business_review_handoff=True,
    )
    return {**result, "draft_checkpoint": {**saved,
        "next_step": "原运行草稿已保留，映射已同步本次正式业务决定；运行设计仍未验证。按新 revision 回读、修补并完整预检，不得据此跳过 S3。"}}


def resolve_with_checkpoint(tools, *, option_only, project_id, **kwargs):
    with tools.service._project_mutation_lock(project_id):
        project_dir = tools.service._resolve_project(project_id)
        state = tools.service._read_state(project_dir)
        tools.service._require_expected_revision(state, kwargs.get("expected_revision"))
        draft = tools._get_stage_draft(project_id=project_id, stage="S3")
        payload = None
        if draft.get("payload_file"):
            payload, _ = tools._read_submission_payload(project_dir, draft["payload_file"])
        method = tools.service.resolve_confirmation_option if option_only else tools.service.resolve_confirmation
        result = method(project_id=project_id, **kwargs)
        return carry_review_checkpoint(tools, project_id, payload, result) if payload is not None else result
