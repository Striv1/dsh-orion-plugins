"""Static check: CQ required answer variables that a UNION leaves unbound.

If a required (non-nullable) answer variable is bound in only one UNION
branch and nowhere mandatory outside it, every row produced by the other
branch has that variable empty, so G-S6-CQ-ANSWER fails after a long S6 run.
This module reports that at S3/S4 submission.  It is read-only and never
relaxes a contract: OPTIONAL-only variables are not flagged because complete
source data can still bind them.
"""

from __future__ import annotations

from typing import Any

from rdflib import Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from .sparql_execution import _guaranteed_bound

_WRAPPERS = {"Project", "Distinct", "Reduced", "OrderBy", "Slice", "ToMultiSet", "SelectQuery"}


def _mentioned(value: Any) -> set[str]:
    if isinstance(value, Variable):
        return {str(value)}
    if isinstance(value, CompValue):
        return set().union(set(), *(_mentioned(v) for k, v in value.items() if k != "_vars"))
    if isinstance(value, list | tuple):
        return set().union(set(), *(_mentioned(v) for v in value))
    return set()


def _nodes(value: Any):
    if isinstance(value, CompValue):
        yield value
        for key, item in value.items():
            if key != "_vars":
                yield from _nodes(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _nodes(item)


def union_unbound_required_variables(sparql: str, required: list[str]) -> dict[str, str]:
    """Required variable -> reason, only for provable one-sided UNION bindings."""
    try:
        node = translateQuery(parseQuery(sparql)).algebra
    except Exception:
        return {}  # grammar gates report syntax separately
    while isinstance(node, CompValue) and node.name in _WRAPPERS and "p" in node:
        node = node.p
    if not isinstance(node, CompValue) or any(
        n.name in {"Group", "AggregateJoin"} for n in _nodes(node)
    ):
        return {}  # aggregates rename variables; leave to S6 evidence
    # BIND (Extend) is kept: a branch-local BIND target counts as mentioned on
    # that side only, and an outer BIND cannot rebind an in-scope variable.
    guaranteed = {str(v) for v in _guaranteed_bound(node)}
    found: dict[str, str] = {}
    for union in (n for n in _nodes(node) if n.name == "Union"):
        left, right = _mentioned(union.p1), _mentioned(union.p2)
        for name in required:
            if name in guaranteed or name in found:
                continue
            if (name in left) != (name in right):
                found[name] = "left" if name in left else "right"
    return found


def unbound_required_binding_issues(questions: list[dict[str, Any]], runtime: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from .cq_graph_reachability import reviewed_binding_source_path

    issues = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        contract = question.get("answer_contract") or {}
        required = [str(x).removeprefix("?") for x in contract.get("required_bindings") or []]
        nullable = set((contract.get("nullable_bindings") or {}).keys())
        found = union_unbound_required_variables(str(question.get("sparql") or ""), [x for x in required if x not in nullable])
        if not found:
            continue
        fields = ", ".join(sorted(found))
        issue = {
            "gate": "G-S4-CQ-UNION-UNBOUND",
            "reason_code": "REQUIRED_BINDING_UNBOUND_IN_UNION_BRANCH",
            "owner": "ENGINEERING_AGENT",
            "question_id": question.get("id"),
            "path": f"competency_questions[{index}].sparql",
            "unbound_fields": sorted(found),
            "message": (
                f"能力问题 {question.get('id')} 的必填答案变量 {fields} 只在 UNION 的一侧分支绑定，"
                "另一侧分支产出的每一行这些变量都必然为空，S6 会以 G-S6-CQ-ANSWER 失败。请二选一："
                "① 改写查询，让两侧分支都绑定这些变量，或把 UNION 拆成两个 CQ；"
                "② 若另一侧确属业务上无此信息，在 S3 cq_bindings.nullable_bindings 为这些变量声明同行条件"
                "（{字段:{when:{判别字段:值},reason_zh,source_refs}}），或从 required_bindings 中移除。"
            ),
        }
        upstream = reviewed_binding_source_path(runtime or {}, contract.get("reviewed_runtime_binding"))
        if upstream:
            issue["upstream_source"] = {
                "stage": "S3", "path": upstream, "changed_components": ["RUNTIME_RULES"],
                "instruction": (
                    "该 CQ 由 S3 已审 cq_bindings 编译：用 preview_stage_rollback(target_stage=S3, "
                    "changed_components=[RUNTIME_RULES]) → reopen_stage_for_correction → fork_s3_draft_from_history "
                    "→ 在 path 处改写查询或补 nullable_bindings → S3 预检/提交。"
                ),
            }
        issues.append(issue)
    return issues
