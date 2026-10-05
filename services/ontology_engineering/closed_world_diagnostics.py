"""Field-level diagnostics for closed-world (NOT) rule source contracts.

The S2 gate for a closed-world set-difference rule checks roughly a dozen
independent facts about each snapshot it is given: the dataset identity, two
content hashes, a snapshot version, a row-count receipt, key fields, role
bindings, a status filter and a PII-minimisation declaration.  Expressing that
as one boolean `if` is cheap to write and expensive to satisfy: the agent is
told "缺少完整记录快照、字段绑定、状态过滤、版本或 PII 最小化声明" and has to
guess which of the five it actually got wrong.

These helpers evaluate the same conditions one at a time and return the list of
failures, each naming the field, what was submitted and how to fix it, so the
gate message can be acted on without reading the platform source.
"""

from __future__ import annotations

import json
import re
from typing import Any

from services.ontology_contracts import closed_world_roles

SHA256_PATTERN = re.compile(r"^sha256:[a-fA-F0-9]{64}$")

COMPLETENESS_REQUIRED = "COMPLETE_FOR_CASE_AND_SNAPSHOT"


def _seen(value: Any) -> str:
    """A short, safe rendering of what was submitted for a field."""

    if value is None:
        return "缺失"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "空字符串"
        return text if len(text) <= 48 else f"{text[:45]}…"
    if isinstance(value, dict):
        keys = list(value)[:8]
        return f"对象，键：{'、'.join(str(key) for key in keys) or '无'}"
    if isinstance(value, list):
        return f"数组，长度 {len(value)}"
    return type(value).__name__


def _require_hash(problems: list[str], source: dict[str, Any], field: str) -> None:
    value = source.get(field)
    if not SHA256_PATTERN.fullmatch(str(value or "")):
        problems.append(f"{field} 必须是 sha256:<64 位十六进制>（当前：{_seen(value)}）")


def _require_text(problems: list[str], source: dict[str, Any], field: str, hint: str) -> None:
    if not str(source.get(field) or "").strip():
        problems.append(f"{field} 缺失或为空：{hint}")


def _require_row_count(problems: list[str], source: dict[str, Any]) -> None:
    row_count = source.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        problems.append(
            f"row_count 必须是非负整数计数回执（当前：{_seen(row_count)}）；"
            "请用 query_source_evidence 数一次真实行数再填"
        )


def _require_nonempty_list(
    problems: list[str], source: dict[str, Any], field: str, hint: str
) -> list[Any] | None:
    value = source.get(field)
    if not isinstance(value, list) or not value:
        problems.append(f"{field} 必须是非空数组：{hint}（当前：{_seen(value)}）")
        return None
    return value


def _require_production_evidence(problems: list[str], source: dict[str, Any]) -> None:
    dataset_type = str(source.get("dataset_type") or "").upper()
    if dataset_type != "PRODUCTION_EVIDENCE":
        problems.append(
            f'dataset_type 必须是 "PRODUCTION_EVIDENCE"（当前：{_seen(source.get("dataset_type"))}）'
        )
    if source.get("production_evidence") is not True:
        problems.append(
            "production_evidence 必须是布尔 true（当前："
            f"{_seen(source.get('production_evidence'))}）"
        )


def _require_roles(
    problems: list[str],
    source: dict[str, Any],
    aliases: dict[str, str],
    resolver: Any,
) -> dict[str, str] | None:
    bindings = source.get("field_bindings")
    roles = resolver(bindings)
    if roles is None:
        problems.append(
            f"field_bindings 的角色键不正确（当前：{_seen(bindings)}）。"
            + closed_world_roles.role_binding_help(aliases)
        )
    return roles


