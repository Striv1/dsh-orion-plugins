"""Pure design diagnostics. Declarations and resolved references are not business proof.

This module never grants approval, changes gates, reads runtime data or mutates inputs.
Missing optional semantics are review questions, not invented business requirements.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

CLASS_KINDS = {"CLASS", "OWL:CLASS", "CONCEPT", "ENTITY", "BUSINESS_OBJECT", "类", "实体"}
RELATION_KINDS = {"OBJECT_PROPERTY", "OBJECTPROPERTY", "OWL:OBJECTPROPERTY", "RELATION", "RELATIONSHIP"}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refs(value: Any) -> list[str]:
    return [v for v in value if _text(v)] if isinstance(value, list) else []


def build_semantic_quality(*, candidates: list[dict], rules: list[dict],
                           questions: list[dict], assessments: list[dict] | None = None,
                           known_source_refs: set[str] | None = None,
                           classes: list[dict] | None = None,
                           relations: list[dict] | None = None) -> dict:
    """Inspect existing candidates/designs; input availability remains explicit.

    Optional design collections are inspected alongside candidates, under distinct
    identities. An empty known_source_refs set means checked against an empty registry;
    None means registry unavailable. Neither source resolution nor text proves truth.
    """
    issues: list[dict] = []
    checks: list[dict] = []
    edges: list[dict] = []

    def issue(code: str, component: str, message: str, *, status: str = "REVIEW_REQUIRED") -> None:
        issues.append({"code": code, "component": component, "status": status, "message": message})

    def rows(values: Any, kind: str) -> list[dict]:
        if not isinstance(values, list):
            issue("INVALID_COLLECTION", kind, "资料集合格式不正确，无法完成诊断。")
            return []
        result = []
        for index, value in enumerate(values):
            if isinstance(value, dict):
                result.append(value)
            else:
                issue("INVALID_ENTRY", f"{kind}:{index}", "该项不是结构化对象。")
        return result

    candidates = rows(candidates, "candidate")
    rules = rows(rules, "rule")
    questions = rows(questions, "cq")
    assessments = rows(assessments, "assessment") if assessments is not None else []
    collections = {"candidate": candidates, "rule": rules, "cq": questions}
    ids: dict[str, set[str]] = {}
    ambiguous: dict[str, set[str]] = {}
    for kind, values in collections.items():
        counts = Counter(item["id"] for item in values if _text(item.get("id")))
        ids[kind] = set(counts)
        ambiguous[kind] = {identity for identity, count in counts.items() if count > 1}
        for identity, count in counts.items():
            if count > 1:
                issue("DUPLICATE_ID", f"{kind}:{identity}", "标识重复，追溯目标存在歧义。")
        for index, item in enumerate(values):
            if not _text(item.get("id")):
                issue("MISSING_ID", f"{kind}:index-{index}", "缺少稳定标识，不能可靠追溯。")

    def declared(component: str, dimension: str, value: Any, missing: str) -> None:
        present = _text(value)
        checks.append({"component": component, "dimension": dimension,
                       "status": "DECLARED" if present else "UNKNOWN",
                       "business_validation_status": "NOT_VALIDATED"})
        if not present:
            issue("MISSING_" + dimension.upper(), component, missing, status="UNKNOWN")

    def sources(component: str, value: Any) -> None:
        refs = _refs(value)
        invalid = value is not None and (not isinstance(value, list) or len(refs) != len(value))
        if invalid:
            issue("INVALID_SOURCE_REFS", component, "来源引用必须是非空字符串数组。")
        unresolved = [ref for ref in refs if known_source_refs is not None
                      and ref not in known_source_refs and ref.split("#", 1)[0] not in known_source_refs]
        status = "UNKNOWN" if not refs or known_source_refs is None else ("UNRESOLVED" if unresolved else "REFERENCE_RESOLVED")
        checks.append({"component": component, "dimension": "source_traceability", "status": status,
                       "source_refs": refs, "unresolved_refs": unresolved,
                       "business_validation_status": "NOT_VALIDATED"})
        if unresolved:
            issue("UNRESOLVED_SOURCE", component, "部分来源引用未能在已登记来源中解析。")
        elif not refs:
            issue("MISSING_SOURCE", component, "尚未提供来源引用；需确认定义的依据。", status="UNKNOWN")

    def edge(source: str, kind: str, target: str, relation: str) -> None:
        resolved = target in ids[kind]
        status = "AMBIGUOUS" if target in ambiguous[kind] else "REFERENCE_RESOLVED" if resolved else "UNRESOLVED"
        edges.append({"source": source, "target": f"{kind}:{target}", "relation": relation,
                      "status": status})
        if not resolved:
            issue("UNRESOLVED_DEPENDENCY", source, f"引用的 {kind} 标识不存在：{target}")

    def inspect(item: dict, kind: str, index: int, category: str) -> None:
        identity = next((item[k] for k in ("id", "iri", "name") if _text(item.get(k))), f"index-{index}")
        component = f"{kind}:{identity}"
        if category == "class":
            contract = item.get("instance_contract")
            contract = contract if isinstance(contract, dict) else {}
            declared(component, "instance_grain", contract.get("instance_meaning"), "需说明一个实例代表的业务粒度。")
            declared(component, "identity_rule", contract.get("identity_rule"), "需说明同一对象的识别规则，并用来源数据验证。")
            checks.append({"component": component, "dimension": "identity_correctness", "status": "UNKNOWN",
                           "business_validation_status": "NOT_VALIDATED"})
        elif category == "relation":
            declared(component, "relation_definition", item.get("description_zh") or item.get("description"), "需核对关系的业务含义；名称不能代替定义。")
            # S2 relation candidates have no endpoint fields in their contract.
            # Do not report future S4 design obligations as current-stage defects.
            deferred = kind == "candidate" and "domain" not in item and "range" not in item
            for field in ("domain", "range"):
                endpoint = item.get(field)
                present = _text(endpoint) or bool(_refs(endpoint))
                status = "DEFERRED_TO_DESIGN" if deferred else "DECLARED" if present else "UNKNOWN"
                checks.append({"component": component, "dimension": field, "status": status,
                               "business_validation_status": "NOT_VALIDATED"})
                if not present and not deferred:
                    issue("MISSING_" + field.upper(), component, "关系端点尚未声明，需核对方向和对象粒度。", status="UNKNOWN")
        if category in {"class", "relation"}:
            # Existing contracts do not guarantee explicit temporal semantics. Do not
            # require invented fields or infer timelessness from their absence.
            checks.append({"component": component, "dimension": "temporal_semantics", "status": "UNKNOWN",
                           "message": "需核对时间适用性；当前诊断未验证有效时间、状态变化或历史口径。",
                           "business_validation_status": "NOT_VALIDATED"})
        mapping_refs = _refs(item.get("source_mapping_ids"))
        if kind in {"class", "relation"} and not _refs(item.get("source_refs")) and mapping_refs:
            # Compiled designs retain lineage through reviewed mappings. This
            # declaration does not prove the mappings or their sources are valid.
            checks.append({"component": component, "dimension": "source_traceability",
                           "status": "MAPPING_REFERENCE_DECLARED", "mapping_refs": mapping_refs,
                           "business_validation_status": "NOT_VALIDATED"})
        else:
            sources(component, item.get("source_refs"))
        for qid in _refs(item.get("business_question_ids")):
            edge(component, "cq", qid, "SUPPORTS")

    for index, candidate in enumerate(candidates):
        kind = str(candidate.get("kind", "")).upper()
        inspect(candidate, "candidate", index, "class" if kind in CLASS_KINDS else "relation" if kind in RELATION_KINDS else "other")
    for index, rule in enumerate(rules):
        inspect(rule, "rule", index, "rule")
    for category, values in (("class", classes), ("relation", relations)):
        if values is not None:
            for index, item in enumerate(rows(values, category)):
                inspect(item, category, index, category)

    assessed: set[str] = set()
    for index, assessment in enumerate(assessments):
        qid = assessment.get("question_id")
        if not _text(qid):
            issue("MISSING_QUESTION_ID", f"assessment:{index}", "语义评估缺少原始问题标识。")
            continue
        component = f"cq:{qid}"
        if qid in assessed:
            issue("DUPLICATE_ASSESSMENT", component, "同一问题存在重复评估。")
        assessed.add(qid)
        if qid not in ids["cq"]:
            issue("UNKNOWN_QUESTION", component, "语义评估未对应已登记业务问题。")
        sources(component, assessment.get("definition_source_refs"))
        for field, kind in (("required_candidate_ids", "candidate"), ("required_rule_ids", "rule")):
            raw = assessment.get(field)
            if raw is not None and (not isinstance(raw, list) or len(_refs(raw)) != len(raw)):
                issue("INVALID_DEPENDENCY_REFS", component, f"{field} 必须为非空字符串数组。")
            for target in _refs(raw):
                edge(component, kind, target, "REQUIRES")
        if assessment.get("requires_business_confirmation"):
            issue("BUSINESS_CONFIRMATION_REQUIRED", component, "业务定义仍标记为需要确认。")
        for field in ("missing_semantics", "missing_data"):
            for gap in _refs(assessment.get(field)):
                issue(field.upper(), component, gap)
    for qid in sorted(ids["cq"] - assessed):
        issue("CQ_UNASSESSED", f"cq:{qid}", "尚无逐问题语义评估，不能推断该问题已覆盖。", status="UNKNOWN")

    return {"schema_version": 1, "scope": "DESIGN_DIAGNOSTICS_ONLY", "status": "REVIEW_REQUIRED" if any(
                item["status"] == "REVIEW_REQUIRED" for item in issues) else "UNKNOWN",
            "business_validation_status": "NOT_VALIDATED", "writes_performed": False,
            "summary": {"candidate_count": len(candidates), "rule_count": len(rules),
                        "question_count": len(questions), "issue_count": len(issues),
                        "check_count": len(checks), "dependency_count": len(edges)},
            "checks": checks, "issues": issues, "traceability": {"edges": edges},
            "notice": "仅诊断声明完整性与引用可解析性；不证明业务正确、映射正确或验收通过，不改变阶段与审批。"}
