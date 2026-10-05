"""Strict, reusable MCP input contracts for ORION workflow business payloads.

These schemas are intentionally explicit at the model-facing boundary.  The
workflow service remains the authority for semantic and evidence validation;
the schemas stop an agent from inventing field names before the request reaches
those gates.
"""

from __future__ import annotations

from typing import Any

from services.ontology_contracts.rule_conditions import RULE_CONDITION_SCHEMA


def object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": additional_properties,
        "properties": properties,
        "required": required or [],
    }


NON_EMPTY_STRING = {"type": "string", "minLength": 1}
STRING_LIST = {"type": "array", "items": NON_EMPTY_STRING}

# S3 read-back cards carry platform decision/fingerprint fields.  Keep these
# objects extensible so an unchanged reviewed card can be resubmitted with the
# complete runtime design.  The workflow gate validates IDs and evidence.
S3_EVIDENCE_FACT_SCHEMA = object_schema(
    {
        "summary": {**NON_EMPTY_STRING, "description": "真实来源支持的事实摘要，不把 AI 推断写成事实。"},
        "source_refs": {**STRING_LIST, "minItems": 1, "description": "本工程可定位的资料、表/列或查询回执引用。"},
    },
    ["summary", "source_refs"],
    additional_properties=True,
)

S3_OPTION_SCHEMA = object_schema(
    {
        "id": {**NON_EMPTY_STRING, "description": "当前卡内唯一的稳定方案 ID。"},
        "label": {**NON_EMPTY_STRING, "description": "业务人员看到的中文选项名称。"},
        "summary": {**NON_EMPTY_STRING, "description": "该方案采用的具体业务口径。"},
        "impact": {**NON_EMPTY_STRING, "description": "选择该方案对业务范围、判断或结果的影响。"},
        "recommended": {"type": "boolean", "description": "两个方案中必须且只能一个为 true；建议不等于用户已批准。"},
        "mapping_updates": {
            "type": "array",
            "items": object_schema({"id": NON_EMPTY_STRING}, ["id"], additional_properties=True),
            "description": "可省略或为空；每项 id 必须引用 mapping_draft 中已有映射，其他字段为该映射的拟议更新。不能用此字段修改 S2 规则。",
        },
        "action": {**NON_EMPTY_STRING, "description": "默认 APPLY；业务选择需要正式退回 S2 时使用 RETURN_TO_S2。保留平台回读的 action，不自行伪造决定。"},
    },
    ["id", "label", "summary", "impact"],
    additional_properties=True,
)

S3_CARD_SCHEMA = object_schema(
    {
        "id": {**NON_EMPTY_STRING, "description": "本批确认卡内唯一稳定 ID；不得使用平台保留值 S3-OVERALL-MAPPING-REVIEW。"},
        "title": {**NON_EMPTY_STRING, "description": "中文卡片标题；不能仅提供 business_question 而省略 title。"},
        "business_question": {**NON_EMPTY_STRING, "description": "需要业务负责人选择的具体问题，不让用户填写工程内部字段。"},
        "evidence": object_schema(
            {
                "database_facts": {"type": "array", "items": S3_EVIDENCE_FACT_SCHEMA, "description": "DATABASE_ONLY/HYBRID 至少一项；DOCUMENT_ONLY 可为空。"},
                "business_materials": {"type": "array", "items": S3_EVIDENCE_FACT_SCHEMA, "description": "资料证据；DOCUMENT_ONLY 至少有 business_materials 或 customer_interviews 一路证据。"},
                "customer_interviews": {"type": "array", "items": {"anyOf": [NON_EMPTY_STRING, {"type": "object"}]}, "description": "已有客户访谈依据，可保留既有字符串或结构化记录；须能追溯，不可编造访谈。"},
                "ai_inference": {**NON_EMPTY_STRING, "description": "单独说明 AI 判断和仍需决定之处。"},
            },
            ["ai_inference"],
            additional_properties=True,
        ),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "options": {"type": "array", "items": S3_OPTION_SCHEMA, "minItems": 2, "maxItems": 2, "description": "恰好两个方案，ID 互不重复，且恰好一个 recommended=true；最终由现有工作流门禁校验。"},
        "affected_mapping_ids": {**STRING_LIST, "description": "受影响的已有 Mapping ID；可省略或为空，不得引用不存在的映射。"},
        "technical_impact": {**STRING_LIST, "description": "可选技术影响说明。"},
        "decision_basis": {**STRING_LIST, "description": "可选决策依据说明；不是用户批准记录。"},
    },
    ["id", "title", "business_question", "evidence", "confidence", "options"],
    additional_properties=True,
)

S3_AUTOMATIC_DECISION_SCHEMA = object_schema(
    {
        "id": {**NON_EMPTY_STRING, "description": "建议填写稳定 ID；本批自动决定内唯一且不得与确认卡 ID 重复。省略时平台按决定内容生成稳定 ID。"},
        "topic": {**NON_EMPTY_STRING, "description": "决定涉及的中文业务主题。"},
        "decision": {**NON_EMPTY_STRING, "description": "证据已支持、无需高影响人工选择的具体决定。"},
        "reason": {**NON_EMPTY_STRING, "description": "做出该决定的依据与原因。"},
        "source_refs": {**STRING_LIST, "minItems": 1},
        "affected_mapping_ids": {**STRING_LIST, "description": "受影响的已有 Mapping ID；允许省略或空数组，有值时必须存在。"},
    },
    ["topic", "decision", "reason", "source_refs"],
    additional_properties=True,
)

