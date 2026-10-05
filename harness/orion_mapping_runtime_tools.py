"""Revision-bound MCP adapters for mapping compilation and file-based review."""
from __future__ import annotations

import json

from services.ontology_contracts.errors import WorkflowError
from services.ontology_engineering.mapping_runtime_compiler import compile_mapping_runtime
from services.ontology_engineering.semantic_artifact import (
    ONTOLOGY_CANDIDATES_PATH,
    parse_ontology_candidates,
)


def compile_current_mapping(tools, *, project_id: str, expected_revision: int) -> dict:
    with tools.service._project_mutation_lock(project_id):
        project_dir = tools.service._resolve_project(project_id)
        state = tools.service._read_state(project_dir)
        tools.service._require_expected_revision(state, expected_revision)
        draft = tools._get_stage_draft(project_id=project_id, stage="S3")
        reference = draft.get("payload_file")
        if not reference:
            raise WorkflowError("当前修订没有 S3 检查点；先 generate_mapping_skeleton 或 save_stage_submission，不复用旧修订草稿。")
        payload, _ = tools._read_submission_payload(project_dir, reference)
        mapping = payload.get("mapping_draft")
        if not isinstance(mapping, dict):
            raise WorkflowError("S3 检查点缺少 mapping_draft。")

        def read(relative):
            with tools._open_workspace_source(project_dir, relative) as stream:
                try:
                    return json.load(stream)
                except (ValueError, UnicodeError) as exc:
                    raise WorkflowError(f"编译来源不是有效 JSON：{relative}") from exc

        snapshot = read("01-data-understanding/schema-snapshot.json")
        rules = read("02-semantic-recognition/business-rule-candidates.json")
        intake = read("00-document-evidence/cq-intake.json")
        if not isinstance(snapshot, dict) or not isinstance(rules, list) or not isinstance(intake, dict):
            raise WorkflowError("S1/S2 或业务问题产物结构不正确，不能编译运行设计。")
        with tools._open_workspace_source(project_dir, ONTOLOGY_CANDIDATES_PATH) as stream:
            candidates = parse_ontology_candidates(stream.read())
        result = compile_mapping_runtime(mapping, snapshot, project_id=project_id,
                                         intake_mode=state.get("intake_mode"),
                                         datasource_inventory=(read("01-data-understanding/datasource-inventory.json")
                                                               if any(t.get("physical_version_table") for t in snapshot.get("tables", [])) else None),
                                         ontology_candidates=candidates,
                                         rule_candidates=rules, cq_questions=intake.get("questions") or [])
        business_guidance = ""
        if mapping.get("business_query_plans"):
            result["input_contract_read"] = {"tool": "get_stage_input_contract", "args": {
                "project_id": project_id, "stage": "S3", "section": "mapping_draft.business_query_plans"}}
            business_guidance = ("业务计划错误按 open_items.path 和正式合同局部 patch 计划，以 CHECKPOINT 保存后重新编译；"
                                 "生成查询与规则不直接 patch，不改变业务条件或独立验收期望来通过门禁。")
        if any(item.get("kind") == "BUSINESS_PLAN_REVIEW_REQUIRED" for item in result["open_items"]):
            # An invalid business plan produces an incomplete runtime. Merging
            # it can hide the actual plan error behind an ownership conflict,
            # or remove previously generated assets from the current draft.
            # Return the compiler's diagnostics before attempting any merge.
            result.pop("runtime")
            result.pop("compiled_blocks")
            failure = {**result, "project_id": project_id, "stage": "S3", "revision": expected_revision,
                       "status": "BUSINESS_PLAN_REQUIRES_REPAIR", "payload_file": reference,
                       "formal_state_changed": False, "draft_saved": False, "validated": False,
                       "next_step": business_guidance + "本次未合并或保存不完整运行内容；先解决业务计划错误，再编译和完整预检。"}
            from services.ontology_engineering.stage_checkpoint import record_checkpoint_compilation
            record_checkpoint_compilation(tools, project_dir, state, reference, failure)
            return failure
        existing = payload.get("realtime_runtime")
        if existing:
            if not isinstance(existing, dict):
                raise WorkflowError("realtime_runtime 必须是对象。")
            from services.ontology_engineering.mapping_runtime_extension import (
                extend_mapping_runtime,
            )
            extension = extend_mapping_runtime(existing, result, mapping, intake_mode=state.get("intake_mode"))
            result["runtime"] = extension["runtime"]
            result["open_items"] = extension["open_items"]
            result["self_check"] = extension["self_check"]
            result["summary"].update(added_mapping_ids=extension["added_mapping_ids"],
                                     existing_runtime_preserved=True, open_item_count=len(extension["open_items"]),
                                     compiled_mapping_count_scope="GENERATED_BY_COMPILER",
                                     query_count_scope="GENERATED_ONLY",
                                     runtime_query_count=len(extension["runtime"].get("ontop_queries") or {}),
                                     runtime_query_capability_count=len(extension["runtime"].get("query_capabilities") or {}),
                                     runtime_rule_count=len(extension["runtime"].get("reasoning_capabilities") or {}))
            if not extension.get("runtime_changed", bool(extension["added_mapping_ids"])):
                result.pop("runtime")
                result.pop("compiled_blocks")
                return {**result, "project_id": project_id, "stage": "S3", "revision": expected_revision,
                        "status": "RUNTIME_ALREADY_PRESENT", "payload_file": reference,
                        "formal_state_changed": False, "validated": False,
                        "next_step": business_guidance + "生成内容没有变化；已保留当前运行设计。按 self_check/open_items 局部修复，禁止删除运行设计后重做。"}
        result.pop("compiled_blocks")
        payload["realtime_runtime"] = result.pop("runtime")
        payload.setdefault("confirmations", [])
        payload.setdefault("automatic_decisions", [])
        saved = tools._save_stage_submission(project_id=project_id, stage="S3", payload=payload,
                                              expected_revision=expected_revision, validation_mode="CHECKPOINT",
                                              _base_payload_file=reference)
        return {**saved, **result, "status": "RUNTIME_EXTENDED_PENDING_REVIEW" if existing else "RUNTIME_DRAFT_SAVED_PENDING_REVIEW",
                "next_step": business_guidance + "回读并处理 open_items；confirmations/automatic_decisions 每批最多 3 条。完成后 preflight_stage_submission，只有通过后才 commit；静态编译不代表实际数据与问答已验收。"}


