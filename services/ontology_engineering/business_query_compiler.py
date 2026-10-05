"""Compile declared business choices into existing query and rule contracts.

No workflow writes, source reads or expected-answer generation. This first
operator set supports connected entity projections, typed scalar comparisons,
and positive unary rules over one entity. Unsupported plans remain diagnostics.
"""
from __future__ import annotations

import copy
import math
import re
from decimal import Decimal

from jsonschema import Draft202012Validator
from rdflib import XSD, Literal, URIRef

from services.ontology_contracts.business_query_plan import BUSINESS_QUERY_PLAN_SCHEMA
from services.ontology_contracts.cq_answers import normalize_expected_rows
from services.ontology_contracts.rule_syntax import parse_atom, validate_rule_expression
from services.ontology_engineering.business_answer_contract import compile_business_answer_bindings
from services.ontology_engineering.business_mapping_scope import resolve_business_mapping_scope
from services.ontology_engineering.business_source_contract import (
    business_evidence_refs,
    source_capability,
)
from services.ontology_engineering.rule_condition_binding import (
    RuleConditionError,
    bind_declared_condition,
    declared_conditions,
)
from services.realtime_qa.cq_contract import compile_reviewed_cq
from services.realtime_qa.query_capabilities import (
    normalize_query_capabilities,
    validate_query_template_contract,
)
from services.realtime_qa.reasoning_contract import normalize_reasoning_capabilities

OPS = {"EQ": "=", "NE": "!=", "GT": ">", "GE": ">=", "LT": "<", "LE": "<="}
NUMERIC = {"int", "integer", "long", "decimal", "double", "float"}


class BusinessPlanError(ValueError):
    """A stable repair cause, independent of source values or wording changes."""

    def __init__(self, code, message, *, path="", **guidance):
        super().__init__(message)
        self.reason_code, self.path, self.guidance = code, path, guidance


def _unique(items, field):
    result = {item[field]: item for item in items}
    if len(result) != len(items):
        raise BusinessPlanError("DUPLICATE_BUSINESS_BINDING", f"{field} 必须唯一，不能覆盖已有业务绑定")
    return result


def _comparison(condition, fields, parameters):
    field = condition["field"]
    if field not in fields:
        raise ValueError(f"条件引用未声明数据属性：{field}")
    datatype = fields[field]["datatype"].rsplit("#", 1)[-1].removeprefix("xsd:")
    if datatype not in NUMERIC | {"string", "boolean", "date", "dateTime"}:
        raise ValueError(f"比较暂不支持数据类型：{datatype}")
    if condition["op"] not in {"EQ", "NE"} and datatype in {"string", "boolean"}:
        raise ValueError("字符串和布尔值只支持相等/不等比较")
    if "parameter" in condition:
        name = condition["parameter"]
        spec = parameters.get(name, {})
        accepted = ({"decimal", "integer"} if datatype in NUMERIC else
                    {"datetime"} if datatype == "dateTime" else {datatype})
        if spec.get("type") not in accepted or not spec.get("required"):
            raise ValueError(f"参数 {name} 必须必填且与属性类型 {datatype} 相容")
        right = "{{" + name + "}}"
    else:
        value = condition["value"]
        if datatype in NUMERIC:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise ValueError("数值条件必须使用有限数值")
            if datatype in {"int", "integer", "long"} and not isinstance(value, int):
                raise ValueError("整数属性的比较值必须是整数")
            if datatype == "decimal":
                value = Decimal(str(value))
        elif datatype == "boolean":
            if not isinstance(value, bool):
                raise ValueError("布尔属性条件必须使用布尔值")
        elif not isinstance(value, str):
            raise ValueError("字符串/日期条件必须使用字符串值")
        term = Literal(value, datatype=URIRef(str(XSD) + datatype))
        if getattr(term, "ill_typed", False):
            raise ValueError("条件值不是有效的已声明数据类型")
        right = term.n3()
    return f"?{field} {OPS[condition['op']]} {right}"