RULE_PREDICATE_SCHEMA = {
    **NON_EMPTY_STRING,
    "pattern": r"^[A-Za-z_][A-Za-z0-9_:-]{0,127}$",
    "description": "仅填稳定 ASCII 谓词名，区分大小写；首字符为英文字母或下划线，其后允许字母、数字、下划线、冒号和连字符，最多128字符。不得填中文谓词或含参数的完整原子。中文含义写入 name/description，并保持与来源业务语义对应。",
    "examples": ["ParameterObserved", "NeedsReview"],
}
RULE_FACT_SCHEMA = {
    **NON_EMPTY_STRING,
    "pattern": r"^\s*[A-Za-z_][A-Za-z0-9_:-]{0,127}\s*\([^()]*\)\s*$",
    "description": "每项是一个完整测试事实原子 Predicate(arguments)，不是谓词名、自然语言或 IF/THEN 规则。谓词使用 ASCII 标识；参数可保留中文值和原始业务编码，包括连字符，不为迎合谓词格式改写常量。此格式校验不证明参数绑定或数值比较正确。",
    "examples": ["ParameterObserved(sample_1,SC-JYH999,1.02)", "NeedsReview(sample_1)"],
}

INITIAL_COMPETENCY_QUESTION_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "question": NON_EMPTY_STRING,
        "expected": NON_EMPTY_STRING,
        "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "example_entities": STRING_LIST,
    },
    ["question", "expected"],
)

# S2 records what one instance of a business class means.  The S3 mapping
# contract reuses the same vocabulary and adds executable mapping_refs.
S2_INSTANCE_CONTRACT_REQUIRED = [
    "business_role", "generation_mode", "instance_meaning", "identity_rule", "empty_policy", "empty_reason",
]
INSTANCE_CONTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "description": "business-first-v1 工程的 CLASS 候选必须填写：说明一个实例在业务上是什么、如何生成与去重、是否允许为空。S2 可暂不填 mapping_refs，S3 映射时补齐。不要把实例合同写进 description。",
    "properties": {
        "business_role": {"type": "string", "enum": ["ABSTRACT", "ASSESSMENT_RESULT", "BUSINESS_OBJECT", "BUSINESS_RECORD", "DERIVED_CLASSIFICATION", "DICTIONARY", "INTERNAL_EVIDENCE"]},
        "generation_mode": {"type": "string", "enum": ["DICTIONARY_ITEMS", "DOCUMENT_FACTS", "INTERNAL", "RULE_DERIVED", "SOURCE_MAPPING", "SUBCLASS_MEMBERS"]},
        "instance_meaning": {**NON_EMPTY_STRING, "description": "一个实例在业务上代表什么。"},
        "identity_rule": {**NON_EMPTY_STRING, "description": "实例如何唯一标识及去重，由真实来源字段或事实支撑。"},
        "empty_policy": {"type": "string", "enum": ["ALLOW_EMPTY", "NO_DIRECT_INSTANCES", "REQUIRE_NONEMPTY"]},
        "empty_reason": {**NON_EMPTY_STRING, "description": "说明为何允许/不允许为空；REQUIRE_NONEMPTY 也须填写。"},
        "mapping_refs": {"type": "array", "items": NON_EMPTY_STRING, "description": "S2 可省略；S3 引用本批映射 id。"},
        "default_business_exploration": {"type": "boolean", "description": "INTERNAL_EVIDENCE 必须显式为 false。"},
    },
}

# S2 may state where a candidate comes from in structured form.  Optional for
# compatibility: older projects only described the column or join in prose, and
# the S3 skeleton then reports an open item instead of guessing.
CANDIDATE_SOURCE_BINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": "候选的结构化来源绑定，供 S3 机械生成映射骨架。只填真实存在于已授权快照的表列；不确定就省略本字段并在 description 说明，平台会列为待人工确认项，不要填推测值。",
    "properties": {
        "table": {**NON_EMPTY_STRING, "description": "已授权快照中的业务表名，与 source_refs 的 table: 引用一致。"},
        "column": {**NON_EMPTY_STRING, "description": "DATA_PROPERTY 的来源列名；不含表前缀，不填技术列。"},
        "declared_sql_type": {**NON_EMPTY_STRING, "description": "来源库中该列的原始类型，如 numeric/timestamptz；与快照物理类型不同时平台会提示需显式转换。"},
        "join": {
            "type": "object",
            "additionalProperties": False,
            "description": "OBJECT_PROPERTY 的连接列；多对多请指向桥接表并分别登记两段关系。",
            "properties": {
                "from_table": NON_EMPTY_STRING,
                "from_column": NON_EMPTY_STRING,
                "to_table": NON_EMPTY_STRING,
                "to_column": NON_EMPTY_STRING,
            },
            "required": ["from_table", "from_column", "to_table", "to_column"],
        },
    },
}

ONTOLOGY_CANDIDATE_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "name": NON_EMPTY_STRING,
        "label_zh": {"type": "string", "description": "业务人员可理解的中文名称；name 用英文标识时必填，S3 骨架直接用作 target_label_zh。"},
        "kind": {
            "type": "string",
            "enum": ["CLASS", "OBJECT_PROPERTY", "DATA_PROPERTY", "INDIVIDUAL"],
        },
        "status": {
            "type": "string",
            "enum": [
                "DATABASE_FACT",
                "DOCUMENT_EVIDENCE",
                "AI_INFERENCE",
                "NEEDS_HUMAN_CONFIRMATION",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "description": {"type": "string"},
        "source_refs": {**STRING_LIST, "minItems": 1},
        "review_required": {"type": "boolean"},
        "instance_contract": INSTANCE_CONTRACT_SCHEMA,
        "source_binding": CANDIDATE_SOURCE_BINDING_SCHEMA,
    },
    ["id", "name", "kind", "status", "source_refs"],
)

RULE_TEST_CASE_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "case_type": {
            "type": "string",
            "enum": ["POSITIVE", "NEGATIVE", "BOUNDARY"],
        },
        "facts": {
            "type": "array",
            "items": RULE_FACT_SCHEMA,
            "minItems": 1,
            "description": "具有明确假设的候选逻辑场景，非生产观测或验收证据。S2 使用与运行链路共用的参数匹配函数核验 ?变量、常量和跨前提的记录一致性；NOT 按同一绑定检查测试快照。负例可以保留完整字典事实，不能为通过门禁删除不相关事实或补造达标事实。S2 不执行数值比较或聚类算法，不替代 S3 数据绑定与 S6 实际引擎验证。",
        },
        "expected_outcome": {
            "type": "string",
            "enum": ["FIRE", "NO_FIRE"],
            "description": "POSITIVE 必须 FIRE，NEGATIVE 必须 NO_FIRE；BOUNDARY 按真实边界预期填 FIRE 或 NO_FIRE。三类用例均须提供。",
        },
        "note": {"type": "string"},
    },
    ["id", "case_type", "facts", "expected_outcome"],
)

