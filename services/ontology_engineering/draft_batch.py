"""草稿分批契约。

单次工具参数过大会在模型侧被截断成非法 JSON（MALFORMED_RESPONSE），这发生在
平台收到调用之前，平台无法拦截。唯一可靠的规避是让每次修补都小。此前上限只
写在 `get_next_workflow_action` 的建议文本里，智能体读完仍会在下一步发出几十
个 operations 的巨包。这里把同一组数字变成一处定义、两处共用的可执行门禁：
策略播报与修补校验引用同一常量，修补超限直接拒绝并给出拆分方式。
"""

from __future__ import annotations

import json
from typing import Any

MAX_OPERATIONS_PER_PATCH = 12
MAX_RECOMMENDED_PATCH_BYTES = 32768

_POLICY_INSTRUCTION = (
    "单次 patch_stage_submission 只提交一小批增量（上限 "
    f"{MAX_OPERATIONS_PER_PATCH} 个 operations，建议不超过 {MAX_RECOMMENDED_PATCH_BYTES} 字节），"
    "分批分多轮补齐。长文本用 replace_text 与 value={old:唯一原文,new:替换文本} 做精确局部编辑，不重传整段 OBDA。单次工具参数过大会在模型侧被截断成非法 JSON（MALFORMED_RESPONSE），"
    "该失败发生在平台之前；运行时会把它作为工具错误返回（提示参数不是合法 JSON、未执行任何写入），此时回读当前草稿后用更小的一批重发即可。含嵌套 options 的 confirmations 决策卡每批不超过 3 条。超过 operations 上限的修补会被拒绝。"
)


def draft_batch_policy() -> dict[str, Any]:
    """播报给智能体的分批策略。"""

    return {
        "max_operations_per_patch": MAX_OPERATIONS_PER_PATCH,
        "max_recommended_patch_bytes": MAX_RECOMMENDED_PATCH_BYTES,
        "enforced": True,
        "instruction": _POLICY_INSTRUCTION,
    }


def operations_byte_count(operations: Any) -> int:
    """operations 的紧凑 JSON 字节数；无法序列化时返回 0 而不是抛错。"""

    try:
        return len(json.dumps(operations, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def oversized_patch_problem(operations: Any) -> str | None:
    """超过 operations 上限时给出拒绝原因与拆分方式，否则返回 None。"""

    if not isinstance(operations, list):
        return None
    count = len(operations)
    if count <= MAX_OPERATIONS_PER_PATCH:
        return None
    batches = (count + MAX_OPERATIONS_PER_PATCH - 1) // MAX_OPERATIONS_PER_PATCH
    return (
        f"本次修补有 {count} 个 operations，超过单次上限 {MAX_OPERATIONS_PER_PATCH}。"
        f"请拆成 {batches} 批提交：先提交前 {MAX_OPERATIONS_PER_PATCH} 个，"
        "用返回的新 payload_file 作为下一批的基线，重复到补齐。"
        "上限存在的原因是单次工具参数过大会在模型侧被截断成非法 JSON，平台无法拦截该失败。"
    )


def patch_batch_receipt(operations: Any) -> dict[str, Any]:
    """本次修补的批次用量回执，便于智能体自校准下一批大小。"""

    count = len(operations) if isinstance(operations, list) else 0
    byte_count = operations_byte_count(operations)
    return {
        "operations": count,
        "max_operations_per_patch": MAX_OPERATIONS_PER_PATCH,
        "operations_bytes": byte_count,
        "max_recommended_patch_bytes": MAX_RECOMMENDED_PATCH_BYTES,
        "within_recommended_bytes": byte_count <= MAX_RECOMMENDED_PATCH_BYTES,
    }
