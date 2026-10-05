"""Model-facing S3 mapping fields, projected from existing workflow gates.

This is authoring guidance, not a replacement for formal semantic validation.
Keep legacy label aliases and version-gated instance contracts compatible.
"""
from __future__ import annotations

from copy import deepcopy

from services.ontology_contracts.business_query_plan import BUSINESS_QUERY_PLAN_SCHEMA
from services.ontology_engineering import business_modeling_contract as business
from services.ontology_engineering.stage_submission_validation import (
    DOCUMENT_MAPPING_TYPES,
    RULE_CLASS_MAPPING_TYPES,
    SUPPORTED_MAPPING_TYPES,
)


def mapping_draft_schema(intake_mode: str | None, *, business_modeling_contract_version: str | None = None) -> dict:
    mode = str(intake_mode or "HYBRID").upper()
    types = (DOCUMENT_MAPPING_TYPES | RULE_CLASS_MAPPING_TYPES if mode == "DOCUMENT_ONLY" else
             SUPPORTED_MAPPING_TYPES - DOCUMENT_MAPPING_TYPES if mode == "DATABASE_ONLY" else SUPPORTED_MAPPING_TYPES)
    text = {"type": "string", "minLength": 1}
    refs = {"type": "array", "items": text, "minItems": 1}
    versioned = business_modeling_contract_version == business.VERSION
    instance = {
        "type": "object", "additionalProperties": True,
        "description": "business-first-v1 类映射必须填写；沿用 S2 已确认实例含义，并补齐实际映射引用，不能为通过校验改业务定义。",
        "properties": {
            "business_role": {"type": "string", "enum": sorted(business.ROLES)},
            "generation_mode": {"type": "string", "enum": sorted(business.MODES)},
            "instance_meaning": {**text, "description": "一个实例在业务上代表什么。"},
            "identity_rule": {**text, "description": "实例如何唯一标识及去重，必须由真实来源字段或事实支撑。"},
            "empty_policy": {"type": "string", "enum": sorted(business.EMPTY)},
            "empty_reason": {**text, "description": "说明为何允许/不允许为空；即使 REQUIRE_NONEMPTY 也须填写。"},
            "mapping_refs": {"type": "array", "items": text,
                             "description": "引用本次 mappings 中真实存在的 id；除 SUBCLASS_MEMBERS/INTERNAL 外必须非空。"},
            "default_business_exploration": {"type": "boolean", "description": "INTERNAL_EVIDENCE 必须显式为 false。"},
        },
    }
    if versioned:
        instance["required"] = ["business_role", "generation_mode", "instance_meaning", "identity_rule", "empty_policy", "empty_reason"]
        instance["allOf"] = [
            {"if": {"properties": {"generation_mode": {"not": {"enum": ["SUBCLASS_MEMBERS", "INTERNAL"]}}}, "required": ["generation_mode"]},
             "then": {"required": ["mapping_refs"], "properties": {"mapping_refs": refs}}},
            {"if": {"properties": {"business_role": {"const": "INTERNAL_EVIDENCE"}}, "required": ["business_role"]},
             "then": {"required": ["default_business_exploration"], "properties": {"default_business_exploration": {"const": False}}}},
        ]
    instance_guidance = deepcopy(instance)
    instance_guidance.pop("required", None)
    instance_guidance.pop("allOf", None)
    item = {
        "type": "object", "additionalProperties": True,
        "required": ["id", "mapping_type", "target", "source_refs"],
        "properties": {
            "id": {**text, "description": "本批唯一且稳定的映射 ID；供实例合同和 CQ source_refs 引用。"},
            "mapping_type": {"type": "string", "enum": sorted(types),
                             "description": "TABLE_TO_CLASS 按快照业务键编译；SQL_TO_CLASS 仅在提供受控的 subject_class/joins/filter 结构化 derivation 后编译，绝不执行 source 中的任意 SQL；COLUMN_VALUE_TO_CLASS 仍需单独评审。RULE_TO_CLASS 声明已审规则的结论类（由规则执行产生成员），或在 derivation.premise_predicate 指定时声明该规则的派生前提类（由推理能力 evidence_query 结果行经 fact_bindings 产生成员）；均不生成 OBDA。派生类必须与基础业务对象使用同一实例 IRI。"},
            "target": {**text, "description": "目标业务类或属性；保持与运行绑定和本体声明一致。"},
            "source": {**text, "description": "资料映射的来源说明，或数据库真实表/列/查询来源；不能替代 source_refs。"},
            "source_refs": {**refs, "description": "必填：本工程已登记证据/资料或授权表列/查询回执的可定位引用；复用正式来源标识，不自编来源。"},
            "target_label_zh": {**text, "description": "有业务含义的中文名称；正式门禁也接受 label_zh 或已知词汇中文名称，不要求重复填写别名。"},
            "target_comment_zh": {**text, "description": "推荐明确填写中文业务定义；也接受 comment_zh/definition_zh/definition，缺省时平台按中文名称生成说明。"},
            "label_zh": {"type": "string", "description": "target_label_zh 的既有兼容别名。"},
            "comment_zh": {"type": "string", "description": "target_comment_zh 的既有兼容别名。"},
            "definition_zh": {"type": "string"}, "definition": {"type": "string"},
            "domain": {**text, "description": "属性所属已声明业务类；资料属性必须填写，不用 subject 替代。"},
            "range": {**text, "description": "对象属性指向的已声明业务类；不能用 xsd 数据类型替代。"},
            "datatype": {**text, "description": "数据属性类型，如 xsd:string 或完整 datatype IRI；与真实数据及运行时声明一致。"},
            "class": {"type": "string", "description": "部分数据库数据属性映射的既有 domain 别名。"},
            "derivation": {
                "type": "object", "additionalProperties": True,
                "description": "平台编译绑定；只能引用当前 S1 已登记快照。无法确定时保留待办，不猜表或列。",
                "properties": {
                    "from_snapshot_table": text, "source_id": text,
                    "from_snapshot_column": text, "join_target": text,
                    "from_candidate": {**text, "description": "对应S2正式候选ID；自动规则字段必须与condition_contract引用的DATA_PROPERTY及其来源表列一致。映射骨架已生成时沿用。"},
                    "identity_columns": {"type": "array", "items": text, "minItems": 1, "uniqueItems": True,
                                         "description": "类的有序业务标识列；复合标识全部列出，与业务 identity_rule 一致。"},
                    "subject_class": {**text, "description": "SQL_TO_CLASS 必填：已编译 TABLE_TO_CLASS 的基础类名；派生类成员复用其业务实例 IRI、来源表和标识列。"},
                    "rule_id": {**text, "description": "RULE_TO_CLASS 必填：S2 已审规则 id；运行推理能力须把该规则结论谓词（或 premise_predicate）绑定到 mapping.target 对应的类 IRI。"},
                    "premise_predicate": {**text, "description": "RULE_TO_CLASS 可选：声明规则前提类时填写，必须是该规则 premise_predicates 之一，且被引用该规则的推理能力 fact_bindings 消费、ontology_terms 指向 mapping.target 类 IRI。"},
                    "joins": {"type": "array", "maxItems": 3, "items": {"type": "object", "additionalProperties": False,
                              "required": ["source_table", "left_column", "right_column"],
                              "properties": {"source_table": text, "source_id": text, "left_table": text,
                                             "left_column": text, "right_column": text}},
                              "description": "SQL_TO_CLASS 可选：仅连接 S1 已登记快照；left_table 缺省为基础表。"},
                    "filter": {"type": "object", "additionalProperties": True,
                               "description": "SQL_TO_CLASS 必填：结构化条件。叶子 {table?,column,op,value|other_column,other_table?,datatype?}；op=EQ/NE/GT/GE/LT/LE/IN/IS_NULL/IS_NOT_NULL。组合 {all:[...]} 或 {any:[...]}；字段须来自基础表或 joins，数值比较显式 datatype=decimal。"},
                },
            },
            "instance_contract": instance_guidance,
        },
        "allOf": [{
            "if": {"properties": {"mapping_type": {"enum": [
                "COLUMN_VALUE_TO_OBJECT_PROPERTY", "SQL_TO_OBJECT_PROPERTY", "CANDIDATE_JOIN_TO_OBJECT_PROPERTY", "EVIDENCE_TO_OBJECT_PROPERTY",
            ]}}, "required": ["mapping_type"]},
            "then": {"required": ["domain", "range"]},
        }],
    }
    if versioned:
        item["allOf"].append({"if": {"properties": {"mapping_type": {"enum": sorted(t for t in types if t.endswith("TO_CLASS"))}}, "required": ["mapping_type"]},
                              "then": {"required": ["instance_contract"], "properties": {"instance_contract": instance}}})
    return {
        "type": "object", "additionalProperties": True, "required": ["mappings"],
        "properties": {
            "namespace": {**text, "description": "工程本体命名空间；与运行时正式 IRI/OBDA 声明一致，后续 S4 沿用。"},
            "mappings": {"type": "array", "minItems": 1, "items": item},
            "business_query_plans": {
                "type": "array", "maxItems": 32, "items": business_query_plan_schema(),
                "description": "可选的业务能力编制。引用 mappings 稳定ID描述对象、关系、属性、条件和S2规则，由 compile_mapping_runtime 生成 SPARQL/事实绑定/CQ执行绑定。首版仅快照事实查询与同一对象的正向一元规则；不支持算子必须明示待办，不手写替代业务定义。验收期望须来自独立已知数据，不能由编译器生成。",
            },
        },
    }


