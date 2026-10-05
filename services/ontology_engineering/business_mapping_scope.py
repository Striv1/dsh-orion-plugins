"""Pure mapping dependency closure shared by preview and business compilation.

Generated queries address target IRIs, not mapping IDs. Until an explicit
multi-source policy exists, every target in a plan's closure must have exactly
one producer in the entire draft, including producers not named by the plan.
Unrelated mapping semantics are left to their own compilation and review.
"""
from __future__ import annotations

from collections import defaultdict


def _plan_mapping_refs(plan):
    refs = set()

    def add(ref):
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("业务计划的 mapping_ref 必须是明确的映射 ID。")
        refs.add(ref)

    def collect(items):
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("业务计划的映射引用须为对象数组。")
        for item in items:
            add(item.get("mapping_ref"))

    if not isinstance(plan, dict):
        raise ValueError("业务计划须为对象。")
    for name in ("entities", "relations", "fields"):
        collect(plan.get(name, []))
    if "rule" in plan:
        rule = plan["rule"]
        if not isinstance(rule, dict):
            raise ValueError("业务计划的 rule 须为对象。")
        collect(rule.get("premises", []))
        add(rule.get("conclusion_mapping_ref"))
    # Source citations and oracle row keys do not introduce executable mappings.
    return refs


def resolve_business_mapping_scope(mappings: list[dict], plan: dict) -> frozenset[str]:
    """Return explicit and transitive mapping IDs, or raise ValueError.

    Call with the *complete* draft before filtering mappings, both in preview
    and in _compile_plan. Do not pass only compiled IDs: even an uncompiled
    same-target producer makes automatic source selection ambiguous. Inputs
    are never changed and no source, rule or expected result is inferred.
    """
    if not isinstance(mappings, list) or any(not isinstance(item, dict) for item in mappings):
        raise ValueError("业务草稿缺少 mappings。")
    by_id, by_target = defaultdict(list), defaultdict(list)
    for item in mappings:
        if isinstance(item.get("id"), str):
            by_id[item["id"]].append(item)
        if isinstance(item.get("target"), str):
            by_target[item["target"]].append(item)

    def target_mapping(target):
        matches = by_target[target]
        if not matches:
            raise ValueError(f"依赖业务对象 {target} 缺少映射。")
        if len(matches) != 1:
            ids = ", ".join(sorted(str(item.get("id") or "<缺少ID>") for item in matches))
            raise ValueError(
                f"本体目标 {target} 存在多个映射（{ids}），查询按 target IRI 取数，"
                "无法确定同目标多来源语义；拒绝自动编译，请明确映射语义后再试。"
            )
        return matches[0]

    pending = sorted(_plan_mapping_refs(plan), reverse=True)
    resolved = set()
    while pending:
        ref = pending.pop()
        if ref in resolved:
            continue
        matches = by_id[ref]
        if not matches:
            raise ValueError(f"业务计划引用了不存在的映射：{ref}")
        if len(matches) != 1:
            raise ValueError(f"映射 ID 重复，不能确定业务能力的依赖：{ref}")
        item = matches[0]
        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"映射 {ref} 缺少明确的本体 target。")
        target_mapping(target)
        resolved.add(ref)
        if len(resolved) > 64:
            raise ValueError("单项业务能力依赖须为 1 至 64 个映射，请拆分业务能力。")
        derivation = item.get("derivation") or {}
        if not isinstance(derivation, dict):
            raise ValueError(f"映射 {ref} 的 derivation 须为对象。")
        for dependency in (item.get("domain"), item.get("range"), item.get("class"),
                           derivation.get("subject_class")):
            if dependency is None or dependency == "":
                continue
            if not isinstance(dependency, str) or not dependency.strip():
                raise ValueError(f"映射 {ref} 的类依赖须为明确的本体目标名。")
            parent = target_mapping(dependency)
            if not str(parent.get("mapping_type") or "").endswith("TO_CLASS"):
                raise ValueError(f"映射 {ref} 的依赖 {dependency} 不是类映射。")
            parent_id = parent.get("id")
            if not isinstance(parent_id, str) or not parent_id.strip():
                raise ValueError(f"依赖业务对象 {dependency} 缺少映射 ID。")
            pending.append(parent_id)
    if not resolved:
        raise ValueError("单项业务能力依赖须为 1 至 64 个映射，请拆分业务能力。")
    return frozenset(resolved)
