"""Model-facing AST for read-only S1 evidence; it never accepts SQL fragments."""
from __future__ import annotations


def obj(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


TEXT = {"type": "string", "minLength": 1}
FIELD = {**TEXT, "description": "sources 别名.真实业务列，如 inspection.inspected_at；不能填 SQL 表达式"}
NAME = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,47}$"}
CAST = {"type": "string", "enum": ["text", "decimal", "integer", "datetime", "date"]}
NORMALIZE = {"type": "string", "enum": ["upper", "lower"]}
EXPRESSION = obj({"field": FIELD, "cast": CAST, "normalize": NORMALIZE,
                  "aggregate": {"type": "string", "enum": ["count", "count_distinct", "sum", "avg", "min", "max"]}})
OP = {"type": "string", "enum": ["eq", "ne", "lt", "lte", "gt", "gte", "is_null", "is_not_null"]}
SCALAR = {"type": ["string", "number", "boolean"]}
SOURCE_QUERY_PLAN_SCHEMA = obj({
    "sources": {"type": "object", "minProperties": 1, "maxProperties": 4,
                "propertyNames": NAME, "additionalProperties": {**TEXT, "description": "当前 S1 精确来源表名/物理快照名；同名表用 source_id.table"}},
    "from": {**NAME, "description": "起始来源别名"},
    "joins": {"type": "array", "maxItems": 3, "items": obj({"left": FIELD, "right": FIELD}, ["left", "right"]),
              "description": "按顺序将新来源用等值关系连接到已连接来源；全部来源须连通，不允许笛卡尔积"},
    "select": {"type": "object", "minProperties": 1, "maxProperties": 12, "propertyNames": NAME,
               "additionalProperties": EXPRESSION,
               "description": "输出名到表达式；count 无 field 表示 COUNT(*)，其他聚合须有 field；文本数值先 cast=decimal"},
    "filters": {"type": "array", "maxItems": 12, "items": obj({"field": FIELD, "cast": CAST, "normalize": NORMALIZE,
                 "op": OP, "value": SCALAR, "value_field": FIELD}, ["field", "op"]),
                "description": "所有条件按 AND 连接；value 与 value_field 二选一，空值检查不填值。datetime 比较值用带时区 ISO 8601"},
    "group_by": {"type": "array", "maxItems": 6, "uniqueItems": True, "items": NAME,
                 "description": "恰好列出 select 所有非聚合输出名；纯聚合用 []"},
    "having": {"type": "array", "maxItems": 12, "items": obj({"field": NAME, "op": OP, "value": SCALAR}, ["field", "op"]),
               "description": "对 select 输出分组结果进行比较，如 count_distinct 的结果 >= 2"},
    "order_by": {"type": "array", "maxItems": 12, "items": obj({"field": NAME, "direction": {"type": "string", "enum": ["ASC", "DESC"]}}, ["field"]),
                 "description": "按输出名排序，省略时按全部输出名升序"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
}, ["sources", "from", "select"])
