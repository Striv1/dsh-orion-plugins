"""Bounded, mapping-referenced authoring surface for generated S3 capabilities."""
from __future__ import annotations


def _object(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


TEXT = {"type": "string", "minLength": 1, "maxLength": 512}
NAME = {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,40}$"}
VARIABLE = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"}
CONDITION = _object({
    "field": VARIABLE, "op": {"enum": ["EQ", "NE", "GT", "GE", "LT", "LE"]},
    "value": {"type": ["string", "number", "boolean"], "maxLength": 512},
    "parameter": VARIABLE,
}, ["field", "op"])
CONDITION["oneOf"] = [{"required": ["value"]}, {"required": ["parameter"]}]

# Detailed case, parameter and CQ contracts are the existing runtime contracts;
# the harness projects those into this shape for authoring. The compiler also
# invokes their native normalizers before installing any generated capability.
BUSINESS_QUERY_PLAN_SCHEMA = _object({
    "id": NAME, "description_zh": TEXT,
    "entities": {"type": "array", "minItems": 1, "maxItems": 4, "items": _object({
        "as": VARIABLE, "mapping_ref": TEXT,
    }, ["as", "mapping_ref"])},
    "relations": {"type": "array", "maxItems": 3, "items": _object({
        "mapping_ref": TEXT, "from": VARIABLE, "to": VARIABLE,
    }, ["mapping_ref", "from", "to"])},
    "fields": {"type": "array", "maxItems": 16, "items": _object({
        "as": VARIABLE, "mapping_ref": TEXT, "entity": VARIABLE,
        "optional": {"type": "boolean", "description":
                     "位于 fields 每项内。省略或 false 表示必须有该属性；计划含 rule 时每项必须显式为 true，"
                     "以 OPTIONAL 保留属性缺失的业务对象为未知。不能用 nullable/presence/required 替代，"
                     "也不能放在 rule.premises 或 mappings 中。"},
    }, ["as", "mapping_ref", "entity"])},
    "select": {"type": "array", "items": VARIABLE, "minItems": 1, "uniqueItems": True,
               "description": "业务输出字段。含rule时平台还会投影每个规则条件引用的来源属性及条件布尔值，保留实际推理证据；规则结论仍单独输出主体。"},
    "filters": {"type": "array", "maxItems": 8, "items": CONDITION},
    "parameters": {"type": "object"},
    "question_examples": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 20},
    "validation_cases": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 20,
                         "description": "验证本计划的事实查询输出。含rule时这里验证未按规则结果过滤的完整证据，包含反例与未知对象；规则结论另在rule.validation_cases验证。"},
    "cq_bindings": {"type": "object"},
    "rule": _object({
        "rule_id": TEXT,
        "subject": {**VARIABLE, "description": "entities中的对象别名，必须也在计划select中投影；不是该对象的数据属性或业务编号字段。"},
        "conclusion_mapping_ref": TEXT,
        "premises": {"type": "array", "minItems": 1, "maxItems": 8, "items": _object({
            "mapping_ref": TEXT,
            "field": {**VARIABLE, "description": "绑定S2 condition_contract声明的业务属性；平台从S2生成比较条件，不重复填写阈值。"},
            "when": {**CONDITION, "description": "兼容显式条件，但必须与S2 condition_contract完全一致。优先只绑定field。"},
        }, ["mapping_ref"])},
        "runtime_validation": {"type": "object"},
        "validation_cases": {"type": "array", "items": {"type": "object"}, "minItems": 1,
                             "description": "规则结论用例。输出仅rule.subject字段，其值为已有对象的实例IRI，不继承事实查询的标量字段。预期须来自独立业务答案和已确定身份映射，不能倒填实际结果或删减期望。"},
        "cq_bindings": {"type": "object"},
    }, ["rule_id", "subject", "conclusion_mapping_ref", "premises", "runtime_validation"]),
}, ["id", "description_zh", "entities", "select", "question_examples", "validation_cases"])
BUSINESS_QUERY_PLAN_SCHEMA["properties"]["rule"]["properties"]["premises"]["items"]["oneOf"] = [
    {"required": ["field"]}, {"required": ["when"]},
]

# Project the compiler's existing rule-evidence constraints into the same
# authoring schema. This describes required input; it never fills business data.
BUSINESS_QUERY_PLAN_SCHEMA["allOf"] = [{
    "if": {"required": ["rule"]},
    "then": {"properties": {
        "fields": {"items": {"required": ["optional"], "properties": {
            "optional": {"const": True, "description": "规则证据属性必须 optional=true，缺失数据应保留为未知。"},
        }}},
        "filters": {"maxItems": 0, "description":
                    "规则证据不能先按结果条件过滤；将判断声明在 rule.premises，保留正例、反例和未知。"},
    }},
}]
