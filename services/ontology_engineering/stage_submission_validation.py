"""Read-only diagnostics for stage submissions.

These functions inspect explicit payloads and policy values. They do not read
project files, approve semantics, execute engines, issue tokens or commit stages.
Formal stage validators remain authoritative after this diagnostic pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from services.ontology_contracts import cq_answers
from services.ontology_contracts.errors import WorkflowGateError

from .mapping_preflight import collect_mapping_runtime_issues, normalize_known_literal_datatypes
from .reporting import ONTOLOGY_NAME_LABELS

SEMANTIC_STATUSES = {
    "DATABASE_FACT",
    "DOCUMENT_EVIDENCE",
    "AI_INFERENCE",
    "NEEDS_HUMAN_CONFIRMATION",
}

DATABASE_CLASS_MAPPING_TYPES = {
    "TABLE_TO_CLASS",
    "SQL_TO_CLASS",
    "COLUMN_VALUE_TO_CLASS",
}

RULE_CLASS_MAPPING_TYPES = {"RULE_TO_CLASS"}

DATABASE_OBJECT_MAPPING_TYPES = {
    "FK_TO_OBJECT_PROPERTY",
    "FOREIGN_KEY_TO_OBJECT_PROPERTY",
    "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
    "COLUMN_VALUE_TO_OBJECT_PROPERTY",
    "SQL_TO_OBJECT_PROPERTY",
}

DATABASE_DATA_MAPPING_TYPES = {
    "COLUMN_TO_DATA_PROPERTY",
    "SQL_TO_DATA_PROPERTY",
}

DOCUMENT_MAPPING_TYPES = {
    "EVIDENCE_TO_CLASS",
    "EVIDENCE_TO_OBJECT_PROPERTY",
    "EVIDENCE_TO_DATA_PROPERTY",
}

SUPPORTED_MAPPING_TYPES = (
    DATABASE_CLASS_MAPPING_TYPES
    | RULE_CLASS_MAPPING_TYPES
    | DATABASE_OBJECT_MAPPING_TYPES
    | DATABASE_DATA_MAPPING_TYPES
    | DOCUMENT_MAPPING_TYPES
)


def rule_class_binding_issues(mappings: list[dict], registered_rules: list[dict],
                              runtime: dict | None, namespace: str) -> list[str]:
    return [item["id"] for item in rule_class_binding_diagnostics(mappings, registered_rules, runtime, namespace)]


def rule_class_binding_diagnostics(mappings: list[dict], registered_rules: list[dict],
                                   runtime: dict | None, namespace: str) -> list[dict[str, str]]:
    """A rule class needs an executable, release-bound rule term.

    Without derivation.premise_predicate the mapping declares the reviewed rule's
    conclusion class (members produced by rule execution).  With it, the mapping
    declares a reviewed premise predicate whose members come from a reasoning
    capability's evidence rows via fact_bindings.  Neither produces OBDA.
    """
    by_id = {str(item.get("id") or ""): item for item in registered_rules if isinstance(item, dict)}
    capabilities = (runtime or {}).get("reasoning_capabilities") or {}
    issues = []
    for item in mappings:
        if str(item.get("mapping_type") or "").upper() != "RULE_TO_CLASS":
            continue
        derivation = item.get("derivation") or {}
        rule_id = str(derivation.get("rule_id") or "")
        rule = by_id.get(rule_id) or {}
        premise = str(derivation.get("premise_predicate") or "").strip()
        if premise:
            premises = {str(value).strip() for value in rule.get("premise_predicates") or []}
            predicate = premise if premise in premises else ""
        else:
            predicate = str(rule.get("conclusion_predicate") or "")
        target = str(item.get("target") or "")
        contract = item.get("instance_contract") or {}
        bound = [cap for cap in capabilities.values() if isinstance(cap, dict)
                 and rule_id in (cap.get("source_rule_ids") or [])
                 and _capability_produces(cap, predicate, premise=bool(premise))
                 and (cap.get("ontology_terms") or {}).get(predicate) == namespace + target]
        # A conclusion has exactly one producing capability; a source-backed
        # premise may legitimately be bound by several capabilities of the rule.
        bound_ok = len(bound) >= 1 if premise else len(bound) == 1
        mapping_id = str(item.get("id") or target)
        reason = ""
        if not rule_id or rule_id not in by_id:
            reason = f"derivation.rule_id={rule_id or '（空）'} 不是已审 S2 规则"
        elif premise and not predicate:
            reason = f"derivation.premise_predicate={premise} 不在规则 {rule_id} 的 premise_predicates 中"
        elif not predicate:
            reason = f"规则 {rule_id} 缺少 conclusion_predicate"
        elif not target:
            reason = "target 为空"
        elif contract.get("generation_mode") != "RULE_DERIVED":
            reason = "instance_contract.generation_mode 必须是 RULE_DERIVED"
        elif not namespace:
            reason = "mapping_draft.namespace 为空"
        elif not bound_ok:
            expected_iri = namespace + target
            role = "fact_bindings 产生前提" if premise else "result_predicates 产出结论"
            candidates = sorted(
                name for name, cap in capabilities.items() if isinstance(cap, dict)
                and rule_id in (cap.get("source_rule_ids") or [])
            )
            wrong_iri = sorted(
                f"{name}→{(cap.get('ontology_terms') or {}).get(predicate) or '（未登记）'}"
                for name, cap in capabilities.items() if isinstance(cap, dict)
                and rule_id in (cap.get("source_rule_ids") or [])
                and _capability_produces(cap, predicate, premise=bool(premise))
                and (cap.get("ontology_terms") or {}).get(predicate) != expected_iri
            )
            if len(bound) > 1:
                reason = (f"谓词 {predicate} 由 {len(bound)} 个推理能力同时{role}："
                          + ", ".join(sorted(name for name, cap in capabilities.items() if cap in bound))
                          + "；结论只能由唯一能力产出，请删除重复能力或把同一规则只保留在一个能力中"
                          "（多规则 CQ 可共用这一个能力）")
            elif wrong_iri:
                reason = f"能力 ontology_terms.{predicate} 须等于 {expected_iri}，当前：" + ", ".join(wrong_iri)
            elif candidates:
                reason = f"引用规则 {rule_id} 的能力 {', '.join(candidates)} 未经 {role} {predicate}"
            else:
                reason = f"没有任何推理能力在 source_rule_ids 中引用规则 {rule_id}"
        if reason:
            issues.append({"id": mapping_id, "reason": reason})
    return issues


def _capability_produces(capability: dict, predicate: str, *, premise: bool) -> bool:
    if premise:
        return any(isinstance(binding, dict) and str(binding.get("predicate") or "") == predicate
                   for binding in capability.get("fact_bindings") or [])
    return predicate in (capability.get("result_predicates") or [])


def enforce_rule_class_bindings(mappings: list[dict], runtime: dict | None,
                                namespace: str, project_dir: Path | None, read_json: Any) -> None:
    if not any(str(item.get("mapping_type") or "").upper() == "RULE_TO_CLASS" for item in mappings):
        return
    rule_path = project_dir / "02-semantic-recognition/business-rule-candidates.json" if project_dir is not None else None
    registered_rules = read_json(rule_path) if rule_path is not None and rule_path.is_file() else []
    diagnostics = rule_class_binding_diagnostics(mappings, registered_rules, runtime, namespace)
    if diagnostics:
        raise WorkflowGateError(
            "G-S3-RULE-CLASS-BINDING",
            "规则类绑定未通过（逐项原因）：" + "；".join(f"{d['id']}：{d['reason']}" for d in diagnostics[:12])
            + "。通用要求：规则类必须引用已审 S2 规则且 generation_mode=RULE_DERIVED："
            "结论类须由唯一运行推理能力把结论谓词绑定到本体类 IRI；"
            "前提类（derivation.premise_predicate）须是该规则 premise_predicates 之一，"
            "由引用该规则的推理能力经 fact_bindings 产生，且 ontology_terms 指向本类 IRI。仅声明类不等于可执行规则。",
        )


def preflight_issue(
    gate: str,
    message: str,
    *,
    path: str | None = None,
    owner: str = "ENGINEERING_AGENT",
) -> dict[str, Any]:
    return {
        "gate": gate,
        "message": message,
        "path": path,
        "owner": owner,
    }


def s2_rule_contract_issues(item: dict[str, Any], path: str) -> list[dict[str, Any]]:
    """Validate rule shape and grounded scenarios, not production operators."""
    from .rule_scenarios import RuleScenarioError, ground_horn_scenario_fires

    issues: list[dict[str, Any]] = []
    rule_id = str(item.get("id") or path)
    name_pattern = r"[A-Za-z_][A-Za-z0-9_:-]{0,127}"
    fact_pattern = re.compile(rf"^({name_pattern})\s*\(([^()]*)\)$")
    atom_pattern = re.compile(rf"(?<![\w:-])({name_pattern})\s*\(([^()]*)\)")
    # Match the existing flat, comma-separated atom representation only.
    # Counts are local to this rule, with no value evaluation or unification.
    arities: dict[str, int] = {}

    def argument_count(arguments: str) -> int:
        return len(arguments.split(",")) if arguments.strip() else 0

    def issue(field: str, reason: str, message: str, **details: Any) -> None:
        issues.append({
            **preflight_issue("G-S2-RULE-SEMANTICS", f"业务规则 {rule_id}：{message}", path=f"{path}.{field}"),
            "reason_code": reason, "details": details,
            "validation_scope": "RULE_SHAPE_AND_PREDICATE_COVERAGE_ONLY",
        })

    premises = item.get("premise_predicates")
    declarations = [
        (f"premise_predicates[{index}]", value) for index, value in enumerate(premises)
    ] if isinstance(premises, list) else []
    declarations.append(("conclusion_predicate", item.get("conclusion_predicate")))
    for field, value in declarations:
        if value is not None and not re.fullmatch(name_pattern, str(value).strip()):
            is_atom = "(" in str(value) or ")" in str(value)
            issue(field, "PREDICATE_DECLARATION_IS_ATOM" if is_atom else "UNSUPPORTED_PREDICATE_IDENTIFIER",
                  "声明字段只填 ASCII 谓词名，如 ParameterObserved；不得填含参数的整颗原子或中文谓词。中文含义保留在 name/description。",
                  received=value, expected_pattern=f"^{name_pattern}$", example="ParameterObserved")
    expression = " ".join(str(item.get("formal_expression") or "").split())
    parts = re.split(r"\s+THEN\s+", expression, maxsplit=1, flags=re.I)
    positive: set[str] = set()
    negative: set[str] = set()
    expression_valid = bool(re.match(r"^IF\s+", expression, re.I) and len(parts) == 2)
    if expression and not expression_valid:
        issue("formal_expression", "RULE_BODY_FORMAT", "规则正文需要 IF 前提原子 THEN 唯一结论原子。",
              example="IF ParameterObserved(?x) AND ThresholdExceeded(?x) THEN NeedsReview(?x)")
    if expression_valid:
        body = re.sub(r"^IF\s+", "", parts[0], flags=re.I)
        found = re.findall(r"([^\s(),]+)\s*\(", expression)
        unsupported = sorted({name for name in found if not re.fullmatch(name_pattern, name)})
        if unsupported:
            issue("formal_expression", "UNSUPPORTED_PREDICATE_IDENTIFIER",
                  "规则正文包含不支持的谓词标识（如中文谓词）；仅谓词名使用稳定 ASCII 标识，不能因此改写中文参数值或业务编码。",
                  unsupported_predicates=unsupported, expected_pattern=f"^{name_pattern}$")
            expression_valid = False
        if expression_valid:
            for atom_index, atom in enumerate(atom_pattern.finditer(expression)):
                predicate = atom.group(1)
                actual_arity = argument_count(atom.group(2))
                expected_arity = arities.setdefault(predicate, actual_arity)
                if actual_arity != expected_arity:
                    issue("formal_expression", "RULE_ATOM_ARITY_MISMATCH",
                          f"正文中谓词 {predicate} 的参数个数不一致：本规则首次出现为 {expected_arity} 个，此处为 {actual_arity} 个。请核对原始规则，平台不会自动增删参数。",
                          predicate=predicate, expected_arity=expected_arity, actual_arity=actual_arity,
                          atom=atom.group(0), atom_index=atom_index)
        premise_names = re.findall(rf"({name_pattern})\s*\(", body)
        conclusion_names = re.findall(rf"({name_pattern})\s*\(", parts[1])
        negative = set(re.findall(rf"\bNOT\s+({name_pattern})\s*\(", body, re.I))
        positive = set(premise_names) - negative
        if expression_valid and not premise_names:
            issue("formal_expression", "MISSING_PREMISE_ATOM", "IF 部分至少需要一个含参数的前提原子。")
            expression_valid = False
        if expression_valid and len(conclusion_names) != 1:
            issue("formal_expression", "SINGLE_CONCLUSION_REQUIRED", "生产规则仅接受单结论 Horn 规则，THEN 后必须恰好一个结论原子。", detected_conclusions=conclusion_names)
            expression_valid = False
        if expression_valid and isinstance(premises, list) and all(re.fullmatch(name_pattern, str(v).strip()) for v in premises):
            declared = {str(v).strip() for v in premises}
            if set(premise_names) != declared:
                issue("premise_predicates", "PREMISE_DECLARATION_MISMATCH", "formal_expression 与前提/结论谓词声明不一致；前提声明必须精确覆盖 IF 中的谓词名称。",
                      expression_predicates=sorted(set(premise_names)), declared_predicates=sorted(declared))
        conclusion = str(item.get("conclusion_predicate") or "").strip()
        if expression_valid and re.fullmatch(name_pattern, conclusion) and conclusion_names[0] != conclusion:
            issue("conclusion_predicate", "CONCLUSION_DECLARATION_MISMATCH", "formal_expression 与前提/结论谓词声明不一致；结论声明必须是 THEN 后唯一原子的谓词名。",
                  expression_predicate=conclusion_names[0], declared_predicate=conclusion)
    cases = item.get("test_cases")
    for index, case in enumerate(cases if isinstance(cases, list) else []):
        if not isinstance(case, dict):
            continue
        field = f"test_cases[{index}]"
        case_type = str(case.get("case_type") or "").upper()
        expected = str(case.get("expected_outcome") or "").upper()
        if (case_type == "POSITIVE" and expected != "FIRE") or (case_type == "NEGATIVE" and expected != "NO_FIRE"):
            issue(f"{field}.expected_outcome", "CASE_OUTCOME_CONTRACT", "正例必须期望 FIRE，反例必须期望 NO_FIRE；边界用例按业务条件确定。", case_id=case.get("id"))
        facts = case.get("facts")
        if not isinstance(facts, list) or not facts:
            continue
        fact_predicates = set()
        facts_valid = True
        for fact_index, fact in enumerate(facts):
            matched = fact_pattern.fullmatch(str(fact).strip())
            if matched is None:
                facts_valid = False
                issue(f"{field}.facts[{fact_index}]", "INVALID_TEST_FACT_ATOM",
                      "测试事实不是合法谓词表达式：需要 ASCII 谓词名及参数，如 ParameterObserved(sample_1,SC-JYH999,1.02)；不接受中文谓词、裸谓词名或嵌套原子。不要改写业务常量。",
                      received=fact, case_id=case.get("id"))
            else:
                fact_predicates.add(matched.group(1))
                predicate = matched.group(1)
                actual_arity = argument_count(matched.group(2))
                if predicate in arities and actual_arity != arities[predicate]:
                    facts_valid = False
                    issue(f"{field}.facts[{fact_index}]", "TEST_FACT_ARITY_MISMATCH",
                          f"测试事实谓词 {predicate} 有 {actual_arity} 个参数，规则正文声明为 {arities[predicate]} 个。请核对候选场景和参数位置，不能自动改写事实或规则。",
                          predicate=predicate, expected_arity=arities[predicate], actual_arity=actual_arity,
                          received=fact, case_id=case.get("id"))
        if expression_valid and facts_valid and expected in {"FIRE", "NO_FIRE"}:
            missing = sorted(positive - fact_predicates)
            negated_present = sorted(negative & fact_predicates)
            try:
                would_fire = ground_horn_scenario_fires(expression, facts)
            except RuleScenarioError as error:
                issue(f"{field}.facts", "UNSUPPORTED_GROUND_HORN_SCENARIO",
                      str(error), case_id=case.get("id"))
                continue
            if would_fire != (expected == "FIRE"):
                issue(f"{field}.facts", "CASE_GROUND_BINDING_MISMATCH",
                      f"{case_type} 用例与声明结果不一致：按变量、常量及同一记录关联匹配为 {'FIRE' if would_fire else 'NO_FIRE'}；缺少正向前提谓词 {missing}，出现否定谓词 {negated_present}。变量须以 ? 开头，裸参数按常量匹配。请核对候选逻辑场景与期望；不能为通过检查补造事实。S2 只匹配具体事实，不执行数值比较或聚类算法，也不替代 S6 引擎验证。",
                      case_id=case.get("id"), missing_premise_predicates=missing,
                      present_negated_predicates=negated_present, expected_outcome=expected,
                      scenario_outcome="FIRE" if would_fire else "NO_FIRE")
                issues[-1]["validation_scope"] = "GROUND_HORN_SCENARIO_ONLY"
    return issues


def semantic_submission_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    candidates = payload.get("ontology_candidates")
    if not isinstance(candidates, list) or not candidates:
        issues.append(
            preflight_issue(
                "G-S2-REQUIRED",
                "本体候选不能为空。",
                path="ontology_candidates",
            )
        )
    else:
        seen: set[str] = set()
        for index, item in enumerate(candidates):
            path = f"ontology_candidates[{index}]"
            if not isinstance(item, dict):
                issues.append(
                    preflight_issue("G-S2-REQUIRED", "候选必须是对象。", path=path)
                )
                continue
            candidate_id = str(item.get("id") or "").strip()
            if not candidate_id or candidate_id in seen:
                issues.append(
                    preflight_issue(
                        "G-S2-UNIQUE", "候选 id 缺失或重复。", path=f"{path}.id"
                    )
                )
            seen.add(candidate_id)
            if item.get("status") not in SEMANTIC_STATUSES:
                issues.append(
                    preflight_issue(
                        "G-S2-STATUS",
                        f"候选 {candidate_id or index + 1} 的事实等级无效。",
                        path=f"{path}.status",
                    )
                )
            if not item.get("source_refs"):
                issues.append(
                    preflight_issue(
                        "G-S2-EVIDENCE",
                        f"候选 {candidate_id or index + 1} 缺少 source_refs。",
                        path=f"{path}.source_refs",
                    )
                )
            if (
                not str(item.get("name") or "").strip()
                or not str(item.get("kind") or "").strip()
            ):
                issues.append(
                    preflight_issue(
                        "G-S2-REQUIRED",
                        f"候选 {candidate_id or index + 1} 缺少 name 或 kind。",
                        path=path,
                    )
                )
    rules = payload.get("business_rule_candidates") or []
    if not isinstance(rules, list):
        issues.append(
            preflight_issue(
                "G-S2-RULE",
                "business_rule_candidates 必须是数组。",
                path="business_rule_candidates",
            )
        )
    else:
        seen_rules: set[str] = set()
        for index, item in enumerate(rules):
            path = f"business_rule_candidates[{index}]"
            if not isinstance(item, dict):
                issues.append(
                    preflight_issue("G-S2-RULE", "业务规则必须是对象。", path=path)
                )
                continue
            rule_id = str(item.get("id") or "").strip()
            if not rule_id or rule_id in seen_rules:
                issues.append(
                    preflight_issue(
                        "G-S2-RULE", "业务规则 id 缺失或重复。", path=f"{path}.id"
                    )
                )
            seen_rules.add(rule_id)
            for field in (
                "name",
                "rule_type",
                "description",
                "formal_expression",
                "conclusion_predicate",
            ):
                if not str(item.get(field) or "").strip():
                    issues.append(
                        preflight_issue(
                            "G-S2-RULE-CONTRACT",
                            f"业务规则 {rule_id or index + 1} 缺少 {field}。",
                            path=f"{path}.{field}",
                        )
                    )
            if not item.get("source_refs"):
                issues.append(
                    preflight_issue(
                        "G-S2-RULE",
                        f"业务规则 {rule_id or index + 1} 缺少 source_refs。",
                        path=f"{path}.source_refs",
                    )
                )
            if not item.get("business_question_ids"):
                issues.append(
                    preflight_issue(
                        "G-S2-RULE-COVERAGE",
                        f"业务规则 {rule_id or index + 1} 未绑定 business_question_ids。",
                        path=f"{path}.business_question_ids",
                    )
                )
            cases = item.get("test_cases")
            case_types = {
                str(case.get("case_type") or "").upper()
                for case in cases or []
                if isinstance(case, dict)
            }
            missing_cases = sorted({"POSITIVE", "NEGATIVE", "BOUNDARY"} - case_types)
            if missing_cases:
                issues.append(
                    preflight_issue(
                        "G-S2-RULE-CONTRACT",
                        f"业务规则 {rule_id or index + 1} 缺少用例：{', '.join(missing_cases)}。",
                        path=f"{path}.test_cases",
                    )
                )
            issues.extend(s2_rule_contract_issues(item, path))
            from services.ontology_engineering.rule_condition_binding import rule_condition_issues
            issues.extend({
                **preflight_issue("G-S2-RULE-CONDITIONS", issue["message"], path=f"/business_rule_candidates/{index}/" + issue["path"]),
                "reason_code": issue["reason_code"], "validation_scope": "DECLARED_BUSINESS_CONDITIONS",
            } for issue in rule_condition_issues(item, candidates if isinstance(candidates, list) else []))
    return issues


def mapping_submission_issues(payload: dict[str, Any], *, intake_mode: str | None, confirmation_limit: int, business_review_only: bool) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    mapping_draft = payload.get("mapping_draft")
    mappings = mapping_draft.get("mappings") if isinstance(mapping_draft, dict) else None
    if not isinstance(mappings, list) or not mappings:
        issues.append(
            preflight_issue(
                "G-S3-MAPPING",
                "mapping_draft.mappings 不能为空。",
                path="mapping_draft.mappings",
            )
        )
    else:
        seen: set[str] = set()
        for index, item in enumerate(mappings):
            path = f"mapping_draft.mappings[{index}]"
            if not isinstance(item, dict):
                issues.append(
                    preflight_issue("G-S3-MAPPING", "映射必须是对象。", path=path)
                )
                continue
            mapping_id = str(item.get("id") or "").strip()
            if not mapping_id or mapping_id in seen:
                issues.append(
                    preflight_issue(
                        "G-S3-MAPPING", "映射 id 缺失或重复。", path=f"{path}.id"
                    )
                )
            seen.add(mapping_id)
            if not item.get("source_refs"):
                issues.append(
                    preflight_issue(
                        "G-S3-EVIDENCE",
                        f"映射 {mapping_id or index + 1} 缺少 source_refs。",
                        path=f"{path}.source_refs",
                    )
                )
            if str(item.get("mapping_type") or "").upper() not in SUPPORTED_MAPPING_TYPES:
                issues.append(
                    preflight_issue(
                        "G-S3-MAPPING-TYPE",
                        f"映射 {mapping_id or index + 1} 的 mapping_type 不受支持。",
                        path=f"{path}.mapping_type",
                    )
                )
            if not str(item.get("target") or "").strip():
                issues.append(
                    preflight_issue(
                        "G-S3-MAPPING",
                        f"映射 {mapping_id or index + 1} 缺少 target。",
                        path=f"{path}.target",
                    )
                )
            label_zh = str(item.get("target_label_zh") or item.get("label_zh") or ONTOLOGY_NAME_LABELS.get(str(item.get("target") or "")) or "")
            if not re.search(r"[\u4e00-\u9fff]", label_zh):
                issues.append(
                    preflight_issue(
                        "G-S3-CHINESE",
                        f"映射 {mapping_id or index + 1} 缺少有业务含义的中文 target_label_zh（正式提交会被拒并使当前草稿失效）。",
                        path=f"{path}.target_label_zh",
                    )
                )
    confirmations = payload.get("confirmations")
    if not isinstance(confirmations, list):
        issues.append(
            preflight_issue(
                "G-S3-CONFIRMATION", "confirmations 必须是数组。", path="confirmations"
            )
        )
    elif len(confirmations) > confirmation_limit:
        issues.append(
            preflight_issue(
                "G-S3-CONFIRMATION-LIMIT",
                f"高影响建模问题最多 {confirmation_limit} 项。",
                path="confirmations",
            )
        )
    if not business_review_only and intake_mode != "DOCUMENT_ONLY" and not isinstance(
        payload.get("realtime_runtime"), dict
    ):
        issues.append(
            preflight_issue(
                "G-S3-RUNTIME",
                "结构化项目必须提交 realtime_runtime。",
                path="realtime_runtime",
                owner="PLATFORM_COMPILER",
            )
        )
    runtime = payload.get("realtime_runtime")
    if isinstance(mappings, list) and isinstance(runtime, dict):
        mapping_obda, _compiler_decisions = normalize_known_literal_datatypes(
            str(runtime.get("mapping_obda") or "")
        )
        for runtime_issue in collect_mapping_runtime_issues(mappings, mapping_obda):
            issues.append(
                preflight_issue(
                    runtime_issue["gate"],
                    runtime_issue["message"],
                    path=runtime_issue.get("path"),
                    owner="PLATFORM_COMPILER",
                )
            )
    if isinstance(runtime, dict) and isinstance(runtime.get("ontop_queries"), dict):
        from services.ontop_client.ontop_query_lint import collect_runtime_query_issues

        for query_issue in collect_runtime_query_issues(runtime["ontop_queries"]):
            issues.append(
                preflight_issue(
                    query_issue["gate"],
                    query_issue["message"],
                    path=query_issue["path"],
                )
            )
    return issues


def design_submission_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    design = payload.get("ontology_design", payload)
    if (
        "generation_request" not in payload
        and isinstance(design, dict)
        and "classes" in design
    ):
        questions = design.get("competency_questions")
        if not isinstance(questions, list) or not questions:
            issues.append(
                preflight_issue(
                    "G-S4-CQ", "至少需要 1 个业务问题。", path="competency_questions"
                )
            )
        else:
            seen: set[str] = set()
            for index, question in enumerate(questions):
                path = f"competency_questions[{index}]"
                if not isinstance(question, dict):
                    issues.append(
                        preflight_issue("G-S4-CQ", "业务问题必须是对象。", path=path)
                    )
                    continue
                question_id = str(question.get("id") or "").strip()
                if not question_id or question_id in seen:
                    issues.append(
                        preflight_issue(
                            "G-S4-CQ", "业务问题编号缺失或重复。", path=f"{path}.id"
                        )
                    )
                seen.add(question_id)
                sparql = str(question.get("sparql") or "").strip()
                if sparql and cq_answers._sparql_query_type(sparql) is None:
                    issues.append(
                        preflight_issue(
                            "G-S4-CQ",
                            f"业务问题 {question_id or index + 1} 的查询必须是只读 SPARQL。",
                            path=f"{path}.sparql",
                            owner="PLATFORM_COMPILER",
                        )
                    )
                for field in ("question", "expected"):
                    if not str(question.get(field) or "").strip():
                        issues.append(
                            preflight_issue(
                                "G-S4-CQ",
                                f"业务问题 {question_id or index + 1} 缺少 {field}。",
                                path=f"{path}.{field}",
                            )
                        )
    return issues


def build_submission_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for field in ("ontology_owl", "ontology_ttl", "shapes_ttl", "protege_build_report"):
        if payload.get(field) in (None, "", {}):
            issues.append(
                preflight_issue(
                    "G-S5-PAYLOAD", f"S5 缺少 {field}。", path=field, owner="PLATFORM_BUILD"
                )
            )
    return issues


def validation_submission_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    required_reports = {
        "hermit_report": "G-S6-HERMIT",
        "mapping_report": "G-S6-MAPPING",
        "semantic_quality_report": "G-S6-SEMANTIC",
        "competency_question_report": "G-S6-CQ",
        "semantica_report": "G-S6-SEMANTICA",
    }
    if not str(payload.get("materialized_ttl") or "").strip():
        issues.append(
            preflight_issue(
                "G-S6-SHACL",
                "S6 缺少 materialized_ttl。",
                path="materialized_ttl",
                owner="PLATFORM_VALIDATOR",
            )
        )
    for field, gate in required_reports.items():
        report = payload.get(field)
        if not isinstance(report, dict):
            issues.append(
                preflight_issue(
                    gate, f"S6 缺少 {field}。", path=field, owner="PLATFORM_VALIDATOR"
                )
            )
        else:
            allowed_statuses = {"PASSED", "CONSISTENT"}
            if field == "competency_question_report":
                # CQ success is authoritative only after the service has
                # executed the bound queries and answer contracts.  The
                # caller must not forge PASSED before that validation.
                allowed_statuses.add("SUBMITTED_FOR_SERVER_VALIDATION")
            if str(report.get("status") or "").upper() in allowed_statuses:
                continue
            issues.append(
                preflight_issue(
                    gate,
                    f"{field} 尚未通过。",
                    path=f"{field}.status",
                    owner="PLATFORM_VALIDATOR",
                )
            )
    return issues
