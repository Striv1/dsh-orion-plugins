"""CQ capability routing rules, independent of workflow state and storage."""
from __future__ import annotations

import re
from typing import Any

from services.ontology_contracts.errors import WorkflowGateError
from services.realtime_qa.capabilities import (
    CLOSED_WORLD_SET_DIFFERENCE_V1,
    HERMIT_OWL_DL_VALIDATION_V1,
    OWL_RL_CLOSURE_V1,
    SEMANTICA_FORWARD_V1,
    SHACL_VALIDATION_V1,
)

REASONING_QUESTION_PATTERN = re.compile(
    r"(?:能否|是否|判定|判断|推断|推出|缺件|合规判定|责任判定|处置判定|风险|异常|"
    r"需要补齐|具备[^。；，,]*资格|资格[^。；，,]*判定|材料[^。；，,]*齐全|"
    r"负有[^。；，,]*义务|构成[^。；，,]*(?:违法|违规|责任)|"
    r"违法[^。；，,]*(?:认定|责任|处理))",
    re.IGNORECASE,
)
FACT_AGGREGATION_PATTERN = re.compile(r"(?:数量|计数|分布|统计|多少)", re.IGNORECASE)
STRONG_REASONING_PATTERN = re.compile(
    r"(?:能否|是否|判定|判断|推断|推出|缺件|合规|责任|处置|异常|疑似|需要补齐|具备[^。；，,]*资格)",
    re.IGNORECASE,
)


def requires_reasoning(question: str, expected: str) -> bool:
    """Distinguish classification/inference from factual risk aggregation.

    A question such as "风险等级与状态数量分布" contains the word 风险 but is
    still a database aggregation. Treating every occurrence of 风险 as inference
    forces an unrelated Semantica capability and silently changes the question.
    """

    semantic_text = f"{question}\n{expected}"
    if FACT_AGGREGATION_PATTERN.search(semantic_text) and not STRONG_REASONING_PATTERN.search(
        semantic_text
    ):
        return False
    return REASONING_QUESTION_PATTERN.search(semantic_text) is not None


def classify_capability_question(question: str, expected: str) -> str:
    text = f"{question}\n{expected}"
    if re.search(r"(?:缺少|缺件|未提交|没有提交|NOT\s+EXISTS|不存在)", text, re.I):
        return "CLOSED_WORLD_INFERENCE"
    if re.search(r"(?:继承|子类|等价类|类型推断|分类推理|传递关系)", text, re.I):
        return "OWL_INFERENCE"
    if requires_reasoning(question, expected):
        return "RULE_INFERENCE"
    if re.search(r"(?:资料|文档|条款|原文|证据)", text, re.I):
        return "DOCUMENT_EVIDENCE"
    return "FACT_QUERY"


