"""Shared authoring schema for business-owned S2 comparison conditions."""
from __future__ import annotations

from .business_query_plan import TEXT, VARIABLE, _object

_PREDICATE = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_:-]{0,127}$"}
_PREMISE = _object({
    "predicate": _PREDICATE,
    "property_candidate_id": TEXT,
    "op": {"enum": ["EQ", "NE", "GT", "GE", "LT", "LE"]},
    "value": {"type": ["string", "number", "boolean"], "maxLength": 512},
    "parameter": VARIABLE,
}, ["predicate", "property_candidate_id", "op"])
_PREMISE["oneOf"] = [{"required": ["value"]}, {"required": ["parameter"]}]
RULE_CONDITION_SCHEMA = _object({
    "version": {"type": "integer", "const": 1},
    "premises": {"type": "array", "minItems": 1, "maxItems": 8, "items": _PREMISE},
    "parameters": {"type": "object", "maxProperties": 8,
                   "propertyNames": VARIABLE,
                   "additionalProperties": _object({
                       "type": {"enum": ["integer", "decimal", "string", "boolean", "date", "datetime"]},
                       "description_zh": TEXT, "required": {"const": True},
                       "default": {"type": ["number", "string", "boolean"]},
                       "minimum": {"type": "number"}, "maximum": {"type": "number"},
                       "max_length": {"type": "integer", "minimum": 1},
                   }, ["type", "description_zh", "required"])},
}, ["version", "premises"])
RULE_CONDITION_SCHEMA["description"] = (
    "自动编译比较规则时的唯一业务条件声明。premises精确覆盖本规则的前提谓词；"
    "property_candidate_id引用同一S2的DATA_PROPERTY候选。固定阈值用value，"
    "只有来源明确允许调用者改变的条件才用parameter并在parameters声明范围。"
    "S3只绑定字段，不重新定义阈值或算子。缺少此声明的历史规则仍可回读，但不能猜测自动编译。"
)

