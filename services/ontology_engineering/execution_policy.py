"""Deterministic execution boundaries, independent of model interpretation.

This read-only directive does not authorize a write or override a session pause.
"""

from __future__ import annotations

from typing import Any

from services.ontology_engineering.draft_batch import draft_batch_policy

_AUTO_ACTIONS = {
    "INGEST_DOCUMENT_EVIDENCE": {"S0"},
    "BUILD_DATA_UNDERSTANDING_FROM_REGISTERED_DATASETS": {"S1"},
    "REVALIDATE_DOCUMENT_UNDERSTANDING": {"S1"},
    "COMPILE_SEMANTIC_CANDIDATES_AND_RULES": {"S2"},
    "PREPARE_MAPPING_AND_RUNTIME_REVIEW": {"S3"},
    "COMPILE_RUNTIME_FROM_BUSINESS_DECISIONS": {"S3"},
    "GENERATE_AND_PREFLIGHT_ONTOLOGY_DESIGN": {"S4"},
    "BUILD_ONTOLOGY_WITH_PROTEGE_AND_VERIFY": {"S5"},
    "RUN_FULL_QUALITY_AND_REASONING_VALIDATION": {"S6"},
}
_READ_ONLY_ACTIONS = {
    "RESTORE_PROJECT_IF_REQUESTED",
    "USE_RELEASE_OR_CREATE_REVISION",
    "READ_STATUS_AND_INTEGRITY",
    "OBSERVE_APPROVED_RELEASE_RUNTIME",
    "READ_APPROVED_RELEASE_EVIDENCE",
    "WAIT_FOR_EXPLICIT_RELEASE_RESUMPTION",
}
_RUNTIME_ACTIVE = {"PACKAGE_PUBLISHED", "RUNTIME_VERIFYING"}
_RUNTIME_BLOCKED = {"PACKAGE_READY_RUNTIME_BLOCKED", "RUNTIME_FAILED"}