# Closed-world rule inputs have a larger, capability-specific contract.  Their
# top-level fields are named here and the workflow gate validates exact source
# binding, hashes, completeness and PII scope.
CLOSED_WORLD_INPUT_SCHEMA = object_schema(
    {
        "predicate": RULE_PREDICATE_SCHEMA,
        "dataset_id": NON_EMPTY_STRING,
        "dataset_type": {"type": "string", "enum": ["PRODUCTION_EVIDENCE"]},
        "production_evidence": {"type": "boolean", "const": True},
        "source_sha256": NON_EMPTY_STRING,
        "snapshot_sha256": NON_EMPTY_STRING,
        "snapshot_version": NON_EMPTY_STRING,
        "completeness": {
            "type": "string",
            "enum": ["COMPLETE_FOR_CASE_AND_SNAPSHOT"],
        },
        "row_count": {"type": "integer", "minimum": 0},
        "key_fields": {**STRING_LIST, "minItems": 1},
        # Role -> real column name.  See REQUIRED_SET_SOURCE_SCHEMA below.
        "field_bindings": {
            "type": "object",
            "additionalProperties": NON_EMPTY_STRING,
            "minProperties": 6,
            "description": (
                "必填角色键：record_id（快照所属记录列）、classifier_id（分类列）、subject_id（被比较的主体列）、"
                "status_field（受理/接受状态列）、snapshot_version（快照版本列）、source_locator（行级来源定位列）；"
                "值为本工程真实列名，record_id 与 subject_id 绑定的列必须同时出现在 key_fields。"
                "兼容历史政务命名 case_id/service_item_id/material_code/submit_status，但不能混用两套拼写。"
            ),
        },
        "status_filter": {**STRING_LIST, "minItems": 1},
        "source_refs": {**STRING_LIST, "minItems": 1},
        "pii_scope": object_schema(
            {
                "mode": {"type": "string", "enum": ["MINIMUM_NECESSARY"]},
                "allowed_fields": {**STRING_LIST, "minItems": 1},
                "direct_identifiers_included": {"type": "boolean", "const": False},
            },
            ["mode", "allowed_fields", "direct_identifiers_included"],
        ),
    },
    [
        "predicate",
        "dataset_id",
        "dataset_type",
        "production_evidence",
        "source_sha256",
        "snapshot_sha256",
        "snapshot_version",
        "completeness",
        "row_count",
        "key_fields",
        "field_bindings",
        "status_filter",
        "source_refs",
        "pii_scope",
    ],
)

REQUIRED_SET_SOURCE_SCHEMA = object_schema(
    {
        "predicate": RULE_PREDICATE_SCHEMA,
        "dataset_id": NON_EMPTY_STRING,
        "dataset_type": {"type": "string", "enum": ["PRODUCTION_EVIDENCE"]},
        "production_evidence": {"type": "boolean", "const": True},
        "source_sha256": NON_EMPTY_STRING,
        "snapshot_sha256": NON_EMPTY_STRING,
        "snapshot_version": NON_EMPTY_STRING,
        "row_count": {"type": "integer", "minimum": 0},
        # Role -> real column name.  Roles are domain-neutral so a
        # manufacturing or finance project never has to reuse government
        # material-checklist field names; the historical government spelling
        # (service_item_id / material_code) stays accepted by the workflow gate.
        "field_bindings": {
            "type": "object",
            "additionalProperties": NON_EMPTY_STRING,
            "minProperties": 4,
            "description": (
                "必填角色键：classifier_id（缩小必需集合的分类列）、subject_id（被比较的主体列）、"
                "mandatory（是否必需列）、source_locator（行级来源定位列）；值为本工程真实列名。"
                "兼容历史政务命名 service_item_id/material_code/mandatory/source_locator，但不能混用。"
            ),
        },
        "source_refs": {**STRING_LIST, "minItems": 1},
    },
    [
        "predicate",
        "dataset_id",
        "dataset_type",
        "production_evidence",
        "source_sha256",
        "snapshot_sha256",
        "snapshot_version",
        "row_count",
        "field_bindings",
        "source_refs",
    ],
)

CQ_SEMANTIC_ASSESSMENT_SCHEMA = object_schema(
    {
        "question_id": {**NON_EMPTY_STRING, "description": "当前 S0 原始 CQ ID；提交列表时须逐题覆盖且不能重复。"},
        "answer_kind": {"type": "string", "enum": ["FACT_LOOKUP", "AGGREGATION", "RELATION", "RULE_INFERENCE", "DOCUMENT_EVIDENCE"]},
        "business_definition": {"type": "string", "description": "作答所需业务定义、时间/范围和输出口径；为空时必须在 missing_semantics 说明缺失。"},
        "definition_source_refs": STRING_LIST,
        "requires_business_confirmation": {"type": "boolean"},
        "required_candidate_ids": STRING_LIST,
        "required_rule_ids": STRING_LIST,
        "missing_semantics": STRING_LIST,
        "required_fact_descriptions": STRING_LIST,
        "missing_data": STRING_LIST,
        "premise_bindings": {
            "type": "array",
            "description": "规则外部前提与已登记证据的关联，仅说明依据，不声称实例或算子已通过运行验证。",
            "items": object_schema(
                {"rule_id": NON_EMPTY_STRING, "predicate": RULE_PREDICATE_SCHEMA, "source_refs": STRING_LIST},
                ["rule_id", "predicate", "source_refs"],
            ),
        },
    },
    ["question_id", "answer_kind", "business_definition", "definition_source_refs", "requires_business_confirmation", "required_candidate_ids", "required_rule_ids", "missing_semantics", "required_fact_descriptions", "missing_data"],
)