def prepare_mapping_from_draft(tools, *, project_id: str, payload_file=None, review_scope="FULL", **kwargs):
    if review_scope not in {"FULL", "BUSINESS_ONLY"}:
        raise WorkflowError("review_scope 必须是 FULL 或 BUSINESS_ONLY。")
    if review_scope == "BUSINESS_ONLY" and payload_file is None:
        raise WorkflowError("BUSINESS_ONLY 仅用于 payload_file；内联业务评审请省略 realtime_runtime。")
    if payload_file is None:
        if any(key not in kwargs for key in ("mapping_draft", "confirmations", "automatic_decisions")):
            raise WorkflowError("内联提交必须提供 mapping_draft、confirmations、automatic_decisions，或改传 payload_file。")
        return tools.service.prepare_mapping_review(project_id=project_id, **kwargs)
    fields = {"mapping_draft", "confirmations", "automatic_decisions", "realtime_runtime"}
    if fields.intersection(kwargs):
        raise WorkflowError("payload_file 与内联映射内容不能混用。")
    with tools.service._project_mutation_lock(project_id):
        project_dir = tools.service._resolve_project(project_id)
        state = tools.service._read_state(project_dir)
        tools.service._require_expected_revision(state, kwargs.get("expected_revision"))
        latest = tools._get_stage_draft(project_id=project_id, stage="S3")
        if latest.get("payload_file") != payload_file:
            raise WorkflowError("仅可评审当前修订的最新 S3 草稿；请先 get_stage_draft。")
        payload, _ = tools._read_submission_payload(project_dir, payload_file)
        if any(key not in payload for key in ("mapping_draft", "confirmations", "automatic_decisions")):
            raise WorkflowError("草稿缺少 mapping_draft、confirmations 或 automatic_decisions。")
        submission = {k: payload[k] for k in fields if k in payload}
        if review_scope == "BUSINESS_ONLY":
            if not tools.service._s3_business_review_only(state, {**submission, "realtime_runtime": None}):
                raise WorkflowError("BUSINESS_ONLY 仅适用于 v2 且包含非空业务确认卡；不能绕过完整运行门禁。")
            submission["realtime_runtime"] = None
        result = tools.service.prepare_mapping_review(project_id=project_id, **submission, **kwargs)
        if tools.service._read_state(project_dir)["revision"] == state["revision"]:
            return result
        from harness.orion_s3_checkpoint_handoff import carry_review_checkpoint
        return carry_review_checkpoint(tools, project_id, payload, result)
