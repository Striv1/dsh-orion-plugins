"""Evidence-derived exception to the logical-axiom count gate, not to CQ QA."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .stage_contracts import STAGE_CONTRACT_VERSION

POLICY_VERSION = "structured-reviewed-fact-axioms-v1"
RULE_POLICY_VERSION = "reviewed-rule-no-owl-axioms-v1"


def _rule_backed_applicability(
    *, state: dict[str, Any], design: dict[str, Any], intake: dict[str, Any],
    capability_plan: dict[str, Any], runtime: dict[str, Any], rules: list[dict[str, Any]],
    verified_question_ids: set[str], source_sha256: dict[str, str], missing_sources: list[str],
) -> dict[str, Any]:
    """Permit rule execution without inventing OWL axioms, only with a frozen CQ chain."""
    blockers: list[str] = []
    source_questions = intake.get("questions")
    questions = design.get("competency_questions")
    routes = capability_plan.get("routes")
    capabilities = runtime.get("reasoning_capabilities")
    if state.get("stage_contract_version") != STAGE_CONTRACT_VERSION:
        blockers.append("仅适用于 v2 正式评审")
    if state.get("intake_mode") not in {"DATABASE_ONLY", "HYBRID"}:
        blockers.append("当前来源模式尚无规则路径公理适用性合同")
    if any((state.get("stage_statuses") or {}).get(stage) != "PASSED" for stage in ("S1", "S2", "S3")):
        blockers.append("S1/S2/S3 尚未正式通过")
    if missing_sources:
        blockers.append("缺少正式证据：" + ", ".join(missing_sources))
    if (
        not isinstance(source_questions, list) or not source_questions
        or any(not isinstance(row, dict) for row in source_questions)
        or not isinstance(questions, list) or any(not isinstance(row, dict) for row in questions)
        or not isinstance(routes, list) or any(not isinstance(row, dict) for row in routes)
        or not isinstance(capabilities, dict) or not capabilities
    ):
        blockers.append("CQ、能力路由或正式推理能力缺失")
        source_questions, questions, routes, capabilities = [], [], [], {}
    try:
        schema_version = int(runtime.get("schema_version") or 0)
    except (ValueError, TypeError):
        schema_version = 0
    if (
        schema_version < 4 or runtime.get("review_status") != "REVIEWED"
        or runtime.get("reasoning_requirement") != "REQUIRED"
        or capability_plan.get("status") != "READY"
    ):
        blockers.append("S2/S3 未形成已审且就绪的正式规则运行合同")
    rule_by_id = {
        str(rule.get("id") or ""): rule for rule in rules if isinstance(rule, dict)
    }
    if not rule_by_id or len(rule_by_id) != len(rules) or "" in rule_by_id:
        blockers.append("S2 正式规则候选缺失、重复或无编号")
    source_ids = [str(row.get("id") or "") for row in source_questions]
    design_ids = [str(row.get("source_question_id") or "") for row in questions]
    route_ids = [str(row.get("question_id") or "") for row in routes]
    if (
        "" in source_ids or len(set(source_ids)) != len(source_ids)
        or len(design_ids) != len(source_ids) or set(design_ids) != set(source_ids)
        or len(route_ids) != len(source_ids) or set(route_ids) != set(source_ids)
        or verified_question_ids != set(source_ids)
    ):
        blockers.append("原始 CQ 未被唯一、已审的 S3/S4 路由完整覆盖")
    route_by_id = {str(route.get("question_id") or ""): route for route in routes}
    rule_question_count = 0
    pending_rule_review_ids: set[str] = set()
    for question in questions:
        source_id = str(question.get("source_question_id") or "")
        route = route_by_id.get(source_id) or {}
        contract = question.get("answer_contract") or {}
        if not isinstance(contract, dict):
            blockers.append(f"CQ {source_id} 回答合同无效")
            continue
        if (
            route.get("support_status") != "READY"
            or route.get("question_type") == "OWL_INFERENCE"
            or route.get("missing_capabilities") or route.get("missing_source_requirements")
            or not isinstance(route.get("source_readiness"), list)
            or not route.get("source_readiness")
            or any(
                not isinstance(item, dict) or item.get("status") != "READY"
                for item in route.get("source_readiness") or []
            )
            or not contract.get("reviewed_runtime_binding")
            or not contract.get("result_assertions") or not contract.get("boundary_assertions")
        ):
            blockers.append(f"CQ {source_id} 缺少已审路由、来源或结果/边界断言")
        answer_mode = contract.get("answer_mode")
        if answer_mode == "RULE_INFERENCE":
            rule_question_count += 1
            name = str(contract.get("reasoning_capability") or "")
            capability = capabilities.get(name)
            if not isinstance(capability, dict):
                blockers.append(f"CQ {source_id} 未绑定正式规则能力")
                continue
            source_rule_ids = set(capability.get("source_rule_ids") or [])
            bound_rule_ids = set(route.get("bound_rule_ids") or [])
            runtime_rules = capability.get("rules") or []
            if not source_rule_ids:
                blockers.append(f"CQ {source_id} 的能力 {name} 缺少 source_rule_ids")
            if not source_rule_ids.intersection(bound_rule_ids):
                blockers.append(
                    f"CQ {source_id} 的 S2 路由 bound_rule_ids 与 S3 能力 {name}.source_rule_ids 无交集"
                )
            unknown_ids = sorted(source_rule_ids - rule_by_id.keys())
            if unknown_ids:
                blockers.append(f"CQ {source_id} 引用了未登记的 S2 规则：{', '.join(unknown_ids)}")
            if not isinstance(runtime_rules, list) or any(
                not isinstance(row, dict) for row in runtime_rules
            ):
                blockers.append(f"CQ {source_id} 的能力 {name}.rules 不是规则数组")
                runtime_rules = []
            runtime_rule_ids = [str(row.get("rule_id") or "") for row in runtime_rules]
            if set(runtime_rule_ids) != source_rule_ids or len(runtime_rule_ids) != len(source_rule_ids):
                blockers.append(f"CQ {source_id} 的能力 {name}.rules 与 source_rule_ids 不一致")
            for row in runtime_rules:
                rule_id = str(row.get("rule_id") or "")
                registered = rule_by_id.get(rule_id)
                if not registered:
                    continue
                if " ".join(str(row.get("expression") or "").split()) != " ".join(
                    str(registered.get("formal_expression") or "").split()
                ):
                    blockers.append(f"CQ {source_id} 的规则 {rule_id} 表达式与 S2 正式值不同")
                if registered.get("status") not in {"DATABASE_FACT", "DOCUMENT_EVIDENCE"}:
                    blockers.append(f"CQ {source_id} 的规则 {rule_id} 未标记为正式生产来源")
                if registered.get("review_required"):
                    pending_rule_review_ids.add(rule_id)
            derived = set(contract.get("derived_predicates") or [])
            if not derived or not derived.issubset(set(capability.get("result_predicates") or [])):
                blockers.append(f"CQ {source_id} 的派生谓词未被能力 {name} 的输出覆盖")
        elif answer_mode in {"FACT_QUERY", "EVIDENCE_QUERY"}:
            if contract.get("reasoning_capability") or contract.get("derived_predicates"):
                blockers.append(f"CQ {source_id} 的事实路由混入推理结论")
        else:
            blockers.append(f"CQ {source_id} 需要未由正式规则覆盖的 OWL 或未知推理模式")
    if not rule_question_count:
        blockers.append("没有经 S3 已审绑定的规则推理 CQ")
    basis = {
        "source_sha256": source_sha256,
        "question_ids": sorted(verified_question_ids),
        "answer_contracts": [q.get("answer_contract") for q in questions],
        "rule_ids": sorted(rule_by_id),
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "policy_version": RULE_POLICY_VERSION,
        "status": "REQUIRED" if blockers else "NOT_APPLICABLE",
        "axiom_count": 0,
        "reason": (
            "；".join(dict.fromkeys(blockers)) if blockers else
            "全部原始 CQ 已绑定已审事实或正式规则推理能力，规则表达式与 S2 一致且无 OWL 推理需求；无需虚构 OWL 公理。"
            + (
                "待人工评审的规则须在 S4 联合设计批准理由中逐项确认："
                + "、".join(sorted(pending_rule_review_ids)) + "。"
                if pending_rule_review_ids else ""
            )
            + "型别、Mapping、SHACL、实例 CQ、规则执行及发布问答仍须逐项验证。"
        ),
        "verified_question_ids": sorted(verified_question_ids),
        "pending_rule_review_ids": sorted(pending_rule_review_ids),
        "source_sha256": source_sha256,
        "evidence_fingerprint": digest,
    }


def logical_axiom_applicability(
    *,
    state: dict[str, Any],
    design: dict[str, Any],
    intake: dict[str, Any],
    capability_plan: dict[str, Any],
    runtime: dict[str, Any],
    rules: Any,
    verified_question_ids: set[str],
    source_sha256: dict[str, str],
    missing_sources: list[str],
) -> dict[str, Any]:
    """Fail closed unless all frozen structured CQ routes prove factual lookup."""
    count = len(design.get("logical_axioms") or [])
    if count:
        return {
            "policy_version": POLICY_VERSION,
            "status": "REQUIRED",
            "axiom_count": count,
            "reason": "保留并逐条验证已定义公理；不通过轻量适用性删除现有语义。",
        }
    if (
        isinstance(state, dict) and isinstance(runtime, dict)
        and runtime.get("reasoning_requirement") == "REQUIRED"
        and isinstance(rules, list) and rules
    ):
        return _rule_backed_applicability(
            state=state, design=design, intake=intake, capability_plan=capability_plan,
            runtime=runtime, rules=rules, verified_question_ids=verified_question_ids,
            source_sha256=source_sha256, missing_sources=missing_sources,
        )
    blockers: list[str] = []

    def mapping(value: Any, label: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        blockers.append(f"{label} 不是合法对象")
        return {}

    def rows(value: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            blockers.append(f"{label} 不是合法对象数组")
            return []
        return value

    state = mapping(state, "workflow state")
    intake = mapping(intake, "CQ intake")
    capability_plan = mapping(capability_plan, "capability plan")
    runtime = mapping(runtime, "runtime")
    try:
        schema_version = int(runtime.get("schema_version") or 0)
    except (ValueError, TypeError):
        schema_version = 0
    if state.get("stage_contract_version") != STAGE_CONTRACT_VERSION:
        blockers.append("仅适用于明确使用 v2 契约的新评审")
    if state.get("intake_mode") != "DATABASE_ONLY":
        blockers.append("仅 DATABASE_ONLY 已有完整事实查询执行路径适用")
    if missing_sources:
        blockers.append("缺少正式证据：" + ", ".join(missing_sources))
    if any(
        mapping(state.get("stage_statuses") or {}, "stage_statuses").get(stage) != "PASSED"
        for stage in ("S1", "S2", "S3")
    ):
        blockers.append("S1/S2/S3 尚未正式通过")
    if not isinstance(rules, list) or rules:
        blockers.append("正式 S2 规则候选必须明确为空")
    if (
        schema_version < 4
        or runtime.get("review_status") != "REVIEWED"
        or runtime.get("reasoning_requirement") != "NOT_APPLICABLE"
        or len(str(runtime.get("reasoning_not_applicable_reason") or "").strip()) < 12
        or runtime.get("reasoning_capabilities")
        or runtime.get("document_fact_queries")
    ):
        blockers.append("S3 未证明规则推理不适用的正式纯结构化运行契约")
    source_questions = rows(intake.get("questions") or [], "source questions")
    source_ids = [str(q.get("id") or "") for q in source_questions]
    questions = rows(design.get("competency_questions") or [], "design questions")
    question_ids = [str(q.get("source_question_id") or "") for q in questions]
    if (
        not source_ids
        or "" in source_ids
        or len(set(source_ids)) != len(source_ids)
        or len(question_ids) != len(source_ids)
        or set(question_ids) != set(source_ids)
        or verified_question_ids != set(source_ids)
    ):
        blockers.append("所有原始 CQ 必须被唯一且经服务端重编一致的 S3 查询绑定完整覆盖")
    routes = rows(capability_plan.get("routes") or [], "capability routes")
    route_ids = [str(r.get("question_id") or "") for r in routes]
    if (
        capability_plan.get("status") != "READY"
        or len(route_ids) != len(source_ids)
        or set(route_ids) != set(source_ids)
    ):
        blockers.append("S2 能力计划未完整覆盖原始 CQ")
    for route in routes:
        if (
            route.get("question_type") not in {"FACT_QUERY", "EVIDENCE_QUERY"}
            or route.get("support_status") != "READY"
            or route.get("reasoning_capabilities")
            or route.get("bound_rule_ids")
            or route.get("missing_capabilities")
            or route.get("missing_source_requirements")
            or not isinstance(route.get("query_engines"), list)
            or "ONTOP_FACT_QUERY_V1" not in (route.get("query_engines") or [])
            or not any(
                r.get("requirement") == "VERSIONED_READ_ONLY_DATASET" and r.get("status") == "READY"
                for r in rows(route.get("source_readiness") or [], "source readiness")
            )
        ):
            blockers.append(f"CQ {route.get('question_id')} 的正式能力路由不是已就绪事实查询")
    for question in questions:
        contract = mapping(question.get("answer_contract") or {}, "answer contract")
        if (
            contract.get("answer_mode") not in {"FACT_QUERY", "EVIDENCE_QUERY"}
            or not contract.get("reviewed_runtime_binding")
            or not contract.get("result_assertions")
            or not contract.get("boundary_assertions")
            or contract.get("reasoning_capability")
            or contract.get("derived_predicates")
        ):
            blockers.append(f"CQ {question.get('id')} 未完整冻结事实回答、结果与边界断言")
    basis = {
        "source_sha256": source_sha256,
        "question_ids": sorted(verified_question_ids),
        "answer_contracts": [q.get("answer_contract") for q in questions],
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {
        "policy_version": POLICY_VERSION,
        "status": "REQUIRED" if blockers else "NOT_APPLICABLE",
        "axiom_count": 0,
        "reason": (
            "；".join(dict.fromkeys(blockers))
            if blockers
            else "全部原始 CQ 已绑定正式结构化事实查询与结果/边界断言，S2/S3 无规则或 OWL 推理需求；无需为数量门禁新增无业务依据公理。型别、Mapping、SHACL、完整实例 CQ 与发布运行验证仍全部必需。"
        ),
        "verified_question_ids": sorted(verified_question_ids),
        "source_sha256": source_sha256,
        "evidence_fingerprint": digest,
    }