def _compile_plan(plan, mappings, compiled_ids, rules, known_cqs, namespace, ontology_candidates, mapping_sources):
    by_id = _unique(mappings, "id")
    refs = resolve_business_mapping_scope(mappings, plan)
    required_sources = {ref for ref in refs if by_id[ref].get("mapping_type") != "RULE_TO_CLASS"}
    if any(ref not in compiled_ids or not mapping_sources.get(ref) for ref in required_sources):
        raise BusinessPlanError("BUSINESS_MAPPING_DEPENDENCY_INCOMPLETE",
                                "业务能力的执行依赖尚未完整编译，不能从手写来源引用推测数据源。先处理对应映射的编译诊断。")

    def mapping(ref, types, *, compiled=True):
        item = by_id.get(ref)
        if item is None or item.get("mapping_type") not in types:
            raise BusinessPlanError("BUSINESS_MAPPING_REFERENCE_INVALID", f"映射引用不存在或类型不符：{ref}")
        if compiled and ref not in compiled_ids:
            raise BusinessPlanError("BUSINESS_MAPPING_DEPENDENCY_INCOMPLETE", f"来源映射尚未成功编译：{ref}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", item["target"]):
            raise ValueError("本体目标名不受支持")
        return item

    entities = {alias: mapping(item["mapping_ref"], {"TABLE_TO_CLASS", "SQL_TO_CLASS"})
                for alias, item in _unique(plan["entities"], "as").items()}
    if any(alias.startswith("orion_condition_") for alias in entities):
        raise ValueError("对象名称不能占用平台保留字段 orion_condition_")
    patterns = [f"?{alias} a <{namespace}{item['target']}> ." for alias, item in entities.items()]
    connected = {alias: set() for alias in entities}
    for link in plan.get("relations", []):
        prop = mapping(link["mapping_ref"], {"CANDIDATE_JOIN_TO_OBJECT_PROPERTY"})
        left, right = link["from"], link["to"]
        if (left not in entities or right not in entities or
                prop.get("domain") != entities[left]["target"] or prop.get("range") != entities[right]["target"]):
            raise BusinessPlanError("BUSINESS_RELATION_ENDPOINT_INVALID", "关系方向/端点与已声明本体 domain/range 不符", path="/relations")
        connected[left].add(right)
        connected[right].add(left)
        patterns.append(f"?{left} <{namespace}{prop['target']}> ?{right} .")
    seen, pending = set(), [next(iter(entities))]
    while pending:
        alias = pending.pop()
        if alias not in seen:
            seen.add(alias)
            pending.extend(connected[alias] - seen)
    if seen != set(entities):
        raise BusinessPlanError("BUSINESS_ENTITIES_DISCONNECTED", "业务对象之间必须有显式关系，禁止无意笛卡尔积", path="/relations")
    fields, field_patterns = {}, {}
    for alias, item in _unique(plan.get("fields", []), "as").items():
        prop = mapping(item["mapping_ref"], {"COLUMN_TO_DATA_PROPERTY"})
        entity = item["entity"]
        if alias in entities or alias.startswith("orion_condition_"):
            raise ValueError("结果字段与实体/平台保留字段重名")
        if entity not in entities or prop.get("domain", prop.get("class")) != entities[entity]["target"]:
            raise BusinessPlanError("BUSINESS_PROPERTY_DOMAIN_INVALID", "数据属性不属于指定业务对象", path="/fields")
        if not prop.get("datatype"):
            raise BusinessPlanError("BUSINESS_PROPERTY_DATATYPE_MISSING", "数据属性缺少 datatype，不能猜测条件类型", path="/fields")
        fields[alias] = prop
        pattern = f"?{entity} <{namespace}{prop['target']}> ?{alias} ."
        field_patterns[alias] = {"optional": bool(item.get("optional")), "patterns": [pattern]}
    selected = list(plan["select"])
    if not set(selected) <= set(entities) | set(fields):
        raise BusinessPlanError("BUSINESS_PROJECTION_INVALID", "select 只能引用已声明业务对象或数据属性", path="/select")
    parameters = copy.deepcopy(plan.get("parameters", {}))
    filters = plan.get("filters", [])
    reasons, rule_id = {}, None
    if "rule" in plan:
        if filters:
            raise ValueError("规则证据不能先按结果条件过滤；请将判断声明在 rule.premises 中，保留正例、反例和未知")
        if any(not item.get("optional") for item in plan.get("fields", [])):
            raise ValueError("规则证据属性必须 optional，缺失数据应保留为未知")
        reasons, rule_id = _compile_rule(plan, rules, mapping, entities, fields, parameters, field_patterns, selected, namespace,
                                        ontology_candidates)
    for field in field_patterns.values():
        pattern = " ".join(field["patterns"])
        patterns.append("OPTIONAL { " + pattern + " }" if field["optional"] else pattern)
    for condition in filters:
        patterns.append("FILTER(" + _comparison(condition, fields, parameters) + ")")
    # The plan declares no business ranking. Order by projected object identities
    # only: sorting numeric RDF terms or internal nullable condition flags under
    # DISTINCT is unsupported by Ontop/PostgreSQL. Do not invent value ranking
    # or add hidden output columns just to choose an arbitrary presentation order.
    ordering = ["?" + field for field in selected if field in entities]
    if not ordering and any(case.get("expected_first_row") for case in plan["validation_cases"]):
        raise ValueError("纯标量投影没有首行顺序；请保留一个业务对象标识，或使用 CQ 的 ANY/ALL 边界断言核验成员而非首行")
    query = ("SELECT DISTINCT " + " ".join("?" + field for field in selected) + " WHERE {\n  " +
             "\n  ".join(patterns) + "\n}" + (" ORDER BY " + " ".join(ordering) if ordering else ""))
    cq_bindings = copy.deepcopy(plan.get("cq_bindings", {}))
    if any(binding.get("answer_mode") != "FACT_QUERY" for binding in cq_bindings.values()):
        raise ValueError("业务事实查询只绑定 FACT_QUERY；规则 CQ 应放在 rule.cq_bindings")
    cq_bindings = compile_business_answer_bindings(plan, mappings, namespace, bindings=cq_bindings)
    cap = {
        "description_zh": plan["description_zh"], "parameters": parameters, "result_fields": selected,
        "question_examples": plan["question_examples"], "validation_cases": plan["validation_cases"],
        "query_mode": "SNAPSHOT_ONLY", "cq_bindings": cq_bindings, "business_question_ids": sorted(cq_bindings),
        **source_capability(dependency for ref in sorted(required_sources) for dependency in mapping_sources[ref]),
    }
    name = plan["id"]
    try:
        normalized = normalize_query_capabilities({name: cap}, {name}, allow_legacy=False)[name]
        validate_query_template_contract(query, normalized)
    except ValueError as exc:
        raise BusinessPlanError("BUSINESS_QUERY_RUNTIME_CONTRACT_INVALID", str(exc),
                                expected={"result_fields": selected}) from exc
    evidence_refs = business_evidence_refs(
        [by_id[ref] for ref in refs], [rule for rule in rules if rule.get("id") == rule_id])
    for index, case in enumerate(normalized["validation_cases"]):
        if any(not set(item["source_refs"]) <= evidence_refs
               for item in (case.get("nullable_bindings") or {}).values()):
            raise BusinessPlanError("BUSINESS_CASE_EVIDENCE_UNBOUND",
                                    "用例的条件空值必须引用本能力实际依赖的已审来源证据",
                                    path=f"/validation_cases/{index}/nullable_bindings")
    for qid, binding in cq_bindings.items():
        _validate_cq_refs(qid, binding, known_cqs, evidence_refs)
        compile_reviewed_cq(question_id=qid, query_name=name, query=query, capability=normalized)
    for reason in reasons.values():
        if reason.get("cq_bindings"):
            reason["cq_bindings"] = compile_business_answer_bindings(
                plan, mappings, namespace, bindings=reason["cq_bindings"], inference=True)
        for qid, binding in reason.get("cq_bindings", {}).items():
            _validate_cq_refs(qid, binding, known_cqs, evidence_refs)
    try:
        normalize_reasoning_capabilities(reasons, {name}, require_rules=True)
    except ValueError as exc:
        raise BusinessPlanError("BUSINESS_RULE_RUNTIME_CONTRACT_INVALID", str(exc), path="/rule") from exc
    return query, cap, reasons, rule_id