def submitted_snapshot_problems(source: Any) -> list[str]:
    """List every unmet requirement for one `closed_world_inputs` entry."""

    if not isinstance(source, dict):
        return [f"闭世界输入必须是对象（当前：{_seen(source)}）"]

    problems: list[str] = []
    _require_text(
        problems, source, "predicate", "填该输入所覆盖的 NOT 谓词名，须与规则里的 NOT 谓词一致"
    )
    _require_text(problems, source, "dataset_id", "填 S1 已登记的数据集编号")
    if not source.get("source_refs"):
        problems.append("source_refs 缺失：填该快照来源的证据引用（表名、列、快照集编号）")
    _require_hash(problems, source, "source_sha256")
    _require_hash(problems, source, "snapshot_sha256")
    _require_text(problems, source, "snapshot_version", "填 S1 绑定的物理快照版本号")
    completeness = str(source.get("completeness") or "").upper()
    if completeness != COMPLETENESS_REQUIRED:
        problems.append(
            f'completeness 必须是 "{COMPLETENESS_REQUIRED}"'
            f"（当前：{_seen(source.get('completeness'))}）；"
            "闭世界差集只能建立在声明为完整的快照上"
        )
    _require_row_count(problems, source)
    _require_production_evidence(problems, source)

    key_fields = _require_nonempty_list(problems, source, "key_fields", "列出该快照的业务主键列")
    if key_fields is not None and any(not str(value).strip() for value in key_fields):
        problems.append("key_fields 含空项：每一项都必须是真实列名")
    if key_fields:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in key_fields:
            field = str(value).strip()
            if field in seen:
                duplicates.add(field)
            seen.add(field)
        if duplicates:
            problems.append(
                f"key_fields 含重复列：{'、'.join(sorted(duplicates))}；"
                "同一业务主键列只能声明一次"
            )

    roles = _require_roles(
        problems,
        source,
        closed_world_roles.SUBMITTED_ROLE_ALIASES,
        closed_world_roles.resolve_submitted_roles,
    )
    if roles is not None and key_fields:
        declared = {str(value).strip() for value in key_fields}
        missing = sorted(closed_world_roles.key_role_columns(roles) - declared)
        if missing:
            problems.append(
                f"key_fields 缺少角色绑定的列：{'、'.join(missing)}；"
                "record_id 与 subject_id 绑定的列必须同时出现在 key_fields 里"
            )

    _require_nonempty_list(problems, source, "status_filter", "声明取这份快照时使用的状态过滤条件")

    pii_scope = source.get("pii_scope")
    if not isinstance(pii_scope, dict):
        problems.append(
            f"pii_scope 必须是对象（当前：{_seen(pii_scope)}），"
            '形如 {"mode":"MINIMUM_NECESSARY","allowed_fields":[…],'
            '"direct_identifiers_included":false}'
        )
    else:
        if str(pii_scope.get("mode") or "").upper() != "MINIMUM_NECESSARY":
            problems.append(
                f'pii_scope.mode 必须是 "MINIMUM_NECESSARY"（当前：{_seen(pii_scope.get("mode"))}）'
            )
        allowed_fields = pii_scope.get("allowed_fields")
        if not isinstance(allowed_fields, list) or not allowed_fields:
            problems.append(
                "pii_scope.allowed_fields 必须是非空数组：列出本规则实际读取的列"
                f"（当前：{_seen(allowed_fields)}）"
            )
        if pii_scope.get("direct_identifiers_included") is not False:
            problems.append(
                "pii_scope.direct_identifiers_included 必须是布尔 false"
                f"（当前：{_seen(pii_scope.get('direct_identifiers_included'))}）"
            )
    return problems


def snapshot_contract_differences(expected: list[dict], actual: list[dict]) -> list[str]:
    """Describe S3 snapshot drift against the registered S2 rule, without guessing a repair."""

    expected_by_predicate = {str(item.get("predicate")): item for item in expected}
    actual_by_predicate = {str(item.get("predicate")): item for item in actual}
    if len(expected_by_predicate) != len(expected) or len(actual_by_predicate) != len(actual):
        return ["同一谓词出现多个快照声明；请逐条对照 S2 正式规则候选"]

    differences: list[str] = []
    for predicate in sorted(expected_by_predicate.keys() | actual_by_predicate.keys()):
        if predicate not in expected_by_predicate:
            differences.append(f"{predicate}: S3 多出此闭世界输入")
            continue
        if predicate not in actual_by_predicate:
            differences.append(f"{predicate}: S3 缺少 S2 已登记的闭世界输入")
            continue
        s2 = expected_by_predicate[predicate]
        s3 = actual_by_predicate[predicate]
        for field in sorted(s2.keys() | s3.keys()):
            if s2.get(field) == s3.get(field):
                continue
            expected_value = json.dumps(s2.get(field), ensure_ascii=False, sort_keys=True)
            actual_value = json.dumps(s3.get(field), ensure_ascii=False, sort_keys=True)
            differences.append(
                f"{predicate}.{field}: S2={expected_value[:180]}；S3={actual_value[:180]}"
            )
    return differences[:4]


def canonicalize_registered_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    """Ignore duplicate S2 key declarations when comparing with a valid S3 runtime.

    Older S2 drafts could save the same column twice. S3 requires unique keys,
    so literal equality would make those projects impossible to advance. Keep
    the first declaration and leave every other snapshot field untouched.
    """

    key_fields = source.get("key_fields")
    if not isinstance(key_fields, list) or not all(isinstance(key, str) for key in key_fields):
        return source
    return {**source, "key_fields": list(dict.fromkeys(key_fields))}


def required_set_problems(source: Any) -> list[str]:
    """List every unmet requirement for a rule's `required_set_source`."""

    if not isinstance(source, dict):
        return [f"required_set_source 必须是对象（当前：{_seen(source)}）"]

    problems: list[str] = []
    _require_text(problems, source, "dataset_id", "填 S1 已登记的目录数据集编号")
    if not source.get("source_refs"):
        problems.append("source_refs 缺失：填该必备集合来源的证据引用")
    _require_hash(problems, source, "source_sha256")
    _require_hash(problems, source, "snapshot_sha256")
    _require_text(problems, source, "snapshot_version", "填 S1 绑定的物理快照版本号")
    _require_row_count(problems, source)
    _require_roles(
        problems,
        source,
        closed_world_roles.REQUIRED_SET_ROLE_ALIASES,
        closed_world_roles.resolve_required_set_roles,
    )
    return problems


def describe(problems: list[str]) -> str:
    """Render diagnostics as a numbered, single-line-per-item remedy list."""

    return "；".join(f"({index}) {problem}" for index, problem in enumerate(problems, start=1))