def finalize_execution_directive(
    directive: dict[str, Any],
    *,
    state: dict[str, Any],
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decorate every next-action path and fail closed on unknown combinations."""
    result = {**directive}
    stage = str(result.get("current_stage") or "").upper()
    status = str(result.get("stage_status") or "").upper()
    project_status = str(state.get("project_status") or "").upper()
    mode = "READ_ONLY_NO_ACTION"
    reason = "只读回读状态；当前指令没有可自动执行的确定性分支。"
    preserve_approval = False

    if stage == "S7" and project_status in _RUNTIME_ACTIVE | _RUNTIME_BLOCKED:
        publication = publication or {}
        preserve_approval = publication.get("approval_decision") == "APPROVED"
        valid_receipt = preserve_approval and all(
            str(publication.get(key) or "").strip()
            for key in ("release_version", "approved_by", "release_notes")
        )
        result.update(
            {
                "recommended_tool": "get_ontology_workflow_status",
                "allowed_write_tools": [],
                "input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                "existing_release_approval": {
                    "decision": publication.get("approval_decision"),
                    "release_version": publication.get("release_version"),
                    "source_artifact": "07-release/publication.json",
                    "reapproval_required": False,
                },
            }
        )
        if not valid_receipt:
            result["action"] = "READ_APPROVED_RELEASE_EVIDENCE"
            reason = "发布运行状态缺少完整原批准回执；先修复证据，不重新审批或重新生成发布包。"
        elif project_status in _RUNTIME_ACTIVE:
            result["action"] = "OBSERVE_APPROVED_RELEASE_RUNTIME"
            reason = "原批准和发布包已经完成；只观察现有运行时任务，不重复启动或再次审批。"
        elif (state.get("release_runtime_status") or {}).get("failure_category") == "RELEASE_CONTRACT_INVALID":
            preview_args = {
                "project_id": result["project_id"], "target_stage": "S3",
                "changed_components": ["RUNTIME_RULES"],
            }
            result.update(
                action="CORRECT_RELEASE_CONTRACT",
                failure_category="RELEASE_CONTRACT_INVALID",
                contract_error=(state.get("release_runtime_status") or {}).get("contract_error"),
                recommended_tool="preview_stage_rollback",
                recommended_args=preview_args,
                allowed_write_tools=["preview_stage_rollback", "create_revision_from_release"],
                correction_preview={
                    "tool": "preview_stage_rollback", "arguments_prefix": preview_args,
                    "missing_arguments": [],
                },
                correction_revision={
                    "tool": "create_revision_from_release",
                    "arguments_prefix": {
                        "project_id": result["project_id"],
                        "release_version": publication["release_version"],
                        "target_stage": "S3", "expected_revision": int(state.get("revision") or 0),
                    },
                    "missing_arguments": ["reason", "requested_by"],
                    "instruction": (
                        "先回读预览影响；已有 publication 的原工程 commit_allowed=false，"
                        "不能对原工程调用 reopen_stage_for_correction。"
                        "在用户已授权正式修订且未暂停时创建独立 S3 修订工程；"
                        "核对实际受影响组件，重新预检并完成 S4/S7 适用审批与验收。"
                    ),
                },
            )
            result.pop("existing_release_replay", None)
            result.pop("managed_execution", None)
            result.pop("recovery_notice", None)
            result["existing_release_approval"]["applies_to"] = "EXISTING_RELEASE_ONLY"
            mode = "REPAIR_THEN_RETRY"
            reason = (
                "冻结发布合同无效，原样重试不能修复。保留不可变旧包和原审批历史，"
                "先正式预览，再按用户已有修订授权创建独立修订；新修订不得继承旧审批或跳过门禁。"
            )
        else:
            result.update(
                {
                    "action": "REPAIR_APPROVED_RELEASE_RUNTIME",
                    "recommended_tool": "retry_release_runtime_deployment",
                    "recommended_args": {"project_id": result["project_id"]},
                    "allowed_write_tools": [
                        "retry_release_runtime_deployment",
                        "publish_ontology_package",
                    ],
                    "existing_release_replay": {
                        "tool": "publish_ontology_package",
                        "args": {
                            "project_id": result["project_id"],
                            **{
                                key: publication[key]
                                for key in (
                                    "release_version",
                                    "approval_decision",
                                    "approved_by",
                                    "release_notes",
                                )
                            },
                        },
                        "instruction": "仅修复已识别的运行时故障后原样重放原批准参数；复用不可变包，不生成新批准。",
                    },
                }
            )
            mode = "REPAIR_THEN_RETRY"
            reason = "保留原发布批准与不可变包，只修复并恢复 S7 运行时；不得回退 S6 或重新批准。"
    elif project_status == "RELEASE_DEFERRED" or (stage == "S7" and status == "DEFERRED"):
        result.update(
            {
                "action": "WAIT_FOR_EXPLICIT_RELEASE_RESUMPTION",
                "recommended_tool": "get_ontology_workflow_status",
                "allowed_write_tools": [],
            }
        )
        reason = "用户已明确暂缓发布；必须等待明确恢复请求，不能自动恢复审批或发布。"
    else:
        action = str(result.get("action") or "")
        if action == "RECOVER_PREFLIGHT_OPERATION":
            mode = "REPAIR_THEN_RETRY"
            reason = "使用原提交令牌核对持久结果；不得生成新载荷或绕过未决操作。"
        elif action == "WAIT_FOR_BUSINESS_DECISION" and status == "BLOCKED_HUMAN":
            if stage == "S4":
                mode = "WAIT_FOR_DESIGN_APPROVAL"
                reason = "当前版本规定的 S4 评审已进入人工确认；等待用户批准或退回，不能代替业务决定。"
            elif stage == "S3":
                mode = "WAIT_FOR_BUSINESS_DECISION"
                reason = "等待已展示的业务决策，不要求用户补内部技术字段。"
        elif (
            action == "WAIT_FOR_RELEASE_DECISION"
            and stage == "S7"
            and status in {"RUNNING", "PENDING"}
        ):
            mode = "WAIT_FOR_RELEASE_APPROVAL"
            reason = "S7 尚无正式批准；等待明确发布决定，不自动发布。"
        elif (
            action == "REPAIR_THEN_RETRY_FAILED_STAGE"
            and status == "FAILED"
            and stage in {f"S{i}" for i in range(8)}
        ):
            mode = "REPAIR_THEN_RETRY"
            reason = "先根据门禁修复输入或执行环境，再恢复原阶段；禁止原样反复重试。"
        elif action == "REOPEN_S2_FOR_PRODUCTION_RULE_CONTRACT_UPGRADE" and stage == "S3":
            mode = "REPAIR_THEN_RETRY"
            reason = "先按正式预览与回退令牌升级旧规则契约；保留原业务决定，不绕过门禁。"
        elif stage in _AUTO_ACTIONS.get(action, set()) and status in {"RUNNING", "PENDING"}:
            mode = "AUTO_CONTINUE"
            reason = "在已有构建授权且用户未暂停的前提下继续已确定动作；技术准备无需新增业务审批。"
        elif action in _READ_ONLY_ACTIONS:
            reason = "当前只读观察；恢复、修订或归档等新动作需要用户明确请求。"

    # A persisted pause is defensive; session cancellation must also be checked
    # by the caller because the workflow does not record native session pause.
    if (
        project_status in {"ARCHIVED", "PUBLISHED", "PAUSED"}
        or status == "PAUSED"
        or state.get("paused") is True
    ):
        mode = "READ_ONLY_NO_ACTION"
        reason = "工程已终止当前自动执行或处于暂停；只读观察，等待用户明确新请求。"
    if stage == "S7" and project_status in _RUNTIME_ACTIVE | _RUNTIME_BLOCKED | {
        "RELEASE_DEFERRED"
    }:
        result["reason"] = reason
    if mode == "READ_ONLY_NO_ACTION":
        result["allowed_write_tools"] = []
        result["recommended_tool"] = "get_ontology_workflow_status"
        result.pop("managed_execution", None)

    if stage in {"S2", "S3", "S4"}:
        result["input_contract_read"] = {
            "tool": "get_stage_input_contract",
            "args": {"project_id": result["project_id"], "stage": stage, "section": "overview"},
        }
        result["draft_submission_tool"] = "save_stage_submission"
        result["draft_patch_tool"] = "patch_stage_submission"
        result["draft_recovery_read"] = {
            "tool": "get_stage_draft", "args": {"project_id": result["project_id"], "stage": stage},
        }
        result["draft_checkpoint_policy"] = {
            "save_mode": "CHECKPOINT", "final_mode": "PREFLIGHT",
            "instruction": "尽早保存已有内容，分批修补；中断后先回读当前修订草稿。只有完整预检 token 能提交正式阶段。",
            "grants_authorization": False,
        }
        result["draft_batch_policy"] = draft_batch_policy()
        if mode in {"AUTO_CONTINUE", "REPAIR_THEN_RETRY"} and result.get("action") != "RECOVER_PREFLIGHT_OPERATION":
            result["allowed_write_tools"] = list(
                dict.fromkeys(
                    [
                        *(result.get("allowed_write_tools") or []),
                        "save_stage_submission",
                        "patch_stage_submission",
                    ]
                )
            )
    result["execution_policy"] = {
        "schema_version": 1,
        "mode": mode,
        "action": result.get("action"),
        "stage": stage or None,
        "grants_authorization": False,
        "requires_existing_build_authorization": True,
        "must_check_user_pause": True,
        "continuation_condition": "仅在用户已授权本次构建且未暂停时适用；当前会话取消或暂停优先，本政策不授予新的操作权限。",
        "preserve_existing_release_approval": preserve_approval,
        "requires_repair_before_retry": mode == "REPAIR_THEN_RETRY",
        "requires_input_change_before_retry": (
            mode == "REPAIR_THEN_RETRY"
            and result.get("action") in {
                "REOPEN_S2_FOR_PRODUCTION_RULE_CONTRACT_UPGRADE", "CORRECT_RELEASE_CONTRACT",
            }
        ),
        "requires_release_revision": result.get("action") == "CORRECT_RELEASE_CONTRACT",
        "revision_requires_approval": result.get("action") == "CORRECT_RELEASE_CONTRACT",
        "repeat_unchanged_call_allowed": result.get("action") == "RECOVER_PREFLIGHT_OPERATION",
        "stop_on_reconciliation_required": result.get("action") == "RECOVER_PREFLIGHT_OPERATION",
        "reason": reason,
    }
    return result