def _validate_cq_refs(qid, binding, known_cqs, refs):
    if qid not in known_cqs:
        raise BusinessPlanError("BUSINESS_CQ_REFERENCE_UNKNOWN", f"CQ 必须引用本工程已登记业务问题：{qid}")
    citations = set(binding.get("source_refs", []))
    for nullable in (binding.get("nullable_bindings") or {}).values():
        citations.update(nullable.get("source_refs", []))
    if not citations <= refs:
        raise BusinessPlanError("BUSINESS_CQ_EVIDENCE_UNBOUND",
                                f"CQ {qid} 的 source_refs 必须来自本能力实际依赖的映射、规则或其已审来源证据")


def _compile_rule(plan, rules, mapping, entities, fields, parameters, field_patterns, selected, namespace, ontology_candidates):
    declaration = plan["rule"]
    rule_id = declaration["rule_id"]
    candidates = [rule for rule in rules if rule.get("id") == rule_id]
    if len(candidates) != 1:
        raise ValueError("规则必须引用唯一的 S2 业务规则")
    rule = candidates[0]
    conditions, rule_parameters = declared_conditions(rule, ontology_candidates, parameters)
    parameters.clear()
    parameters.update(rule_parameters)
    expression = rule.get("formal_expression", "")
    validate_rule_expression(expression)
    match = re.fullmatch(r"IF\s+(.+?)\s+THEN\s+(.+)", expression, re.I)
    atoms = [parse_atom(part.strip()) for part in re.split(r"\s+AND\s+", match[1], flags=re.I)]
    conclusion, arguments = parse_atom(match[2])
    if (len(arguments) != 1 or not arguments[0].startswith("?") or
            any(args != arguments for _, args in atoms) or
            conclusion in {predicate for predicate, _ in atoms}):
        raise ValueError("首版规则编译只支持同一对象上的正向一元规则；复杂规则需独立评审")
    subject = declaration["subject"]
    if subject not in entities or subject not in selected:
        raise BusinessPlanError("BUSINESS_RULE_SUBJECT_NOT_PROJECTED", "规则主体必须是已投影业务对象", path="/rule/subject",
                                expected={"entity_aliases": list(entities), "projection_required": True})
    bindings, terms = [], {}
    declared = set()
    for index, premise in enumerate(declaration["premises"]):
        item = mapping(premise["mapping_ref"], {"RULE_TO_CLASS"}, compiled=False)
        derivation = item.get("derivation", {})
        predicate = derivation.get("premise_predicate")
        if derivation.get("rule_id") != rule_id or predicate not in {p for p, _ in atoms} or predicate in declared:
            raise ValueError("前提类必须唯一引用此 S2 规则的前提谓词")
        declared.add(predicate)
        flag = f"orion_condition_{index}"
        condition = bind_declared_condition(conditions[predicate], premise, fields, ontology_candidates, index=index)
        # A rule receipt must retain its actual comparison operands. Besides
        # making the condition traceable, this avoids the verified wrong BIND
        # result for unprojected OPTIONAL operands in the current Ontop backend.
        # Missing operands stay unbound; no false/zero/default is introduced.
        if condition["field"] not in selected:
            selected.append(condition["field"])
        # Evaluate where the source property is mandatory. If the OPTIONAL has
        # no property, both its value and condition remain unbound outside it;
        # do not turn missing evidence into an explicit false premise.
        field_patterns[condition["field"]]["patterns"].append(
            f"BIND(({_comparison(condition, fields, parameters)}) AS ?{flag})")
        selected.append(flag)
        bindings.append({"predicate": predicate, "arguments": [{"field": subject}],
                         "when": {"field": flag, "equals": True}})
        terms[predicate] = namespace + item["target"]
    if declared != {p for p, _ in atoms} or set(rule.get("premise_predicates", [])) != declared:
        raise ValueError("必须完整绑定 S2 规则的每个前提，不能遗漏或更改业务表达式")
    if conclusion != rule.get("conclusion_predicate"):
        raise ValueError("S2 规则结论声明与正式表达式不一致")
    return _finish_rule(plan, rule, rule_id, conclusion, mapping, terms, bindings, namespace, parameters), rule_id


