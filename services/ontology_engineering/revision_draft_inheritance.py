"""Rebase reviewed S2/S3 drafts after formal rollback without retranscription."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml

from services.ontology_contracts.errors import WorkflowError


def draft_recovery_action(directive: dict, *, stage: str, active: dict,
                          project: Any, revision: int) -> dict:
    """Expose a verified draft recovery route only before a new checkpoint exists."""
    target = active.get("target_stage")
    if stage == "S2" and target == "S2":
        tool = "fork_s2_draft_from_history"
        fallback = "若无匹配的已预检草稿，按 S0/S1 正式来源重新整理 S2 候选与规则。"
    elif stage == "S3" and target in {"S2", "S3"}:
        tool = "fork_s3_draft_from_history"
        fallback = "若无匹配的验签历史检查点，按 S1/S2 来源重新生成 S3 骨架。"
    else:
        return directive
    if (project / f".submission-drafts/checkpoint-{stage}-r{revision}.json").exists():
        return directive
    return {**directive, "recommended_tool": tool, "history_draft_recovery": {
        "tool": tool, "args": {"project_id": project.name,
                         "from_revision_id": active.get("revision_id"),
                         "expected_revision": revision}, "fallback": fallback}}


def fork_s3_draft_from_history(
    tools: Any, *, project_id: str, from_revision_id: str, expected_revision: int,
) -> dict[str, Any]:
    service = tools.service
    project = service._resolve_project(project_id)
    state = service._read_state(project)
    service._require_expected_revision(state, expected_revision)
    active = state.get("active_revision") or {}
    rollback_target = active.get("target_stage")
    downstream_of_s2 = rollback_target == "S2" and state.get("stage_statuses", {}).get("S2") == "PASSED"
    if (state.get("current_stage") != "S3" or state.get("stage_statuses", {}).get("S3") != "RUNNING"
            or active.get("revision_id") != from_revision_id
            or not (rollback_target == "S3" or downstream_of_s2)
            or re.fullmatch(r"REV-\d{8}T\d{6}-[A-F0-9]{8}", from_revision_id) is None):
        raise WorkflowError("只能从当前 S3 正式回退或已重审 S2 后的本次回退快照继承 S3 草稿。")
    if tools._get_stage_draft(project_id=project_id, stage="S3").get("status") != "NO_CURRENT_CHECKPOINT":
        raise WorkflowError("当前修订已有 S3 检查点；请继续增量 patch，不覆盖现有工作。")
    manifest_rel = f"revisions/{from_revision_id}/before-manifest.json"
    mapping_rel = f"revisions/{from_revision_id}/before/03-mapping-review/mapping-draft.yaml"
    with tools._open_workspace_source(project, manifest_rel) as stream:
        manifest_bytes = stream.read(8 * 1024 * 1024 + 1)
    if len(manifest_bytes) > 8 * 1024 * 1024:
        raise WorkflowError("历史快照清单超出大小限制。")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise WorkflowError("历史快照清单格式无效。") from exc
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    match = [item for item in entries or [] if isinstance(item, dict)
             and item.get("path") == "03-mapping-review/mapping-draft.yaml"]
    if len(match) != 1 or match[0].get("snapshot_path") != "before/03-mapping-review/mapping-draft.yaml":
        raise WorkflowError("历史 S3 映射不在本次回退的完整快照清单中。")
    with tools._open_workspace_source(project, mapping_rel) as stream:
        mapping_bytes = stream.read(8 * 1024 * 1024 + 1)
    if (len(mapping_bytes) != match[0].get("size")
            or "sha256:" + hashlib.sha256(mapping_bytes).hexdigest() != match[0].get("sha256")):
        raise WorkflowError("历史 S3 映射快照大小或哈希不一致。")
    try:
        mapping = yaml.safe_load(mapping_bytes)
    except yaml.YAMLError as exc:
        raise WorkflowError("历史 S3 映射快照格式无效。") from exc
    drafts = project / ".submission-drafts"
    if drafts.is_symlink() or not drafts.is_dir():
        raise WorkflowError("历史草稿目录不存在或不是安全目录。")
    candidates = []
    for path in drafts.glob("checkpoint-S3-r*.json"):
        found = re.fullmatch(r"checkpoint-S3-r(\d+)\.json", path.name)
        if found and int(found[1]) < expected_revision and not path.is_symlink():
            candidates.append((int(found[1]), path))
    for old_revision, path in sorted(candidates, reverse=True):
        if path.stat().st_size > 65536:
            continue
        with tools._open_workspace_source(project, f".submission-drafts/{path.name}") as stream:
            receipt = json.load(stream)
        if (not isinstance(receipt, dict) or receipt.get("project_id") != project_id
                or receipt.get("stage") != "S3" or receipt.get("revision") != old_revision
                # Rollback increments once and formal S2 resubmission once; the
                # S3 editing baseline must belong to the immediately preceding
                # pre-rollback revision, never an arbitrary older checkpoint.
                or (downstream_of_s2 and old_revision != expected_revision - 2)
                or (not downstream_of_s2 and (receipt.get("last_preflight") or {}).get("status") != "PASSED")):
            continue
        payload, _ = tools._read_submission_payload(project, receipt["payload_file"])
        if isinstance(payload, dict) and payload.get("mapping_draft") == mapping:
            saved = tools._save_stage_submission(
                project_id=project_id, stage="S3", payload=payload,
                expected_revision=expected_revision, validation_mode="CHECKPOINT",
                _must_have_no_checkpoint=True,
            )
            return {**saved, "source_revision_id": from_revision_id,
                    "source_checkpoint_revision": old_revision,
                    "source_payload_sha256": receipt["payload_file"]["sha256"],
                    "inherited_from_verified_snapshot": True,
                    "inheritance_basis": ("VERIFIED_S2_ROLLBACK_S3_CHECKPOINT"
                                          if downstream_of_s2 else "VERIFIED_S3_ROLLBACK_PREFLIGHT"),
                    "instruction": "仅继承旧草稿作为当前修订起点；旧批准与预检不继承。先按新业务口径增量修订，再完整预检和正式提交。"}
    raise WorkflowError("未找到与本次 S3 正式映射快照一致、曾预检通过的历史草稿；需按当前来源重建。")


def fork_s2_draft_from_history(
    tools: Any, *, project_id: str, from_revision_id: str, expected_revision: int,
) -> dict[str, Any]:
    """Restore only a verified editing baseline; no S2 approval/preflight survives."""
    service = tools.service
    project = service._resolve_project(project_id)
    state = service._read_state(project)
    service._require_expected_revision(state, expected_revision)
    active = state.get("active_revision") or {}
    if (state.get("current_stage") != "S2" or state.get("stage_statuses", {}).get("S2") != "RUNNING"
            or active.get("revision_id") != from_revision_id
            or active.get("target_stage") != "S2"
            or re.fullmatch(r"REV-\d{8}T\d{6}-[A-F0-9]{8}", from_revision_id) is None):
        raise WorkflowError("只能从当前 S2 正式回退所生成的历史快照继承草稿。")
    if tools._get_stage_draft(project_id=project_id, stage="S2").get("status") != "NO_CURRENT_CHECKPOINT":
        raise WorkflowError("当前修订已有 S2 检查点；请继续增量 patch，不覆盖现有工作。")
    manifest_rel = f"revisions/{from_revision_id}/before-manifest.json"
    with tools._open_workspace_source(project, manifest_rel) as stream:
        manifest_bytes = stream.read(8 * 1024 * 1024 + 1)
    if len(manifest_bytes) > 8 * 1024 * 1024:
        raise WorkflowError("历史快照清单超出大小限制。")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise WorkflowError("历史快照清单格式无效。") from exc
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    artifacts: dict[str, Any] = {}
    for relative in (
        "02-semantic-recognition/ontology-candidates.yaml",
        "02-semantic-recognition/business-rule-candidates.json",
    ):
        match = [item for item in entries or [] if isinstance(item, dict) and item.get("path") == relative]
        if len(match) != 1 or match[0].get("snapshot_path") != "before/" + relative:
            raise WorkflowError(f"历史 S2 产物不在本次回退的完整快照清单中：{relative}")
        with tools._open_workspace_source(project, f"revisions/{from_revision_id}/before/{relative}") as stream:
            content = stream.read(8 * 1024 * 1024 + 1)
        if (len(content) > 8 * 1024 * 1024 or len(content) != match[0].get("size")
                or "sha256:" + hashlib.sha256(content).hexdigest() != match[0].get("sha256")):
            raise WorkflowError(f"历史 S2 快照大小或哈希不一致：{relative}")
        try:
            artifacts[relative] = yaml.safe_load(content) if relative.endswith(".yaml") else json.loads(content)
        except (yaml.YAMLError, ValueError) as exc:
            raise WorkflowError(f"历史 S2 快照格式无效：{relative}") from exc
    candidates_doc = artifacts["02-semantic-recognition/ontology-candidates.yaml"]
    candidates = candidates_doc.get("candidates") if isinstance(candidates_doc, dict) else None
    rules = artifacts["02-semantic-recognition/business-rule-candidates.json"]
    if not isinstance(candidates, list) or not isinstance(rules, list):
        raise WorkflowError("历史 S2 候选或规则格式无效。")
    drafts = project / ".submission-drafts"
    if drafts.is_symlink() or not drafts.is_dir():
        raise WorkflowError("历史草稿目录不存在或不是安全目录。")
    receipts = []
    for path in drafts.glob("checkpoint-S2-r*.json"):
        found = re.fullmatch(r"checkpoint-S2-r(\d+)\.json", path.name)
        if found and int(found[1]) < expected_revision and not path.is_symlink():
            receipts.append((int(found[1]), path))
    for old_revision, path in sorted(receipts, reverse=True):
        if path.stat().st_size > 65536:
            continue
        with tools._open_workspace_source(project, f".submission-drafts/{path.name}") as stream:
            receipt = json.load(stream)
        if (not isinstance(receipt, dict) or receipt.get("project_id") != project_id
                or receipt.get("stage") != "S2" or receipt.get("revision") != old_revision
                or (receipt.get("last_preflight") or {}).get("status") != "PASSED"):
            continue
        payload, _ = tools._read_submission_payload(project, receipt["payload_file"])
        if (isinstance(payload, dict) and payload.get("ontology_candidates") == candidates
                and payload.get("business_rule_candidates") == rules):
            saved = tools._save_stage_submission(
                project_id=project_id, stage="S2", payload=payload,
                expected_revision=expected_revision, validation_mode="CHECKPOINT",
                _must_have_no_checkpoint=True,
            )
            return {**saved, "source_revision_id": from_revision_id,
                    "source_checkpoint_revision": old_revision,
                    "source_payload_sha256": receipt["payload_file"]["sha256"],
                    "inherited_from_verified_snapshot": True,
                    "instruction": "只继承旧 S2 草稿作为当前修订起点；旧批准与预检不继承。按新来源口径增量修订，再完整预检和正式提交。"}
    # S2 can have been submitted through record_semantic_candidates without a
    # checkpoint. The passed formal artifacts still provide a verified editing
    # baseline; the capability plan preserves CQ assessments if they existed.
    plan_rel = "02-semantic-recognition/capability-plan.json"
    plan_entry = [item for item in entries or [] if isinstance(item, dict) and item.get("path") == plan_rel]
    if len(plan_entry) != 1 or plan_entry[0].get("snapshot_path") != "before/" + plan_rel:
        raise WorkflowError("历史 S2 能力方案不在本次回退的完整快照清单中。")
    with tools._open_workspace_source(project, f"revisions/{from_revision_id}/before/{plan_rel}") as stream:
        plan_bytes = stream.read(8 * 1024 * 1024 + 1)
    if (len(plan_bytes) > 8 * 1024 * 1024 or len(plan_bytes) != plan_entry[0].get("size")
            or "sha256:" + hashlib.sha256(plan_bytes).hexdigest() != plan_entry[0].get("sha256")):
        raise WorkflowError("历史 S2 能力方案快照大小或哈希不一致。")
    try:
        plan = json.loads(plan_bytes)
    except ValueError as exc:
        raise WorkflowError("历史 S2 能力方案格式无效。") from exc
    if not isinstance(plan, dict):
        raise WorkflowError("历史 S2 能力方案格式无效。")
    payload = {"ontology_candidates": candidates, "business_rule_candidates": rules}
    assessments = plan.get("cq_semantic_assessments")
    if assessments is not None:
        if not isinstance(assessments, list):
            raise WorkflowError("历史 S2 CQ 语义评估格式无效。")
        payload["cq_semantic_assessments"] = assessments
    saved = tools._save_stage_submission(
        project_id=project_id, stage="S2", payload=payload,
        expected_revision=expected_revision, validation_mode="CHECKPOINT",
        _must_have_no_checkpoint=True,
    )
    return {**saved, "source_revision_id": from_revision_id,
            "source_checkpoint_revision": None,
            "source_payload_sha256": None,
            "inheritance_basis": "VERIFIED_FORMAL_S2_ARTIFACTS",
            "inherited_from_verified_snapshot": True,
            "instruction": "由验签正式 S2 候选、规则和能力方案恢复编辑起点；旧批准与预检不继承。按新来源口径增量修订，再完整预检和正式提交。"}
