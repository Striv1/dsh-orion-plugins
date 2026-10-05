"""CQ design-time review. Never grants stage approval or validates instance data."""
from __future__ import annotations

import re
from typing import Any

ANSWER_KINDS = {"FACT_LOOKUP", "AGGREGATION", "RELATION", "RULE_INFERENCE", "DOCUMENT_EVIDENCE"}
LIST_FIELDS = ("definition_source_refs", "required_candidate_ids", "required_rule_ids",
               "missing_semantics", "required_fact_descriptions", "missing_data")
FIELDS = {"question_id", "answer_kind", "business_definition", "requires_business_confirmation",
          "premise_bindings", *LIST_FIELDS}


def validate_assessments(raw: Any, *, questions: list[dict], candidates: list[dict],
                         rules: list[dict], known_refs: set[str]) -> None:
    if raw is None:  # Existing callers remain compatible, but are not called assessed.
        return
    if not isinstance(raw, list):
        raise ValueError("cq_semantic_assessments 必须是数组。")
    question_ids = {str(q["id"]) for q in questions}
    candidate_ids = {str(c["id"]) for c in candidates}
    rule_by_id = {str(r["id"]): r for r in rules}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - FIELDS or FIELDS - {"premise_bindings"} - set(item):
            raise ValueError("CQ 语义检查缺少必要字段或包含未知字段。")
        qid = item["question_id"]
        if not isinstance(qid, str) or qid not in question_ids or qid in seen:
            raise ValueError("CQ 语义检查必须唯一引用原始 S0 question_id。")
        seen.add(qid)
        if not isinstance(item["answer_kind"], str) or item["answer_kind"] not in ANSWER_KINDS or type(item["requires_business_confirmation"]) is not bool:
            raise ValueError(f"{qid} 的问题类型或业务确认标记不合法。")
        for field in LIST_FIELDS:
            values = item[field]
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f"{qid}.{field} 必须为非空字符串数组（允许空数组）。")
            if len(values) != len(set(values)):
                raise ValueError(f"{qid}.{field} 不能重复。")
        if not isinstance(item["business_definition"], str) or (
            not item["business_definition"].strip() and not item["missing_semantics"]
        ):
            raise ValueError(f"{qid} 必须解释业务定义，或明确列出缺失语义。")
        if not set(item["required_candidate_ids"]).issubset(candidate_ids):
            raise ValueError(f"{qid} 引用了不存在的候选。")
        if not set(item["required_rule_ids"]).issubset(rule_by_id):
            raise ValueError(f"{qid} 引用了不存在的规则。")
        for rid in item["required_rule_ids"]:
            if qid not in rule_by_id[rid].get("business_question_ids", []):
                raise ValueError(f"{qid} 与规则 {rid} 的业务问题血缘不一致。")
        refs = list(item["definition_source_refs"])
        bindings = item.get("premise_bindings", [])
        if not isinstance(bindings, list):
            raise ValueError(f"{qid}.premise_bindings 必须为数组。")
        seen_bindings: set[tuple[str, str]] = set()
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"rule_id", "predicate", "source_refs"}:
                raise ValueError(f"{qid} 前提绑定字段不合法。")
            rid, predicate = binding["rule_id"], binding["predicate"]
            if not isinstance(rid, str) or not isinstance(predicate, str):
                raise ValueError(f"{qid} 前提绑定必须引用规则与谓词。")
            if rid not in item["required_rule_ids"] or predicate not in rule_by_id[rid].get("premise_predicates", []):
                raise ValueError(f"{qid} 前提绑定必须来自本 CQ 所需规则的前提。")
            if (rid, predicate) in seen_bindings:
                raise ValueError(f"{qid} 前提绑定重复。")
            seen_bindings.add((rid, predicate))
            br = binding["source_refs"]
            if not isinstance(br, list) or not br or any(not isinstance(v, str) or not v.strip() for v in br):
                raise ValueError(f"{qid} 前提绑定必须有真实来源引用。")
            refs.extend(br)
        if any(ref not in known_refs and ref.split("#", 1)[0] not in known_refs for ref in refs):
            raise ValueError(f"{qid} 引用了未登记来源；测试事实、任意候选引用不能证明生产来源。")
    if seen != question_ids:
        raise ValueError("提交 CQ 语义检查时必须覆盖全部原始问题；缺失项应明确列为缺口，不能省略。")


def _grounded(rule: dict, path: frozenset[str], bindings: set[tuple[str, str]],
              producers: dict[str, list[dict]]) -> bool:
    """A derived premise needs a source-grounded producer; cycles alone are insufficient."""
    rid = rule["id"]
    if rid in path:
        return False
    premises = rule.get("premise_predicates") or []
    return bool(premises) and all(
        (rid, predicate) in bindings or any(
            _grounded(producer, path | {rid}, bindings, producers)
            for producer in producers.get(predicate, [])
        ) for predicate in premises
    )