def _finish_rule(plan, rule, rule_id, conclusion, mapping, terms, bindings, namespace, parameters):
    # The conclusion mapping is explicit in the authoring contract.
    item = mapping(plan["rule"]["conclusion_mapping_ref"], {"RULE_TO_CLASS"}, compiled=False)
    derivation = item.get("derivation", {})
    if derivation.get("rule_id") != rule_id or derivation.get("premise_predicate"):
        raise ValueError("结论类必须引用此 S2 规则且不能同时声明前提")
    terms[conclusion] = namespace + item["target"]
    name = plan["id"] + "_rule"
    cap = {
        "description_zh": rule.get("description") or rule.get("name") or plan["description_zh"],
        "evidence_query": plan["id"], "execution_scope": "FULL_QUERY_RESULT",
        "fact_bindings": bindings, "result_predicates": [conclusion],
        "question_examples": plan["question_examples"], "source_rule_ids": [rule_id], "ontology_terms": terms,
        "runtime_validation": copy.deepcopy(plan["rule"]["runtime_validation"]),
        "rules": [{"rule_id": rule_id, "description_zh": rule.get("description") or rule.get("name"),
                   "expression": rule["formal_expression"], "confidence": rule.get("confidence", 1)}],
    }
    cq_bindings = copy.deepcopy(plan["rule"].get("cq_bindings", {}))
    if cq_bindings:
        subject = plan["rule"]["subject"]
        for index, case in enumerate(plan["rule"].get("validation_cases", [])):
            try:
                normalize_expected_rows(case, fields=[subject])
            except ValueError as exc:
                raise BusinessPlanError(
                    "BUSINESS_RULE_EXPECTED_ROWS_INVALID",
                    f"规则结论验收只返回主体字段 {subject}（实例IRI）；当前用例不符合该结果合同：{exc}。"
                    "证据查询与推理结论分别验收；依据独立业务预期及已确定身份映射声明结论，不从实际输出倒填、删除或降低期望。",
                    path=f"/rule/validation_cases/{index}", expected={"result_fields": [subject], "value_kind": "ENTITY_IRI"},
                ) from exc
        query = f"SELECT DISTINCT ?{subject} WHERE {{ ?{subject} a <{terms[conclusion]}> . }} ORDER BY ?{subject}"
        for binding in cq_bindings.values():
            if binding.get("answer_mode") != "RULE_INFERENCE":
                raise ValueError("规则 CQ 的 answer_mode 必须为 RULE_INFERENCE")
            binding.update(reasoning_capability=name, derived_predicates=[conclusion], cq_sparql=query)
        cap.update(cq_bindings=cq_bindings, business_question_ids=sorted(cq_bindings), parameters=parameters,
                   result_fields=[subject], validation_cases=copy.deepcopy(plan["rule"].get("validation_cases", [])))
    return {name: cap}