BUSINESS_RULE_CANDIDATE_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "name": NON_EMPTY_STRING,
        "rule_type": NON_EMPTY_STRING,
        "description": NON_EMPTY_STRING,
        "kind": {"type": "string", "enum": ["RULE_CANDIDATE"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "status": ONTOLOGY_CANDIDATE_SCHEMA["properties"]["status"],
        "source_refs": {**STRING_LIST, "minItems": 1},
        "business_question_ids": {**STRING_LIST, "minItems": 1},
        "formal_expression": {
            **NON_EMPTY_STRING,
            "pattern": r"^\s*[Ii][Ff]\s+[\s\S]+\s+[Tt][Hh][Ee][Nn]\s+[\s\S]+$",
            "description": "单结论 Horn 规则正文：IF Predicate(?x) AND OtherPredicate(?x) THEN Conclusion(?x)。谓词用稳定 ASCII 名称；各原子含参数，声明字段则只填名称。NOT 仅在现有闭世界能力、完整快照和来源绑定合同满足时使用。不要改写原业务常量或用枚举结果替代数值条件。",
            "examples": ["IF ParameterObserved(?x) AND ThresholdExceeded(?x) THEN NeedsReview(?x)"],
        },
        "premise_predicates": {
            "type": "array",
            "items": RULE_PREDICATE_SCHEMA,
            "minItems": 1,
            "description": "仅列 IF 部分出现的全部谓词名称（含 NOT 后的名称），区分大小写；例如 [ParameterObserved, ThresholdExceeded]。不要填 ParameterObserved(?x)。",
        },
        "condition_contract": RULE_CONDITION_SCHEMA,
        "conclusion_predicate": {
            **RULE_PREDICATE_SCHEMA,
            "description": RULE_PREDICATE_SCHEMA["description"]
            + " 必须与 THEN 后唯一结论原子的谓词名完全一致，例如 NeedsReview，不是 NeedsReview(?x)。",
        },
        "test_cases": {
            "type": "array",
            "items": RULE_TEST_CASE_SCHEMA,
            "minItems": 3,
        },
        "review_required": {"type": "boolean"},
        "required_capabilities": STRING_LIST,
        "closed_world_inputs": {
            "type": "array",
            "items": CLOSED_WORLD_INPUT_SCHEMA,
        },
        "required_set_source": REQUIRED_SET_SOURCE_SCHEMA,
    },
    [
        "id",
        "name",
        "rule_type",
        "description",
        "status",
        "source_refs",
        "business_question_ids",
        "formal_expression",
        "premise_predicates",
        "conclusion_predicate",
        "test_cases",
    ],
)

CQ_ASSERTION_SCHEMA = object_schema(
    {
        "binding": NON_EMPTY_STRING,
        "operator": {
            "type": "string",
            "enum": ["EQ", "NE", "GT", "GTE", "LT", "LTE", "APPROX", "CONTAINS"],
        },
        "expected": {},
        "row": {"type": "string", "enum": ["FIRST", "ANY", "ALL"]},
        "tolerance": {"type": "number", "minimum": 0},
    },
    ["binding", "operator", "expected"],
)

CQ_BUSINESS_DIMENSION_SCHEMA = object_schema(
    {
        "dimension": NON_EMPTY_STRING,
        "label_zh": NON_EMPTY_STRING,
        "applicability": {
            "type": "string",
            "enum": ["REQUIRED", "NOT_APPLICABLE"],
            "default": "REQUIRED",
        },
        "binding": {**NON_EMPTY_STRING, "description": "applicability 为 REQUIRED（默认）时必填；必须是本 CQ SELECT 实际返回变量，不能填写业务维度名称代替变量。"},
        "ontology_term": {
            "type": "string",
            "pattern": "^(?:https?://|urn:)",
            "description": "applicability 为 REQUIRED（默认）时与 binding 同时必填；使用已声明的本体类或属性完整 IRI，不能填局部名或中文描述。",
        },
        "path": {"type": "string", "description": "已声明且在该 CQ SPARQL 中实际引用的单个对象属性或数据属性的完整 IRI，如 https://example.com/ontology#projectName。不是局部名、类名、点号链或 SPARQL / 属性链；多跳查询仍在 sparql 中表达，本字段引用支撑该输出维度的实际属性。关系题非 subject 的适用维度必须提供。"},
        "evidence_refs": {**STRING_LIST, "minItems": 1},
        "reason_zh": {"type": "string"},
    },
    ["dimension", "label_zh", "evidence_refs"],
)

# Mirror the existing normalizer: omitted applicability means REQUIRED.
CQ_BUSINESS_DIMENSION_SCHEMA["allOf"] = [{
    "if": {"required": ["applicability"], "properties": {"applicability": {"const": "NOT_APPLICABLE"}}},
    "then": {"required": ["reason_zh"]},
    "else": {"required": ["binding", "ontology_term"]},
}]

CQ_NULLABLE_BINDINGS_SCHEMA = {
    "type": "object",
    "description": "仅已审来源条件下允许必填输出为空；键为该输出字段。条件必须是同一行其他非空必填字段的具体等值AND，不填0、不跳过结果/边界断言。",
    "additionalProperties": object_schema(
        {
            "when": {"type": "object", "minProperties": 1, "additionalProperties": {"type": ["string", "number", "boolean"]}},
            "reason_zh": NON_EMPTY_STRING,
            "source_refs": {**STRING_LIST, "minItems": 1},
        },
        ["when", "reason_zh", "source_refs"],
    ),
}

CQ_EXACT_ROWS_PROPERTIES = {
    "expected_row_fields": {"type": "array", "minItems": 1, "uniqueItems": True,
        "items": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "description": "独立指定完整结果比较字段，必须属于实际返回字段；不从投影或首行自动补齐。"},
    "expected_rows": {"type": "array", "items": {"type": "object", "additionalProperties": {
        "type": ["string", "number", "boolean", "null"]}},
        "description": "独立预期完整结果，按expected_row_fields组成行元组进行无序多重集合比较。每行须恰好包含这些字段，重复次数参与比较；[]要求结果为空。"},
}
CQ_EXACT_ROWS_DEPENDENCIES = {"expected_row_fields": ["expected_rows"], "expected_rows": ["expected_row_fields"]}

