"""Validate S2 business conditions and bind S3 fields without changing semantics."""
from __future__ import annotations

import copy
import json
import math

from jsonschema import Draft202012Validator

from services.ontology_contracts.rule_conditions import RULE_CONDITION_SCHEMA


class RuleConditionError(ValueError):
    def __init__(self, reason: str, message: str, *, path: str = "", upstream: bool = False):
        super().__init__(message)
        self.reason_code = reason
        self.path = path
        self.upstream = upstream


def _same(left, right):
    # JSON values keep bool and number distinct; Python's True == 1 must not
    # let a model silently change the meaning of a reviewed comparison.
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def rule_condition_issues(rule: dict, candidates: list[dict] | None = None) -> list[dict]:
    """Validate an explicitly declared contract without inventing one for legacy rules."""
    if "condition_contract" not in rule:
        return []
    contract = rule["condition_contract"]
    errors = list(Draft202012Validator(RULE_CONDITION_SCHEMA).iter_errors(contract))
    if errors:
        return [{"path": "condition_contract" + "".join(f"/{p}" for p in error.absolute_path),
                 "reason_code": "RULE_CONDITION_SCHEMA", "message": "规则条件声明不符合正式合同。"}
                for error in errors]
    issues = []

    def add(path, message):
        issues.append({"path": "condition_contract/" + path,
                       "reason_code": "RULE_CONDITION_SEMANTICS", "message": message})

    premises = contract["premises"]
    names = [p["predicate"] for p in premises]
    if len(set(names)) != len(names) or set(names) != set(rule.get("premise_predicates", [])):
        add("premises", "条件必须唯一且完整覆盖本规则的前提谓词。")
    parameters = contract.get("parameters", {})
    used = {p["parameter"] for p in premises if "parameter" in p}
    if used != set(parameters):
        add("parameters", "只声明业务前提实际使用的可变参数；固定条件不得添加调用参数。")
    for name, spec in parameters.items():
        path = f"parameters/{name}"
        numeric = spec["type"] in {"integer", "decimal"}
        bounds = [spec[k] for k in ("minimum", "maximum") if k in spec]
        if bounds and (not numeric or any(not math.isfinite(v) for v in bounds)):
            add(path, "数值范围只适用于数值参数，且上下界必须有限。")
        if ("minimum" in spec and "maximum" in spec and spec["minimum"] > spec["maximum"]):
            add(path, "参数最小值不能大于最大值。")
        if "default" in spec:
            value = spec["default"]
            valid = (isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
                     if numeric else isinstance(value, bool) if spec["type"] == "boolean" else isinstance(value, str))
            if spec["type"] == "integer" and not isinstance(value, int):
                valid = False
            if not valid:
                add(path + "/default", "默认值必须符合声明的参数类型。")
            elif numeric and (("minimum" in spec and value < spec["minimum"])
                              or ("maximum" in spec and value > spec["maximum"])):
                add(path + "/default", "默认值必须位于已声明业务范围内。")
            elif isinstance(value, str) and "max_length" in spec and len(value) > spec["max_length"]:
                add(path + "/default", "默认文本超过已声明长度。")
    for index, premise in enumerate(premises):
        value = premise.get("value")
        if isinstance(value, float) and not math.isfinite(value):
            add(f"premises/{index}/value", "业务常量必须为有限数值。")
        if candidates is not None:
            matches = [c for c in candidates if isinstance(c, dict) and c.get("id") == premise["property_candidate_id"]]
            if len(matches) != 1 or matches[0].get("kind") != "DATA_PROPERTY":
                add(f"premises/{index}/property_candidate_id", "条件属性必须唯一引用本工程S2的DATA_PROPERTY候选。")
            elif not all((matches[0].get("source_binding") or {}).get(k) for k in ("table", "column", "declared_sql_type")):
                add(f"premises/{index}/property_candidate_id", "自动比较规则的属性须有明确来源表、列和数据类型；缺少时应先完成S2来源绑定。")
    return issues


def declared_conditions(rule: dict, candidates: list[dict], supplied_parameters: dict) -> tuple[dict, dict]:
    if "condition_contract" not in rule:
        raise RuleConditionError("RULE_CONDITIONS_UNDECLARED",
                                 "S2规则缺少condition_contract；不能从谓词名称、描述或S3草稿猜测业务阈值。"
                                 "须按正式修订流程补齐S2条件定义，不能在S3反复改写条件。", upstream=True)
    problems = rule_condition_issues(rule, candidates)
    if problems:
        raise RuleConditionError("RULE_CONDITIONS_INVALID", problems[0]["message"], upstream=True)
    contract = rule["condition_contract"]
    parameters = copy.deepcopy(contract.get("parameters", {}))
    if supplied_parameters and not _same(supplied_parameters, parameters):
        raise RuleConditionError("RULE_PARAMETERS_CHANGED",
                                 "规则参数与S2业务声明不一致；固定常量不能改为参数，默认值相同也不代表语义相同。"
                                 "移除重复参数声明或沿用S2合同；真正变更业务规则须正式修订S2。", path="/parameters")
    return {p["predicate"]: p for p in contract["premises"]}, parameters


def bind_declared_condition(definition: dict, premise: dict, fields: dict, candidates: list[dict], *, index: int) -> dict:
    path = f"/rule/premises/{index}"
    field = premise.get("field") or premise.get("when", {}).get("field")
    prop = fields.get(field)
    candidate = next(c for c in candidates if c.get("id") == definition["property_candidate_id"])
    source = candidate.get("source_binding") or {}
    derivation = (prop or {}).get("derivation") or {}
    if (not prop or not source.get("table") or not source.get("column")
            or derivation.get("from_candidate") != candidate["id"]
            or (source.get("source_id") and derivation.get("source_id") != source["source_id"])
            or derivation.get("from_snapshot_table") != source["table"]
            or derivation.get("from_snapshot_column") != source["column"]):
        raise RuleConditionError("RULE_PROPERTY_BINDING_CHANGED",
                                 "规则字段必须绑定S2声明的业务属性及其真实来源身份、表列，不能只改字段别名或候选ID。", path=path)
    condition = {"field": field, "op": definition["op"],
                 **{k: copy.deepcopy(definition[k]) for k in ("value", "parameter") if k in definition}}
    if "when" in premise and not _same(premise["when"], condition):
        raise RuleConditionError("RULE_CONDITION_CHANGED",
                                 "S3条件与S2业务定义不一致。固定常量、比较算子和可变参数必须沿用已声明定义；"
                                 "可仅提交field绑定，由平台生成条件。", path=path + "/when")
    return condition