def build_review(*, questions: list[dict], candidates: list[dict], rules: list[dict],
                 assessments: list[dict] | None, intake_mode: str,
                 structured_source_present: bool, document_instances_present: bool = False) -> dict:
    """Derive conservative statuses; source links are not proof of query correctness."""
    by_question = {a["question_id"]: a for a in assessments or []}
    candidates_by_id = {c["id"]: c for c in candidates}
    rules_by_id = {r["id"]: r for r in rules}
    rows = []
    for q in questions:
        qid = q["id"]
        a = by_question.get(qid)
        bound = [r for r in rules if qid in r.get("business_question_ids", [])]
        text = f"{q.get('question', '')} {q.get('expected', '')}"
        # Only flag potential policy gaps; never infer a rule or threshold from words.
        potential_rule = bool(bound) or bool(re.search(r"判定|预警|风险|合规|推理|推断|达标|资格|eligib|risk|compliant", text, re.I))
        kind = a["answer_kind"] if a else ("RULE_INFERENCE" if potential_rule else "UNASSESSED")
        missing_semantics = list(a["missing_semantics"]) if a else ["尚未逐 CQ 核对业务定义、范围、时间与输出口径。"]
        missing_data = list(a["missing_data"]) if a else []
        needed_rule_ids = list(a["required_rule_ids"]) if a else [r["id"] for r in bound]
        # Include all CQ-linked rules, so omitting a dependency cannot make it disappear.
        needed_rule_ids = list(dict.fromkeys([*needed_rule_ids, *(r["id"] for r in bound)]))
        needed_rules = [rules_by_id[rid] for rid in needed_rule_ids if rid in rules_by_id]
        needs_rule = potential_rule or kind == "RULE_INFERENCE"
        missing_rule = needs_rule and not needed_rules
        if missing_rule:
            missing_semantics.append("该 CQ 涉及业务判定，但没有关联规则；需核实定义，不自动推测政策。")
        conclusion_rules: dict[str, list[dict]] = {}
        for producer in needed_rules:
            conclusion_rules.setdefault(producer.get("conclusion_predicate", ""), []).append(producer)
        bindings = {(b["rule_id"], b["predicate"]) for b in (a or {}).get("premise_bindings", [])}
        unbound: list[str] = []
        for rule in needed_rules:
            for predicate in rule.get("premise_predicates") or []:
                if (rule["id"], predicate) not in bindings and not (
                    any(_grounded(producer, frozenset({rule["id"]}), bindings, conclusion_rules)
                        for producer in conclusion_rules.get(predicate, []))
                ):
                    unbound.append(f"{rule['id']}:{predicate}")
        uncertain_rules = any(
            r.get("review_required") or r.get("status") not in {"DATABASE_FACT", "DOCUMENT_EVIDENCE"}
            for r in needed_rules
        )
        needs_decision = bool(a and a["requires_business_confirmation"]) or uncertain_rules
        if not a:
            business = "UNASSESSED"
        elif needs_decision:
            business = "NEEDS_DECISION"
        elif missing_semantics or not a["definition_source_refs"]:
            business = "UNASSESSED"
        else:
            business = "SOURCE_LINKED"
        candidate_ids = list((a or {}).get("required_candidate_ids", []))
        model = "UNASSESSED" if not a else (
            "INCOMPLETE" if missing_semantics or missing_rule or unbound or not candidate_ids
            or any(cid not in candidates_by_id for cid in candidate_ids) else "CANDIDATE"
        )
        requires_instances = kind != "DOCUMENT_EVIDENCE" or bool((a or {}).get("required_fact_descriptions"))
        if missing_data:
            data = "MISSING"
        elif not requires_instances:
            data = "NOT_REQUIRED"
        elif (intake_mode == "DOCUMENT_ONLY" and not document_instances_present):
            data = "MISSING"
            if not missing_data:
                missing_data.append("已登记资料不足以证明该 CQ 所需个体事实；须提供对应来源或声明可核验文档事实。")
        else:
            data = "UNVERIFIED"
            if not structured_source_present and not document_instances_present:
                missing_data.append("尚无已登记、可回读的实例来源。")
                data = "MISSING"
        action = (
            "PREPARE_BUSINESS_DECISION" if needs_decision else
            "COMPLETE_CQ_SEMANTICS" if business == "UNASSESSED" else
            "COMPLETE_MODEL_OR_RULE_BINDINGS" if model != "CANDIDATE" else
            "WAITING_FOR_SOURCE" if data == "MISSING" else "VALIDATE_SOURCE_AND_QUERY"
        )
        rows.append({
            "question_id": qid, "question": q.get("question", ""), "answer_kind": kind,
            "business_status": business, "model_status": model, "data_status": data,
            "validation_status": "NOT_VALIDATED", "missing_semantics": missing_semantics,
            "missing_data": missing_data, "unbound_premises": sorted(set(unbound)),
            "required_candidate_ids": candidate_ids, "required_rule_ids": needed_rule_ids,
            "business_definition": (a or {}).get("business_definition", ""),
            "definition_source_refs": (a or {}).get("definition_source_refs", []),
            "next_action": action,
        })
    return {
        "schema_version": 1, "scope": "DESIGN_TIME_ASSESSMENT",
        "status": "CANDIDATE" if rows and all(
            r["business_status"] == "SOURCE_LINKED" and r["model_status"] == "CANDIDATE" and r["data_status"] != "MISSING"
            for r in rows) else "NEEDS_REVIEW",
        "questions": rows,
        "business_decision_requests": [
            {"id": f"CQ-SEMANTICS-{r['question_id']}", "business_question_ids": [r["question_id"]],
             "reason": r["missing_semantics"] or ["业务规则仍需确认；先复用现有 S3 决定。"],
             "required_rule_ids": r["required_rule_ids"], "proposed_policy": None}
            for r in rows if r["business_status"] == "NEEDS_DECISION"
        ],
        "notice": "仅评估设计完整性。来源关联不代表政策已批准、规则已执行或生产数据已验证；不改变阶段门禁。",
    }