def business_query_plan_schema() -> dict:
    from harness.orion_workflow_contracts import (
        CQ_RUNTIME_BINDING_SCHEMA,
        QUERY_PARAMETER_SCHEMA,
        QUERY_VALIDATION_CASE_SCHEMA,
        REASONING_RUNTIME_VALIDATION_SCHEMA,
    )

    schema = deepcopy(BUSINESS_QUERY_PLAN_SCHEMA)
    props = schema["properties"]
    props["parameters"]["additionalProperties"] = deepcopy(QUERY_PARAMETER_SCHEMA)
    props["validation_cases"]["items"] = deepcopy(QUERY_VALIDATION_CASE_SCHEMA)
    props["cq_bindings"]["additionalProperties"] = deepcopy(CQ_RUNTIME_BINDING_SCHEMA)
    props["cq_bindings"]["additionalProperties"]["properties"]["answer_mode"] = {"const": "FACT_QUERY"}
    rule_props = props["rule"]["properties"]
    rule_props["runtime_validation"] = deepcopy(REASONING_RUNTIME_VALIDATION_SCHEMA)
    rule_props["validation_cases"]["items"] = deepcopy(QUERY_VALIDATION_CASE_SCHEMA)
    binding = deepcopy(CQ_RUNTIME_BINDING_SCHEMA)
    # Execution details are generated from the selected S2 rule and mappings.
    for key in ("cq_sparql", "reasoning_capability", "derived_predicates"):
        binding["properties"].pop(key, None)
        if key in binding.get("required", []):
            binding["required"].remove(key)
    binding.pop("allOf", None)
    binding["properties"]["answer_mode"] = {"const": "RULE_INFERENCE"}
    rule_props["cq_bindings"]["additionalProperties"] = binding
    return schema