CQ_ANSWER_CONTRACT_SCHEMA = object_schema(
    {
        "contract_version": {"type": "string", "enum": ["cq-answer-v2"]},
        "query_type": {"type": "string", "enum": ["SELECT", "ASK", "CONSTRUCT", "DESCRIBE"]},
        "answer_mode": {
            "type": "string",
            "enum": ["FACT_QUERY", "EVIDENCE_QUERY", "RULE_INFERENCE", "OWL_INFERENCE"],
        },
        "reasoning_capability": {"type": ["string", "null"]},
        "derived_predicates": STRING_LIST,
        "required_bindings": {**STRING_LIST, "minItems": 1},
        "nullable_bindings": CQ_NULLABLE_BINDINGS_SCHEMA,
        "min_rows": {"type": "integer", "minimum": 0},
        **CQ_EXACT_ROWS_PROPERTIES,
        "expected_boolean": {"type": "boolean"},
        "min_triples": {"type": "integer", "minimum": 0},
        "result_assertions": {"type": "array", "items": CQ_ASSERTION_SCHEMA},
        "boundary_assertions": {"type": "array", "items": CQ_ASSERTION_SCHEMA},
        "required_business_dimensions": {
            "type": "array",
            "items": CQ_BUSINESS_DIMENSION_SCHEMA,
        },
        "required_sparql_fragments": STRING_LIST,
        "forbidden_sparql_fragments": STRING_LIST,
    },
)
CQ_ANSWER_CONTRACT_SCHEMA["dependentRequired"] = CQ_EXACT_ROWS_DEPENDENCIES

COMPETENCY_QUESTION_OVERRIDE_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "question": NON_EMPTY_STRING,
        "sparql": {"type": "string", "pattern": "^(?i:SELECT|ASK|CONSTRUCT|DESCRIBE)\\b"},
        "expected": NON_EMPTY_STRING,
        "coverage_status": {"type": "string", "enum": ["DIRECT", "NEEDS_REVIEW"]},
        "source": NON_EMPTY_STRING,
        "source_question_id": NON_EMPTY_STRING,
        "source_question_sha256": NON_EMPTY_STRING,
        "priority": {"type": "string"},
        "example_entities": STRING_LIST,
        "generation_note": {"type": "string"},
        "answer_contract": CQ_ANSWER_CONTRACT_SCHEMA,
    },
    ["id"],
)

LOGICAL_AXIOM_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "axiom_type": {"type": "string", "enum": ["SUBCLASS_OF", "DISJOINT_WITH", "EQUIVALENT_DATA_HAS_VALUE", "EQUIVALENT_OBJECT_SOME_VALUES_FROM"]},
        "class": NON_EMPTY_STRING,
        "other": NON_EMPTY_STRING,
        "child": NON_EMPTY_STRING,
        "parent": NON_EMPTY_STRING,
        "base_class": NON_EMPTY_STRING,
        "filler": NON_EMPTY_STRING,
        "property": NON_EMPTY_STRING,
        "value": {},
        "source_refs": {**STRING_LIST, "minItems": 1},
    },
    ["id", "axiom_type", "source_refs"],
)
LOGICAL_AXIOM_SCHEMA["oneOf"] = [
    {"properties": {"axiom_type": {"const": kind}}, "required": fields}
    for kind, fields in {
        "SUBCLASS_OF": ["child", "parent"],
        "DISJOINT_WITH": ["class", "other"],
        "EQUIVALENT_DATA_HAS_VALUE": ["class", "base_class", "property", "value"],
        "EQUIVALENT_OBJECT_SOME_VALUES_FROM": ["class", "base_class", "property", "filler"],
    }.items()
]

CQ_RUNTIME_BINDING_SCHEMA = object_schema(
    {
        "validation_case_id": {**NON_EMPTY_STRING, "description": "本查询已审validation_cases中的实际用例ID；平台继承参数及独立期望。expected_first_row只生成FIRST断言，expected_row_fields+expected_rows执行完整集合比较。"},
        "answer_mode": {"type": "string", "enum": ["FACT_QUERY", "EVIDENCE_QUERY", "RULE_INFERENCE", "OWL_INFERENCE"]},
        "answer_scope_zh": {**NON_EMPTY_STRING, "description": "中文说明回答范围、已有判定检索/统计/追溯与实际规则执行的分工，不缩减原业务问题。"},
        "source_refs": {**STRING_LIST, "minItems": 1},
        "nullable_bindings": CQ_NULLABLE_BINDINGS_SCHEMA,
        "boundary_assertions": {"type": "array", "items": CQ_ASSERTION_SCHEMA, "description": "有来源的边界断言；同一用例明确声明expected_rows时才可省略或为空。不能把首行冒称完整集合。"},
        "required_business_dimensions": {"type": "array", "items": CQ_BUSINESS_DIMENSION_SCHEMA},
        "reasoning_capability": NON_EMPTY_STRING,
        "derived_predicates": {**STRING_LIST, "minItems": 1},
        "cq_sparql": {**NON_EMPTY_STRING, "description": "仅推理题：S6 在真实事实加派生图上执行的只读 SELECT；必须实际关联正式派生结论，不能给 Ontop 伪造推理映射。"},
    },
    ["validation_case_id", "answer_mode", "answer_scope_zh", "source_refs"],
)
CQ_RUNTIME_BINDING_SCHEMA["allOf"] = [{
    "if": {"properties": {"answer_mode": {"enum": ["RULE_INFERENCE", "OWL_INFERENCE"]}}},
    "then": {"required": ["reasoning_capability", "derived_predicates", "cq_sparql"]},
}]