def _plan_structure_issues(plan, index):
    """Retain validator locations without echoing input values in diagnostics."""
    issues = []
    seen = set()
    for error in Draft202012Validator(BUSINESS_QUERY_PLAN_SCHEMA).iter_errors(plan):
        tokens = list(error.absolute_path)
        detail = f"业务查询计划结构不正确（{error.validator}）；按业务计划正式合同修正。"
        guidance = {}
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance]
            guidance["missing_fields"] = missing
            if len(missing) == 1:
                tokens.append(missing[0])
        elif error.validator == "additionalProperties":
            guidance["allowed_fields"] = list(error.schema.get("properties", {}))
        elif error.validator in {"type", "enum", "const", "maxItems", "minItems", "pattern"}:
            guidance["expected"] = error.validator_value
        if isinstance(plan, dict) and "rule" in plan and len(tokens) == 3 and tokens[0] == "fields" and tokens[2] == "optional":
            detail = "规则证据属性必须在 fields 每项声明 optional=true，缺失数据应保留为未知；不修改业务判断或验收期望。"
        path = f"/mapping_draft/business_query_plans/{index}" + "".join(
            "/" + str(token).replace("~", "~0").replace("/", "~1") for token in tokens)
        identity = (path, error.validator)
        if identity not in seen:
            issues.append({"kind": "BUSINESS_PLAN_REVIEW_REQUIRED", "path": path,
                           "reason_code": f"SCHEMA_{error.validator}", "detail": detail, **guidance})
            seen.add(identity)
    return issues