def build_capability_plan(
    *,
    questions: list[dict[str, Any]],
    intake_mode: str,
    business_rules: list[dict[str, Any]],
    catalog: dict[str, Any],
    available_sources: set[str],
    semantic_review: dict[str, Any] | None = None,
    cq_semantic_assessments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan routes from supplied evidence availability; never read runtime state."""
    assessments = {}
    for row in (semantic_review or {}).get("questions", []):
        assessments.setdefault(row["question_id"], row)
    rules_by_question: dict[str, list[dict[str, Any]]] = {}
    for rule in business_rules:
        for question_id in rule.get("business_question_ids") or []:
            rules_by_question.setdefault(str(question_id), []).append(rule)
    supported_reasoning = set(catalog["reasoning"])
    routes: list[dict[str, Any]] = []
    for index, item in enumerate(questions, start=1):
        question_id = str(item.get("id") or f"CQ-{index:03d}")
        question = str(item.get("question") or "")
        expected = str(item.get("expected") or "")
        question_type = classify_capability_question(question, expected)
        engines: list[str] = []
        required_capabilities: list[str] = []
        source_requirements: list[str] = []
        bound_rules = rules_by_question.get(question_id, [])
        if intake_mode in {"DATABASE_ONLY", "HYBRID"}:
            engines.append("ONTOP_FACT_QUERY_V1")
            source_requirements.append("VERSIONED_READ_ONLY_DATASET")
        if intake_mode in {"DOCUMENT_ONLY", "HYBRID"}:
            engines.append("FUSEKI_DOCUMENT_EVIDENCE_V1")
            source_requirements.append("VERSIONED_DOCUMENT_EVIDENCE")
        if question_type == "RULE_INFERENCE":
            required_capabilities.append(SEMANTICA_FORWARD_V1)
        elif question_type == "CLOSED_WORLD_INFERENCE":
            required_capabilities.extend([CLOSED_WORLD_SET_DIFFERENCE_V1, SEMANTICA_FORWARD_V1])
            source_requirements.extend(
                ["COMPLETE_CASE_SNAPSHOT", "EXPLICIT_CLOSED_WORLD_PREDICATE"]
            )
        elif question_type == "OWL_INFERENCE":
            required_capabilities.append(OWL_RL_CLOSURE_V1)
        for rule in bound_rules:
            required_capabilities.extend(
                str(value).strip()
                for value in rule.get("required_capabilities") or []
                if str(value).strip()
            )
            if str(rule.get("rule_type") or "").upper() == "CLOSED_WORLD_SET_DIFFERENCE":
                source_requirements.extend(
                    ["COMPLETE_CASE_SNAPSHOT", "EXPLICIT_CLOSED_WORLD_PREDICATE"]
                )
        # HermiT and SHACL are always release-quality validators, not query
        # substitutes. Listing them here makes the end-to-end route explicit.
        validation_capabilities = [
            HERMIT_OWL_DL_VALIDATION_V1,
            SHACL_VALIDATION_V1,
        ]
        required_capabilities = list(dict.fromkeys(required_capabilities))
        source_requirements = list(dict.fromkeys(source_requirements))
        missing = sorted(set(required_capabilities) - supported_reasoning)
        source_readiness: list[dict[str, str]] = []
        for requirement in source_requirements:
            if requirement in {"VERSIONED_READ_ONLY_DATASET", "VERSIONED_DOCUMENT_EVIDENCE"}:
                ready = requirement in available_sources
            elif requirement == "COMPLETE_CASE_SNAPSHOT":
                ready = bool(bound_rules) and all(
                    bool(rule.get("closed_world_inputs"))
                    and bool(rule.get("required_set_source"))
                    for rule in bound_rules
                )
            elif requirement == "EXPLICIT_CLOSED_WORLD_PREDICATE":
                ready = bool(bound_rules) and all(
                    all(
                        str(item.get("predicate") or "").strip()
                        for item in rule.get("closed_world_inputs") or []
                        if isinstance(item, dict)
                    )
                    for rule in bound_rules
                )
            else:
                ready = True
            source_readiness.append(
                {
                    "requirement": requirement,
                    "status": "READY" if ready else "MISSING",
                }
            )
        missing_sources = sorted(
            item["requirement"]
            for item in source_readiness
            if item["status"] == "MISSING"
        )
        routes.append(
            {
                "question_id": question_id,
                "question_type": question_type,
                "query_engines": list(dict.fromkeys(engines)),
                "reasoning_capabilities": required_capabilities,
                "validation_capabilities": validation_capabilities,
                "source_requirements": source_requirements,
                "source_readiness": source_readiness,
                "bound_rule_ids": sorted(
                    str(rule.get("id") or "") for rule in bound_rules
                ),
                "support_status": (
                    "READY" if not missing and not missing_sources else "BLOCKED"
                ),
                "missing_capabilities": missing,
                "missing_source_requirements": missing_sources,
                "semantic_assessment": assessments.get(question_id),
            }
        )
    unsupported = [
        route
        for route in routes
        if route["missing_capabilities"] or route["missing_source_requirements"]
    ]
    return {
        "schema_version": 1,
        "policy_version": "cq-capability-routing-v1",
        "intake_mode": intake_mode,
        "status": "READY" if not unsupported else "BLOCKED",
        "question_count": len(routes),
        "routes": routes,
        "routing_status": "READY" if not unsupported else "BLOCKED",
        "readiness_scope": "CAPABILITY_ROUTING_ONLY",
        "engineering_status": (semantic_review or {}).get("status", "NEEDS_REVIEW"),
        "semantic_review": semantic_review,
        "cq_semantic_assessments": cq_semantic_assessments,
    }


def require_ready_capability_plan(capability_plan: dict[str, Any]) -> None:
    """One capability gate for S2 preview and formal submission."""
    if capability_plan["status"] != "READY":
        missing = sorted(
            {
                capability
                for route in capability_plan["routes"]
                for capability in route["missing_capabilities"]
            }
        )
        missing_sources = sorted(
            {
                requirement
                for route in capability_plan["routes"]
                for requirement in route["missing_source_requirements"]
            }
        )
        details: list[str] = []
        if missing:
            details.append("缺少平台能力：" + ", ".join(missing))
        if missing_sources:
            details.append("缺少来源前提：" + ", ".join(missing_sources))
        raise WorkflowGateError(
            "G-S2-PLATFORM-CAPABILITY",
            "；".join(details),
        )