# Public S3 input shapes; runtime_release/query_capabilities remain authoritative
# for query safety, cross-field binding, source provenance and actual validation.
QUERY_PARAMETER_SCHEMA = object_schema(
    {
        "type": {"type": "string", "enum": ["boolean", "code", "comparison", "date", "datetime", "decimal", "enum", "integer", "iri", "string"]},
        "description_zh": NON_EMPTY_STRING,
        "required": {"type": "boolean"},
        "default": {},
        "values": STRING_LIST,
        "minimum": {"type": "number"},
        "maximum": {"type": "number"},
        "max_length": {"type": "integer", "minimum": 1},
    },
    ["type", "description_zh"],
)
QUERY_VALIDATION_CASE_SCHEMA = object_schema(
    {
        "id": {**NON_EMPTY_STRING, "pattern": "^[a-z][a-z0-9_]{0,63}$"},
        "question": NON_EMPTY_STRING,
        "parameters": {"type": "object", "additionalProperties": {}},
        "expected_fields": STRING_LIST,
        "min_rows": {"type": "integer", "minimum": 0},
        "expected_first_row": {"type": "object", "additionalProperties": {}},
        "nullable_bindings": CQ_NULLABLE_BINDINGS_SCHEMA,
        **CQ_EXACT_ROWS_PROPERTIES,
    },
    ["question"],
)
QUERY_VALIDATION_CASE_SCHEMA["dependentRequired"] = CQ_EXACT_ROWS_DEPENDENCIES
STRUCTURED_QUERY_CAPABILITY_SCHEMA = object_schema(
    {
        "description_zh": NON_EMPTY_STRING,
        "parameters": {"type": "object", "additionalProperties": QUERY_PARAMETER_SCHEMA},
        "result_fields": {**STRING_LIST, "minItems": 1},
        "question_examples": {**STRING_LIST, "minItems": 1, "maxItems": 20},
        "business_question_ids": STRING_LIST,
        "validation_cases": {"type": "array", "items": QUERY_VALIDATION_CASE_SCHEMA, "minItems": 1, "maxItems": 20},
        "cq_bindings": {"type": "object", "additionalProperties": CQ_RUNTIME_BINDING_SCHEMA},
        "query_mode": {"type": "string", "enum": ["SNAPSHOT_ONLY", "REALTIME_REQUIRED", "HYBRID"]},
        "source_ids": {
            "type": "array", "uniqueItems": True,
            "items": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{2,63}$"},
            "description": "Source Service 连接绑定 ID，不是 dataset/DS-/evidence/table: 标识。纯文件快照未绑定 Source Service 用 []，不得猜造。",
        },
        "source_tables": STRING_LIST,
        "source_columns": STRING_LIST,
        "source_tables_by_id": {"type": "object", "additionalProperties": STRING_LIST},
        "source_columns_by_id": {"type": "object", "additionalProperties": STRING_LIST},
    },
    ["description_zh", "result_fields", "question_examples", "validation_cases"],
    additional_properties=True,
)
# These are submission shapes, not packaged-runtime shapes: S3 requires inline
# rules, while persisted releases use rule_artifact/rule_sha256. Keep extension
# fields and normalizer defaults; semantic, expression and provenance validation
# remains in reasoning_contract/runtime_release.
FACT_BINDING_ARGUMENT_SCHEMA = {
    **object_schema(
        {key: {"not": {"type": "null"}} for key in ("field", "parameter", "constant")},
        additional_properties=True,
    ),
    "oneOf": [{"required": [key]} for key in ("field", "parameter", "constant")],
    "description": "恰好一个来源：field 为列名或文档事实零起始参数索引，parameter 为参数名，constant 为非空值；不能同时填写多个来源。",
}
ROW_CONDITION_VALUE_SCHEMA = {"type": ["string", "number", "boolean"]}
ROW_CONDITION_LEAF_SCHEMA = {"oneOf": [
    object_schema({"field": NON_EMPTY_STRING, "equals": ROW_CONDITION_VALUE_SCHEMA}, ["field", "equals"]),
    object_schema({"field": NON_EMPTY_STRING, "in": {"type": "array", "minItems": 1, "maxItems": 20,
                                                     "items": ROW_CONDITION_VALUE_SCHEMA}}, ["field", "in"]),
    object_schema({"field": NON_EMPTY_STRING, "not_in": {"type": "array", "minItems": 1, "maxItems": 20,
                                                         "items": ROW_CONDITION_VALUE_SCHEMA}}, ["field", "not_in"]),
    object_schema({"field": NON_EMPTY_STRING, "missing": {"type": "boolean"}}, ["field", "missing"]),
]}
ROW_CONDITION_SCHEMA = {"anyOf": [
    ROW_CONDITION_LEAF_SCHEMA,
    object_schema({"all": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": ROW_CONDITION_LEAF_SCHEMA}}, ["all"]),
    object_schema({"any": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": ROW_CONDITION_LEAF_SCHEMA}}, ["any"]),
]}
FACT_BINDING_SCHEMA = object_schema(
    {
        "predicate": NON_EMPTY_STRING,
        "arguments": {"type": "array", "minItems": 1, "items": FACT_BINDING_ARGUMENT_SCHEMA},
        "when": {"anyOf": [{"type": "null"}, ROW_CONDITION_SCHEMA],
                 "description": "仅数据库行事实：按证据行字段有条件地生成该前提事实。支持 {field,equals}、{field,in:[...]}、{field,not_in:[...]}（有值且不在集合内）、{field,missing:true|false}（未绑定或空白视为缺失）或平铺的 {all:[叶子]} / {any:[叶子]}；除 missing 外字段缺失时不生成事实。业务筛选应写在 when 中而非证据查询 FILTER 里，证据查询返回全部候选记录，NEGATIVE 能力才能构造反事实正例。NOT 规则的被否定前提必须仅在真实正向记录满足条件时生成。"},
    }, ["predicate", "arguments"], additional_properties=True,
)
FACT_BINDINGS_SCHEMA = {"type": "array", "minItems": 1, "items": FACT_BINDING_SCHEMA}
DOCUMENT_FACT_SCHEMA = object_schema(
    {
        "fact": {**NON_EMPTY_STRING, "description": "完整原子 Predicate(arg1,arg2)，参数不可嵌套原子；事实必须来自当前工程的真实证据。"},
        "fact_kind": {"type": "string"},
        "provenance": object_schema(
            {key: {"type": "string"} for key in (
                "evidence_id", "document_id", "source_locator", "source_sha256",
                "document_version", "evidence_sha256",
            )}, ["evidence_id"], additional_properties=True,
        ),
    }, ["fact", "provenance"], additional_properties=True,
)
DOCUMENT_FACT_QUERY_SCHEMA = object_schema(
    {
        "description_zh": NON_EMPTY_STRING,
        "fact_source": NON_EMPTY_STRING,
        "fact_bindings": FACT_BINDINGS_SCHEMA,
        "facts": {"type": "array", "items": DOCUMENT_FACT_SCHEMA},
        "fact_artifact": {"type": "string", "description": "已有受控事实制品引用；非空时替代内联 facts，必须绑定 fact_sha256，不得猜路径。"},
        "fact_sha256": {"type": "string"},
        "result_fields": {**STRING_LIST, "minItems": 1},
        "question_examples": {**STRING_LIST, "minItems": 1},
        "source_scope": {"type": "string", "description": "document_evidence（默认）或 document_evidence_and_inference；由正式 normalizer 验证。"},
        "sparql": {**NON_EMPTY_STRING, "description": "在完整、已审文档事实实例图执行的只读 SELECT；聚合可使用 GROUP BY/HAVING，不需要伪造规则或 Ontop。"},
        "ontology_terms": {"type": "object", "minProperties": 1, "additionalProperties": NON_EMPTY_STRING},
        "parameters": {"type": "object", "additionalProperties": QUERY_PARAMETER_SCHEMA},
        "business_question_ids": STRING_LIST,
        "validation_cases": {"type": "array", "items": QUERY_VALIDATION_CASE_SCHEMA, "minItems": 1, "maxItems": 20},
        "cq_bindings": {"type": "object", "additionalProperties": {
            **CQ_RUNTIME_BINDING_SCHEMA,
            "properties": {**CQ_RUNTIME_BINDING_SCHEMA["properties"], "answer_mode": {"const": "FACT_QUERY"}},
        }, "description": "绑定原 S0 CQ；使用本能力的 sparql，不填规则专用 cq_sparql。事实按 field 位置绑定，返回字段、来源、边界与真实验证用例完整。"},
    }, ["description_zh", "fact_source", "fact_bindings", "result_fields", "question_examples"],
    additional_properties=True,
)
DOCUMENT_FACT_QUERY_SCHEMA["allOf"] = [{
    "if": {"required": ["fact_artifact"], "properties": {"fact_artifact": {"type": "string", "pattern": "\\S"}}},
    "then": {"required": ["fact_sha256"], "properties": {"fact_sha256": {"pattern": "^sha256:[a-f0-9]{64}$"}}},
    "else": {"required": ["facts"], "properties": {"facts": {"minItems": 1}}},
}, {
    "if": {"required": ["cq_bindings"], "properties": {"cq_bindings": {"minProperties": 1}}},
    "then": {"required": ["sparql", "ontology_terms", "parameters", "business_question_ids", "validation_cases"]},
}]
REASONING_RULE_SCHEMA = object_schema(
    {
        "rule_id": NON_EMPTY_STRING,
        "description_zh": NON_EMPTY_STRING,
        "expression": {**NON_EMPTY_STRING, "description": "IF ... THEN ... 完整规则；与 source_rule_ids、fact_bindings 和 ontology_terms 一致，表达式与依赖由引擎验证。"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 1.0},
    }, ["rule_id", "description_zh", "expression"], additional_properties=True,
)
REASONING_RUNTIME_VALIDATION_SCHEMA = object_schema(
    {
        "parameters": {"type": ["object", "null"], "additionalProperties": {}},
        "min_input_facts": {"type": "integer", "minimum": 1, "default": 1},
        "min_result_facts": {"type": "integer", "minimum": 0, "default": 1},
        "require_rules_fired": {"type": "boolean", "default": True},
        "expected_live_outcome": {"type": "string", "description": "POSITIVE（默认）或 NEGATIVE。NEGATIVE 必须 min_result_facts=0 且 require_rules_fired=false；由正式 normalizer 联合验证。"},
    }, additional_properties=True,
)
REASONING_CAPABILITY_SCHEMA = object_schema(
    {
        "description_zh": NON_EMPTY_STRING,
        "evidence_query": {**NON_EMPTY_STRING, "description": "当前 ontop_queries 或 document_fact_queries 中已声明的查询名。"},
        "execution_scope": {"type": "string", "enum": ["FULL_QUERY_RESULT", "REPRESENTATIVE_INSTANCE_PROBE"], "description": "FULL_QUERY_RESULT（默认）或 REPRESENTATIVE_INSTANCE_PROBE；生产完整性限制由正式门禁验证。"},
        "fact_bindings": {**FACT_BINDINGS_SCHEMA, "description": "只把来源查询实际返回的外部前提映射为输入事实；不得把 result_predicates 中的规则结论预先映射为输入事实。"},
        "result_predicates": {**STRING_LIST, "minItems": 1, "description": "规则实际产生的结论谓词；必须由 rules 的 THEN 产生，不能在 fact_bindings 中预置。"},
        "question_examples": {**STRING_LIST, "minItems": 1},
        "source_rule_ids": {**STRING_LIST, "minItems": 1},
        "ontology_terms": {"type": "object", "minProperties": 1, "additionalProperties": NON_EMPTY_STRING, "description": "规则谓词到已声明本体绝对 IRI 的映射；类别/关系/数据属性保持各自语义，不是要求每个谓词都创建核心类。"},
        "runtime_validation": REASONING_RUNTIME_VALIDATION_SCHEMA,
        "rules": {"type": "array", "minItems": 1, "items": REASONING_RULE_SCHEMA},
        "closed_world_inputs": {"type": ["array", "null"], "items": {"type": "object"}},
        "required_set_source": {"type": ["object", "null"]},
        "business_question_ids": STRING_LIST,
        "parameters": {"type": "object", "additionalProperties": QUERY_PARAMETER_SCHEMA},
        "result_fields": {**STRING_LIST, "minItems": 1},
        "validation_cases": {"type": "array", "items": QUERY_VALIDATION_CASE_SCHEMA, "minItems": 1, "maxItems": 20,
            "description": "CQ 在完整事实与规则派生图上的真实行结果用例；与 runtime_validation 的规则执行计数分开。"},
        "cq_bindings": {"type": "object", "additionalProperties": {
            **CQ_RUNTIME_BINDING_SCHEMA,
            "properties": {**CQ_RUNTIME_BINDING_SCHEMA["properties"], "answer_mode": {"const": "RULE_INFERENCE"}},
        }, "description": "按原 S0 CQ id 绑定本能力的规则结果查询。reasoning_capability 必须等于当前能力名，派生谓词必须由正式规则产生。纯资料无需 Ontop；不支持在此声明无规则事实查询。"},
    }, ["description_zh", "evidence_query", "fact_bindings", "result_predicates", "question_examples",
        "source_rule_ids", "ontology_terms", "runtime_validation", "rules"], additional_properties=True,
)
REASONING_CAPABILITY_SCHEMA["allOf"] = [{
    "if": {"required": ["cq_bindings"], "properties": {"cq_bindings": {"minProperties": 1}}},
    "then": {"required": ["business_question_ids", "result_fields", "validation_cases"]},
}]
S3_RUNTIME_SUBMISSION_SCHEMA = object_schema(
    {
        "ontop_deployment_id": {**NON_EMPTY_STRING, "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$"},
        "database_access_mode": {"type": "string", "enum": ["READ_ONLY"]},
        "prepared_by": NON_EMPTY_STRING,
        "mapping_obda": {**NON_EMPTY_STRING, "description": "完整 Ontop OBDA 文本，必须含 [PrefixDeclaration]、[MappingDeclaration] 和 source SELECT；不是 R2RML/Turtle、路径或制品描述对象。"},
        "ontop_queries": {
            "type": "object", "minProperties": 1,
            "additionalProperties": {**NON_EMPTY_STRING, "description": "只读 SPARQL SELECT 文本；不要使用 {sparql:...}、路径或 artifact descriptor 对象。"},
        },
        "query_capabilities": {
            "type": "object", "additionalProperties": STRUCTURED_QUERY_CAPABILITY_SCHEMA,
            "description": "键必须与 ontop_queries 完全一致。查询参数用 {{name}} 占位符，按 parameters 类型安全渲染；不是同名 SPARQL ?变量注入。可选段用 {{#name}}...{{name}}...{{/name}}，不用固定日期兜底。parameters 无参数用 {}，result_fields 是字符串数组，validation_cases 是用例对象数组。来源及结果断言必须真实。",
        },
        "document_query_capabilities": {"type": "array", "items": {"type": "string", "enum": ["current_full_text_search", "reviewed_entity_evidence"]}},
        "document_fact_queries": {"type": "object", "propertyNames": {"pattern": "^[a-z][a-z0-9_]{1,63}$"}, "additionalProperties": DOCUMENT_FACT_QUERY_SCHEMA},
        "reasoning_requirement": {"type": "string", "enum": ["REQUIRED", "NOT_APPLICABLE"]},
        "reasoning_not_applicable_reason": {"type": "string", "minLength": 12},
        "reasoning_capabilities": {"type": "object", "propertyNames": {"pattern": "^[a-z][a-z0-9_]{1,63}$"},
            "description": "键是具体推理能力名称，须以小写字母开头且仅含小写字母、数字和下划线（2–64字符）；不是推理引擎类型。不要把 SEMANTICA_FORWARD_V1 等引擎标识用作能力键。",
            "additionalProperties": REASONING_CAPABILITY_SCHEMA},
    },
    additional_properties=True,
)

# Deliberately reject this known silent-drop field without closing legacy extensions.
S3_RUNTIME_SUBMISSION_SCHEMA["not"] = {"required": ["cq_bindings"]}


def s3_runtime_structure_diagnostics(payload: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Bounded shape-only hints; never change gates or echo submitted values."""
    from jsonschema import Draft202012Validator

    diagnostics: list[dict[str, Any]] = []
    if isinstance(payload, dict) and "cq_bindings" in payload:
        diagnostics.append({
            "path": "realtime_runtime.cq_bindings", "reason_code": "UNSUPPORTED_TOP_LEVEL_CQ_BINDINGS",
            "supported_structured_path": "realtime_runtime.query_capabilities.<query_name>.cq_bindings",
            "supported_reasoning_path": "realtime_runtime.reasoning_capabilities.<capability_name>.cq_bindings",
            "instruction": "将原始CQ意图迁移到对应查询或规则能力级绑定后再移除顶层字段。纯资料规则CQ使用 reasoning_capabilities 路线并提供真实行结果用例，不编造 Ontop 资产、规则或验收结果。",
        })
    seen: set[tuple[str, str]] = set()
    for error in Draft202012Validator(S3_RUNTIME_SUBMISSION_SCHEMA).iter_errors(payload):
        if error.validator == "not" and isinstance(payload, dict) and "cq_bindings" in payload:
            continue
        path = "realtime_runtime" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path
        )
        key = (path, str(error.validator))
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {"path": path, "reason_code": f"SCHEMA_{error.validator}"}
        if error.validator == "required" and isinstance(error.instance, dict):
            item["missing_fields"] = [field for field in error.validator_value if field not in error.instance]
        elif error.validator in {"type", "enum", "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum"}:
            item["expected"] = error.validator_value
        else:
            item["instruction"] = "按同一 repair_contract.schemas.realtime_runtime 对应字段结构修正。"
        diagnostics.append(item)
        if len(diagnostics) >= min(max(limit, 1), 20):
            break
    return diagnostics
