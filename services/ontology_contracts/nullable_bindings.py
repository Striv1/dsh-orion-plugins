"""Source-backed conditional absence rules for CQ output fields."""
from __future__ import annotations

import copy
import re
from decimal import Decimal
from typing import Any

from .errors import CQBindingError


def normalize_nullable_bindings(raw: Any, required_bindings: list[str]) -> dict[str, Any]:
    """Allow absence only under explicit, source-backed same-row conditions."""
    if raw is None:
        return {}
    allowed = ", ".join(required_bindings) or "（无）"
    if not isinstance(raw, dict) or set(raw) - set(required_bindings):
        extra = ", ".join(sorted(set(raw) - set(required_bindings))) if isinstance(raw, dict) else "非对象"
        raise CQBindingError(
            "nullable_bindings must name actual required output fields: "
            f"{extra} 不在必填输出字段中。可声明为条件空值的字段只能是该绑定所引 validation_case 的 "
            f"expected_fields：[{allowed}]；非必填字段为空本就允许，直接删除该条目即可。"
        )
    result = {}
    for field, item in raw.items():
        if not isinstance(item, dict) or set(item) != {"when", "reason_zh", "source_refs"}:
            raise CQBindingError(f"nullable binding requires when/reason_zh/source_refs: {field}")
        when = item["when"]
        if (
            not isinstance(when, dict)
            or not when
            or field in when
            or set(when) - set(required_bindings)
        ):
            raise CQBindingError(
                f"nullable binding requires other actual output conditions: {field} 的 when 只能使用"
                f"同一行其他必填输出字段（validation_case.expected_fields 中除 {field} 外的字段：[{allowed}]），"
                "取具体等值。若需要用分支标记（如类型/路径字段）判别，须先把该字段加入 SELECT 投影、"
                "validation_case 的 result_fields/expected_fields，并确保它在所有分支都绑定。"
            )
        if set(when) & set(raw):
            raise CQBindingError(
                f"nullable conditions must use non-nullable output fields: {field} 的 when 引用了同样被声明为可空的字段 "
                + ", ".join(sorted(set(when) & set(raw))) + "；请改用始终非空的必填字段作判别。"
            )
        for value in when.values():
            if not isinstance(value, str | int | float | bool) or (
                isinstance(value, str) and not value.strip()
            ):
                raise CQBindingError(
                    f"nullable condition must be a concrete non-null scalar: {field}"
                )
            if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and not Decimal(str(value)).is_finite()
            ):
                raise CQBindingError(f"nullable condition must be finite: {field}")
        refs = item["source_refs"]
        if (
            not re.search(r"[\u3400-\u9fff]", str(item["reason_zh"]))
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
        ):
            raise CQBindingError(
                f"nullable binding requires Chinese reason and source evidence: {field}"
            )
        result[field] = copy.deepcopy(item)
    return result
