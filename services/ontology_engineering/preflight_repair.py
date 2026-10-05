"""Turn validator locations into bounded, revision-bound local repair guidance.

No values are inferred, no patch is executed and no validator result is changed.
A dotted diagnostic location is usable only when it resolves uniquely against
this exact submitted payload; capability names may themselves contain dots.
"""
from __future__ import annotations

import re
from typing import Any

_FOLLOWUP = (
    "依据 issues 和 repair_contract 确认修订值，只提交变化字段；不能删减业务范围或编造证据。"
    "完成修订后必须完整预检，只有 PASSED 返回的新 token 才能提交；定位字段不代表已修复。"
)


def _location(payload: dict[str, Any], path: str) -> tuple[str, bool] | None:
    if not isinstance(path, str) or not path or len(path) > 2048:
        return None
    if path.startswith("/"):
        # Compiler diagnostics already use JSON Pointer. Resolve that exact
        # location against this payload rather than treating it as a dotted key.
        encoded = path[1:].split("/")
        if len(encoded) > 64 or any(re.search(r"~(?![01])", token) for token in encoded):
            return None
        current = payload
        for index, token in enumerate(encoded):
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict):
                if key not in current:
                    if index == len(encoded) - 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                        return path, False
                    return None
                current = current[key]
            elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(current):
                current = current[int(key)]
            else:
                return None
        return path, True
    matches: list[tuple[list[str], bool]] = []
    pending: list[tuple[Any, str, list[str]]] = [(payload, path, [])]
    visits = 0
    while pending and visits < 4096 and len(matches) < 2:
        current, remaining, tokens = pending.pop()
        visits += 1
        if not remaining:
            matches.append((tokens, True))
            continue
        if len(tokens) >= 64:
            return None
        if isinstance(current, dict):
            # Only actual key candidates at diagnostic delimiters, not a scan of
            # every field in a potentially large facts object.
            ends = [i for i, char in enumerate(remaining) if char in ".["]
            ends.append(len(remaining))
            for end in ends:
                key, suffix = remaining[:end], remaining[end:]
                if key in current:
                    pending.append((current[key], suffix[1:] if suffix.startswith(".") else suffix, [*tokens, key]))
            # A missing final dictionary field is a valid add target. Missing
            # parents, array slots, wildcard/ID pseudo-paths are never guessed.
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", remaining) and remaining not in current:
                matches.append(([*tokens, remaining], False))
        elif isinstance(current, list):
            match = re.match(r"\[(0|[1-9][0-9]*)\](?:\.|(?=\[)|$)", remaining)
            if match and int(match[1]) < len(current):
                pending.append((current[int(match[1])], remaining[match.end():], [*tokens, match[1]]))
    if pending or len(matches) != 1:
        return None
    tokens, exists = matches[0]
    return "/" + "/".join(token.replace("~", "~0").replace("/", "~1") for token in tokens), exists