def compile_business_query_plans(mapping_draft, *, compiled_ids, rules, cq_questions, ontology_candidates=(),
                                 mapping_source_dependencies=None):
    plans = mapping_draft.get("business_query_plans", [])
    if not isinstance(plans, list) or len(plans) > 32:
        return {}, {}, {}, set(), [{"kind": "BUSINESS_PLAN_REVIEW_REQUIRED",
                                   "path": "/mapping_draft/business_query_plans",
                                   "reason_code": "BUSINESS_PLAN_COLLECTION_INVALID",
                                   "detail": "business_query_plans 必须为最多32项的数组"}]
    queries, capabilities, reasons, bound_rules, issues = {}, {}, {}, set(), []
    ids = [p.get("id") for p in plans if isinstance(p, dict)]
    planned_rules = {p.get("rule", {}).get("rule_id") for p in plans
                     if isinstance(p, dict) and isinstance(p.get("rule", {}), dict)}
    for rule in rules:
        if "condition_contract" in rule and rule.get("id") not in planned_rules:
            issues.append({"kind": "BUSINESS_PLAN_REVIEW_REQUIRED", "path": "/mapping_draft/business_query_plans",
                           "reason_code": "DECLARED_RULE_PLAN_REQUIRED", "rule_id": rule.get("id"),
                           "detail": "S2已声明业务条件的规则必须有对应业务计划，不能删除规则计划后以手写运行内容绕过语义校验。"})
    bound_cqs = set()
    for index, plan in enumerate(plans):
        try:
            structural = _plan_structure_issues(plan, index)
            if structural:
                issues.extend(structural)
                continue
            query, cap, generated, rule_id = _compile_plan(
                plan, mapping_draft["mappings"], set(compiled_ids), rules,
                {q["id"] for q in cq_questions}, mapping_draft["namespace"], ontology_candidates,
                mapping_source_dependencies or {})
            name = plan["id"]
            fact_cqs = set(cap.get("cq_bindings", {}))
            rule_cqs = {qid for reason in generated.values() for qid in reason.get("cq_bindings", {})}
            if fact_cqs & rule_cqs:
                raise ValueError("同一 CQ 不能同时归属事实查询和规则查询")
            cqs = fact_cqs | rule_cqs
            if ids.count(name) != 1 or rule_id in bound_rules or cqs & bound_cqs:
                raise ValueError("计划 ID、规则执行归属和 CQ 绑定必须唯一")
            queries[name], capabilities[name] = query, cap
            reasons.update(generated)
            if rule_id:
                bound_rules.add(rule_id)
            bound_cqs.update(cqs)
        except RuleConditionError as exc:
            item = {"kind": "BUSINESS_PLAN_REVIEW_REQUIRED",
                    "path": f"/mapping_draft/business_query_plans/{index}" + exc.path,
                    "reason_code": exc.reason_code, "detail": str(exc)}
            if exc.upstream:
                item["upstream_source"] = {"stage": "S2", "artifact": "02-semantic-recognition/business-rule-candidates.json",
                                           "rule_id": plan.get("rule", {}).get("rule_id"),
                                           "changed_components": ["SEMANTIC_MODEL"]}
            issues.append(item)
        except BusinessPlanError as exc:
            issues.append({"kind": "BUSINESS_PLAN_REVIEW_REQUIRED",
                           "path": f"/mapping_draft/business_query_plans/{index}" + exc.path,
                           "reason_code": exc.reason_code, "detail": str(exc), **exc.guidance})
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            issues.append({"kind": "BUSINESS_PLAN_REVIEW_REQUIRED", "path": f"/mapping_draft/business_query_plans/{index}",
                           "reason_code": "BUSINESS_PLAN_COMPILATION_FAILED", "detail": str(exc)})
    return queries, capabilities, reasons, bound_rules, issues
