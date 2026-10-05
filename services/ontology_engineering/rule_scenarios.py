"""Single-rule ground Horn scenarios, never production or numeric execution evidence.

Match the same explicit ?variables and literal constants used by runtime rules.
Comparison/aggregation predicates must be supplied as facts; this module does not
evaluate their names as operators or replace S6's real query/engine validation.
"""

from __future__ import annotations

import re
from collections import defaultdict

from services.realtime_qa.reasoning import match_ground_atom

_ATOM = re.compile(r"([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(([^()]*)\)")
_VARIABLE = re.compile(r"\?[A-Za-z_]\w*")


class RuleScenarioError(ValueError):
    pass


def _atom(text: str) -> tuple[str, tuple[str, ...]]:
    match = _ATOM.fullmatch(text.strip())
    if not match:
        raise RuleScenarioError("仅支持平面的谓词原子，不支持嵌套表达式。")
    arguments = tuple(value.strip() for value in match[2].split(",")) if match[2].strip() else ()
    if not arguments or any(not value for value in arguments):
        raise RuleScenarioError("谓词参数不能包含空项。")
    if any(value.startswith("?") and not _VARIABLE.fullmatch(value) for value in arguments):
        raise RuleScenarioError("变量必须使用 ?name；不支持参数中的表达式。")
    return match[1], arguments


def ground_horn_scenario_fires(expression: str, facts: list[str]) -> bool:
    """Whether one grounded witness satisfies all premises of one rule.

    NOT uses the finite supplied case snapshot. Production closed-world source
    completeness is enforced separately by the workflow and runtime contracts.
    """
    parts = re.split(r"\s+THEN\s+", expression.strip(), maxsplit=1, flags=re.I)
    if len(parts) != 2 or not re.match(r"^IF\s+", parts[0], re.I):
        raise RuleScenarioError("规则需要 IF 前提 THEN 单一结论。")
    body = re.sub(r"^IF\s+", "", parts[0], flags=re.I)
    positive, negative = [], []
    for term in re.split(r"\s+AND\s+", body, flags=re.I):
        negated = bool(re.match(r"^NOT\s+", term, re.I))
        atom = _atom(re.sub(r"^NOT\s+", "", term, flags=re.I) if negated else term)
        (negative if negated else positive).append(atom)
    conclusion = _atom(parts[1])
    if len(positive) + len(negative) > 64 or len(facts) > 10_000:
        raise RuleScenarioError("单条场景规模过大；请拆分为有界的正反与边界用例。")
    bound_variables = {arg for _, args in positive for arg in args if _VARIABLE.fullmatch(arg)}
    required_variables = {arg for _, args in [*negative, conclusion] for arg in args if _VARIABLE.fullmatch(arg)}
    if not positive or not required_variables.issubset(bound_variables):
        raise RuleScenarioError("结论与否定前提的变量必须先由正向前提绑定。")
    index = defaultdict(list)
    for value in facts:
        predicate, arguments = _atom(value)
        if any(_VARIABLE.fullmatch(arg) for arg in arguments):
            raise RuleScenarioError("测试事实必须为具体值，不能含未绑定变量。")
        index[(predicate, len(arguments))].append(arguments)

    attempts = 0

    def counted_match(predicate, args, row, binding):
        nonlocal attempts
        attempts += 1
        if attempts > 50_000:
            raise RuleScenarioError("场景关联组合过多；请用记录身份缩小测试输入。")
        return match_ground_atom((predicate, args), (predicate, row), binding)

    def search(position, binding):
        if position == len(positive):
            return not any(
                counted_match(predicate, args, row, binding) is not None
                for predicate, args in negative
                for row in index[(predicate, len(args))]
            )
        predicate, args = positive[position]
        for row in index[(predicate, len(args))]:
            next_binding = counted_match(predicate, args, row, binding)
            if next_binding is not None and search(position + 1, next_binding):
                return True
        return False

    return search(0, {})