def build_preflight_repair_plan(
    *, project_id: str, stage: str, revision: int, payload: dict[str, Any],
    issues: list[dict[str, Any]], payload_file: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe local repair targets while retaining issue indexes as authority."""
    blocking_state_gates = {"G-PREFLIGHT-STAGE", "G-PREFLIGHT-STALE", "G-CROSS-STAGE-CONTRACT", "G-S6-VALIDATION-STALE"}
    if stage not in {"S1", "S2", "S3", "S4", "S5", "S6"} or any(
        isinstance(issue, dict) and issue.get("gate") in blocking_state_gates for issue in issues
    ):
        return {"status": "STATE_REFRESH_REQUIRED", "targets": [],
                "next_action": {"tool": "get_next_workflow_action", "arguments_prefix": {"project_id": project_id},
                                "missing_arguments": []},
                "instruction": "先回读正式工程状态和允许的下一步；阶段、修订或跨阶段合同冲突不能靠修改当前载荷绕过。",
                "automatic_repair_performed": False}
    cq_failure = any(isinstance(issue, dict) and str(issue.get("gate") or "").startswith("G-S6-CQ-") for issue in issues)
    class_failure = any(isinstance(issue, dict) and issue.get("gate") == "G-S6-CLASS-INSTANCES" for issue in issues)
    if stage == "S6" and (cq_failure or class_failure):
        return {
            "status": "REVIEWED_CONTRACT_DIAGNOSIS_REQUIRED", "targets": [],
            "next_action": {"tool": "get_next_workflow_action",
                            "arguments_prefix": {"project_id": project_id}, "missing_arguments": []},
            "contract_read": {"tool": "get_cq_semantic_review" if cq_failure else "get_business_quality",
                              "arguments_prefix": {"project_id": project_id}, "missing_arguments": []},
            "correction_preview": {"tool": "preview_stage_rollback",
                                   "arguments_prefix": {"project_id": project_id},
                                   "missing_arguments": ["target_stage", "changed_components"]},
            "instruction": (
                "S6 查询结果和类实例覆盖由服务端计算，不能通过修改验收报告、预期行数或清空必需实例要求消除失败。"
                "先核对实际结果、已审答案契约及来源证据，区分执行故障、来源缺失与契约错误；"
                "只有确认需要改变上游设计时，才按实际受影响阶段和组件预览并正式修订。"
                "不要猜测修订值、修改冻结产物或继承失效的批准；修订后重新执行必要验收。"
            ),
            "automatic_repair_performed": False,
        }
    upstream = [issue.get("upstream_source") for issue in issues if isinstance(issue, dict)]
    rule_upstream = [item for item in upstream if isinstance(item, dict) and item.get("stage") == "S2"]
    if stage == "S3" and rule_upstream:
        return {"status": "UPSTREAM_RULE_DEFINITION_REQUIRED", "targets": [],
                "upstream_targets": rule_upstream,
                "next_action": {"tool": "preview_stage_rollback", "arguments_prefix": {
                    "project_id": project_id, "target_stage": "S2", "changed_components": ["SEMANTIC_MODEL"]},
                    "missing_arguments": []},
                "instruction": "规则前提缺少唯一的业务定义；按来源核对并通过正式修订流程补齐S2。"
                               "不能在S3猜测阈值、修改预期答案或反复编译；回退预览不是回退批准。",
                "automatic_repair_performed": False}
    if stage == "S4" and upstream and all(isinstance(item, dict) for item in upstream):
        components = sorted({c for item in upstream for c in item.get("changed_components") or []})
        return {
            "status": "UPSTREAM_CORRECTION_REQUIRED", "targets": [],
            "upstream_targets": [{"issue_index": i, **item} for i, item in enumerate(upstream)],
            "next_action": {"tool": "preview_stage_rollback",
                            "arguments_prefix": {"project_id": project_id, "target_stage": "S3",
                                                 "changed_components": components},
                            "missing_arguments": []},
            "instruction": ("这些 S4 CQ 查询由 S3 已审 cq_bindings 编译，S4 没有可修补的草稿载荷。"
                            "按 upstream_targets 回到 S3 局部修订并重新预检/提交；S4 随后由平台重新编译。"),
            "automatic_repair_performed": False,
        }
    targets = []
    managed = []
    unresolved = []
    business_issues = [i for i, issue in enumerate(issues) if stage == "S3" and isinstance(issue, dict)
                       and issue.get("gate") == "G-S3-BUSINESS-COMPILATION"]
    recompile_issues = [i for i in business_issues if issues[i].get("repair_action") == "RECOMPILE_RUNTIME"]
    source_issues = [i for i in business_issues if issues[i].get("repair_action") == "REVIEW_COMPILATION_SOURCE"]
    for index, issue in enumerate(issues[:80]):
        if index in recompile_issues or index in source_issues:
            # Generated execution and unavailable compiler inputs are not model
            # patch targets. Keep their authoritative issue indexes for review.
            continue
        path = issue.get("path") if isinstance(issue, dict) else None
        location = _location(payload, path)
        if location is None:
            unresolved.append(index)
            continue
        pointer, exists = location
        # An execution validator may report a precise generated query/capability
        # path. That is a diagnostic location, not permission to hand-edit an
        # asset the compiler owns; doing so creates the next merge conflict.
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        runtime = payload.get("realtime_runtime") or {}
        manifest = runtime.get("compiler_manifest") if isinstance(runtime, dict) else None
        if stage == "S3" and isinstance(manifest, dict) and manifest.get("version") == 1:
            assets = manifest.get("assets") or {}
            field = parts[1] if len(parts) >= 2 and parts[0] == "realtime_runtime" else None
            names = assets.get(field) if isinstance(assets, dict) else None
            owned = (field == "mapping_obda" and isinstance(names, str)) or (
                len(parts) >= 3 and isinstance(names, dict) and parts[2] in names)
            if owned:
                managed.append({"issue_index": index, "diagnostic_pointer": pointer,
                                "source_pointer": "/mapping_draft", "generated_content_editable": False})
                continue
        targets.append({"issue_index": index, "json_pointer": pointer,
                        "field_exists": exists, "suggested_operation": "replace" if exists else "add",
                        "value_requires_review": True})
    base = {"project_id": project_id, "stage": stage, "expected_revision": revision,
            "validation_mode": "PREFLIGHT"}
    if payload_file is not None:
        action = {"tool": "patch_stage_submission", "arguments_prefix": {**base, "payload_file": dict(payload_file)},
                  "missing_arguments": ["operations"]}
    else:
        checkpoint_supported = stage in {"S2", "S3", "S4"}
        action = {"tool": "save_stage_submission", "arguments_prefix": {
                      **base, "validation_mode": "CHECKPOINT" if checkpoint_supported else "PREFLIGHT"},
                  "missing_arguments": ["payload"],
                  "instruction": ("先保存当前完整草稿取得 payload_file，随后按定位局部修订，不重建业务内容。"
                                  if checkpoint_supported else
                                  "按 issues 修正当前载荷后再保存并完整预检；此阶段不支持 CHECKPOINT，不能原样重试。")}
    result = {"status": "REQUIRES_REPAIR", "targets": targets,
              "unresolved_issue_indexes": unresolved, "omitted_issue_count": max(0, len(issues) - 80),
              "next_action": action, "instruction": _FOLLOWUP, "automatic_repair_performed": False}
    if managed:
        result["managed_runtime_diagnostics"] = managed
        result["instruction"] = (
            "受管查询、规则和能力字段是诊断位置，不是手工修补目标。回读 mapping_draft 的业务计划与映射，"
            "只修正有来源依据的业务定义，然后 compile_mapping_runtime、使用新 payload_file 完整预检。"
            "若最新编译内容仍触发同一门禁且没有可修订的业务定义，保留诊断并停止，报告平台生成合同冲突；"
            "不能手改生成物、改清单哈希或降低验收期望。"
        ) + _FOLLOWUP
        if len(managed) == len(issues):
            result["status"] = "MANAGED_RUNTIME_SOURCE_REVIEW_REQUIRED"
            result["next_action"] = {"tool": "get_stage_draft", "arguments_prefix": {
                "project_id": project_id, "stage": "S3", "pointer": "/mapping_draft"},
                "missing_arguments": []}
    if business_issues:
        result["contract_reads"] = [{"tool": "get_stage_input_contract", "args": {
            "project_id": project_id, "stage": "S3", "section": "mapping_draft.business_query_plans",
        }}]
        compile_action = {"tool": "compile_mapping_runtime", "arguments_prefix": {
            "project_id": project_id, "expected_revision": revision}, "missing_arguments": [],
            "instruction": "此工具读取当前最新 S3 草稿；先 get_stage_draft 核对待编译草稿，保留已审人工扩展并处理编译冲突。"}
        preflight_action = {"tool": "preflight_stage_submission", "arguments_prefix": {
            "project_id": project_id, "stage": "S3", "expected_revision": revision},
            "missing_arguments": ["payload_file"], "instruction": "使用编译返回的最新 payload_file 完整预检。"}
        result["recompile_issue_indexes"] = recompile_issues
        result["source_review_issue_indexes"] = source_issues
        result["instruction"] = (
            "以此修复路径处理业务计划：按精准字段位置及正式合同局部修改；业务条件、映射选择、独立验收期望仍须依据来源评审。"
            "生成查询、规则或 OBDA 与计划不一致时由 compile_mapping_runtime 重新生成，不能手改生成内容迁就旧结果，"
            "不能删除计划、规则或 CQ 绕过门禁。局部修订以 CHECKPOINT 保存后先编译，再使用最新 payload_file 完整预检。"
        ) + result["instruction"]
        if len(recompile_issues) == len(issues):
            result["status"] = "RECOMPILATION_REQUIRED"
            if payload_file is not None:
                # Compilation has no payload_file argument: an old file receipt
                # must never silently select another, newer checkpoint.
                result["next_action"] = {"tool": "get_stage_draft", "arguments_prefix": {
                    "project_id": project_id, "stage": "S3"}, "missing_arguments": [],
                    "expected_payload_file": dict(payload_file),
                    "instruction": "确认当前草稿与本次预检 payload_file 一致后编译；不一致则先回读最新草稿并重新预检。"}
            result["after_repair"] = [compile_action, preflight_action]
        elif len(source_issues) == len(issues):
            result["status"] = "SOURCE_REVIEW_REQUIRED"
            result["next_action"] = {"tool": "get_stage_input_contract", "arguments_prefix": {
                "project_id": project_id, "stage": "S3", "section": "mapping_draft.business_query_plans"},
                "missing_arguments": [], "instruction": "按 issues 回读编译来源和合同；来源未恢复前不要重复编译或改写生成内容。"}
        else:
            action["arguments_prefix"]["validation_mode"] = "CHECKPOINT"
            result["after_repair"] = [compile_action, preflight_action]
    return result
