"""Compact execution semantics for existing stage input contracts.

Documentation only: authoritative validators and stage gates remain unchanged.
The adjacent tests exercise normalization/packaging rather than treating prose as a gate.
"""
from __future__ import annotations


def build_stage_execution_guidance(stage: str, *, intake_mode: str | None = None) -> dict:
    if stage != "S3":
        return {}
    return {
        "schema_version": 1,
        "scope": "EXISTING_S3_RUNTIME_CONTRACT",
        "submission_path": "realtime_runtime",
        "query_parameters": {
            "path": "realtime_runtime.ontop_queries.<query_name>",
            "placeholder": "使用 {{parameter_name}} 模板占位符。平台按 query_capabilities.<query_name>.parameters 声明校验并安全渲染为 SPARQL 字面量/IRI；不会把同名 ?变量自动注入。不要给占位符额外加引号或 datatype。",
            "example": "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> SELECT ?entity ?at WHERE { ?entity <https://example.test/ontology#observedAt> ?at . FILTER(?at >= {{window_start}} && ?at < {{window_end}}) }",
            "example_parameters": {
                "window_start": {"type": "datetime", "required": True, "description_zh": "窗口开始，含边界，ISO 8601 带时区"},
                "window_end": {"type": "datetime", "required": True, "description_zh": "窗口结束，不含边界，ISO 8601 带时区"},
            },
            "optional": "可选整段用 {{#name}} ... {{name}} ... {{/name}}；参数缺省或空时移除该段。实际使用的参数集合必须恰好等于 parameters 的键集。",
            "validation": "validation_cases[].parameters 给出该用例的真实具体参数；缺少 required 参数会拒绝，不使用 BOUND/IF 加固定历史日期兜底。验证窗口只用于该用例，不能成为日常问答默认日期。",
            "business_time": "近7天等相对时间必须明确相对于提问时刻还是已批准快照基准，记录时区和开闭边界；不得因技术查询能力不足而变更原问题。",
            "review_status": "缺少真实答案行时保留待办，不得编造 expected_first_row 或声称该 CQ 已完成；S3 的 CQ 绑定需要有来源的行结果验收。当前数据库快照可用 query_source_evidence(query_plan) 核验跨表、明确时间窗口与聚合基线；它不代表 Ontop 验收。expected_first_row 须有确定排序（并列时用业务键破同序）；聚合小数不能把浮点截断值当精确 EQ，可在 cq_bindings.boundary_assertions 使用 APPROX、明确 tolerance 和 row=FIRST，并保留真实来源精确值。",
        },
        "document_facts": {
            "path": "realtime_runtime.document_fact_queries.<query_name>",
            "required_inline_fields": ["description_zh", "fact_source", "fact_bindings", "facts", "result_fields", "question_examples"],
            "facts": "提交非空 facts 数组，每项为 {fact: 'Predicate(arg1,arg2)', provenance: {...}}；事实必须来自真实已登记资料，不得把验收预期或推测填成来源事实。",
            "provenance": "每条事实需 evidence_id；沿用真实 document_id、source_locator、source_sha256、document_version、evidence_sha256（可用时提供），不得猜造标识或校验和。fact_source 是说明文字，不能代替逐事实来源。",
            "binding": "fact_bindings 声明 predicate 与 arguments；文档参数位置从 0 开始，例如二元事实用 [{field:0},{field:1}]。普通事实绑定支持 field/parameter/constant 三选一；直接文档 CQ 仅允许按原位置 identity field 索引且不能有 when，不允许查询参数改变原始事实。",
            "source_scope": "document_evidence（默认）或 document_evidence_and_inference；声明后仍须由现有来源及规则检查验证。",
            "agent_owned": "准备内联事实、绑定、查询和有依据的验收样例，通过草稿保存/预检/提交工具交付。先保存分批草稿，不能为猜测包装细节反复读取仓库源码。",
            "platform_owned": "S3 提交链路调用 write_runtime_review_assets，将内联 facts 写为 03-mapping-review/runtime/document-facts/<query_name>.json，并生成 fact_artifact、fact_sha256 和 runtime-source.json。新提交不要自行编造或创建这些输出文件；fact_artifact 并非必须事先准备的输入。",
            "rehydration": "已审资产回读由 load_reviewed_runtime_submission 将事实包恢复为内联 facts 后重新校验。规范化器兼容已包装引用不代表新提交可以凭空引用文件。",
        },
        "capability_roles": {
            "direct_document_cq": "直接回答原始 CQ：在 document_fact_queries.<name> 添加 sparql、ontology_terms、parameters、business_question_ids、validation_cases、cq_bindings；answer_mode=FACT_QUERY，绑定覆盖全部声明 CQ，结果字段与 SELECT/验收一致。",
            "reasoning_dependency": "仅作为推理证据的 document_fact_query 可不声明 cq_bindings，由 reasoning_capabilities.<name>.evidence_query 引用。推理能力持有自己的 ontology_terms、fact_bindings、rules、result_predicates、runtime_validation。依赖被读取不等于它自己回答了 CQ。",
            "reasoning_cq": "推理回答的 cq_bindings 位于 reasoning_capabilities.<name>，answer_mode=RULE_INFERENCE，引用本能力；derived_predicates 必须是真实规则产出的 result_predicates。CQ 验收参数需与 runtime_validation.parameters 完全一致。",
            "reasoning_cq_graph": "S7 部署验证与实时问答只在“已发布本体 + 本能力 fact_bindings 事实 + 规则结论”上执行规则 CQ，不含 Ontop 实例。cq_sparql 中出现的每个本体类/属性都必须由本能力 fact_bindings（在 ontology_terms 登记 IRI）或 result_predicates 产出；属性事实二元、类事实一元。结构化 evidence_query 应投影实体 IRI 变量（如 ?eq）作为事实主语，使规则结论与数据实例同一身份，不用编码字面值当主语。",
            "binding_location": "不要放 realtime_runtime.cq_bindings 顶层。结构化 CQ 位于 query_capabilities.<query_name>.cq_bindings。",
            "source_refs": "cq_bindings.source_refs 使用 S3 已审映射 ID、映射自身的 source_refs 或已登记规则 ID；它说明业务答案依据，不替代 facts.provenance，不用候选名或自拟文件路径冒充证据。",
        },
        "formal_fact_semantics": {
            "required_class_coverage": "DOCUMENT_FACTS 且 REQUIRE_NONEMPTY 的类，必须有实际消费它的事实生成路径。直接 CQ 查询或消费 evidence_query 的推理能力需声明对应 ontology_terms 与 fact_bindings；孤立事实包、只声明类名均不能生成实例。S3 提前检查生成绑定是否缺失，S6 仍检查真实实例，不得改成 ALLOW_EMPTY 来掩盖来源或映射遗漏。",
            "assessment_scope": "评估分类需要注明适用对象、行程/时间及输入条件。资料中的预计算判断不等于可执行计算；只有真实执行了对应计算，才能声称输入变化后会重新计算。不要把某次快照下的评价默认为对象的永久属性，保持来源事实、计算前提和规则结论的区别。",
            "ontology_terms": "将实际使用的 predicate 映射到正式本体绝对 IRI。直接文档 CQ 必须恰好覆盖所有 fact_bindings 谓词，且绑定不能丢弃输入事实；纯证据查询的映射语义由消费它的推理能力提供。",
            "arity": "属性（OBJECT_PROPERTY/DATA_PROPERTY）必须二元；一元类事实生成 rdf:type。多元 CLASS 事实形成保留参数顺序的正式断言实例，不会自动变成二元关系；应按已审业务模型选择，不要为绕过校验改类型。",
            "datatype": "数据属性对象按冻结 S4 datatype range 生成字面量并校验词法；对象属性指向资源。不得把数字、日期关系都无差别映射为对象属性。",
        },
        "downstream": [
            "S4 从已审 CQ 绑定编译设计，原阶段审批要求保持不变；S3 预检通过不等于业务验收通过。",
            "S6 从校验和匹配的事实包实例化：处理推理能力的 evidence_query，以及带 cq_bindings 的直接文档查询。没有 CQ 也未被推理引用的孤立事实查询不自动产生业务实例。",
            "S7 将已审事实包校验后复制到发布包 05-运行时/document-facts/<name>.json，并绑定校验和。运行问答读取受保护的发布资产，不能靠工作目录里额外文件补证据。",
        ],
        "mode_boundary": (
            "DOCUMENT_ONLY 禁止 mapping_obda、ontop_queries、ontop_deployment_id；保留 current_full_text_search。无推理时，每个文档事实查询必须有已审 FACT_QUERY CQ 绑定。"
            if str(intake_mode or "").upper() == "DOCUMENT_ONLY" else
            "结构化/混合模式沿用真实授权数据库与既有 Ontop 合同；文档事实路线不能替代数据库授权、字段映射或真实查询。"
        ),
    }
