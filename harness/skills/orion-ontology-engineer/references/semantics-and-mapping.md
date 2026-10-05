# S2 业务语义草案与 S3 语义映射评审

仅在工程进入 S2 或 S3 时读取本文件。先读工程契约版本：v2 的 S3 确认语义与候选映射可行性，正式本体/映射/规则/CQ 在 S4 联合定稿；历史 v1 保留原正式 Mapping 边界。

## S2 业务语义识别

首次设计先使用 [语义建模边界与当前工程内复用](semantic-modeling-boundaries.md) 区分领域实体、参与经历、派生类/关系与参数化查询结果；它同时给出同一经历事实绑定、datatype、未知处理以及 S4 术语缺失的修复边界。不要把每个规则谓词或 CQ 命中集合机械升级为核心类。

- `DOCUMENT_ONLY` 使用 S0 证据；`DATABASE_ONLY` 使用 S1 证据；`HYBRID` 必须同时使用两路证据并在 S2 真正汇合。
- 从资料/数据识别候选后，逐条反向检查当前 S0 的全部 CQ：要回答它，还缺什么业务定义、对象身份、关系路径、属性、规则、时间/范围、输出及计数口径？CQ 是能力需求，不自动给出业务规则；普通查询可以不需要派生规则，不能为了补齐表格发明阈值或政策。
- 业务语义、模型覆盖和真实数据可用性分别判断。资料中的类、属性、关系或公理定义可以支持模型候选；没有个体记录不等于没有模型依据。历史汇总数字不等于本轮实例结果，规则用例只用于明确标记的测试，不得写进生产事实来源。
- 每个派生结论继续追溯到基础数据及可执行条件。例如 `ScoreBelow60` 不能只靠同名谓词证明已落地，还须说明成绩字段、比较条件、空值处理、适用范围和来源；规则或缺口关联到受影响的全部 CQ。
- 每个候选包含稳定 `id`、`name`、`kind`、可定位 `source_refs`，以及唯一状态：`DOCUMENT_EVIDENCE`、`DATABASE_FACT`、`AI_INFERENCE` 或 `NEEDS_HUMAN_CONFIRMATION`。
- 先调用 `preflight_stage_submission(stage=S2)`；通过后使用返回的一次性 `preflight_token` 调用 `commit_preflight_stage_submission`，不再重发完整载荷。AI 推测不得伪装成事实，预检失败不得正式提交。
- S2 同步生成 `capability-plan.json`，在进入 S3 前把每个 CQ 路由到平台真实能力，并及早暴露缺失的事实、闭世界完整性或推理算子。
- 在 S2 payload 提交 `cq_semantic_assessments`，每条原始 CQ 恰好一项；采用 `record_semantic_candidates` 的公开 schema，逐项填写业务定义、来源、候选/规则依赖和缺口，不自报 READY 或 VALIDATED。历史工程省略此字段仍可读，但显示 UNASSESSED；不能从旧路由 READY 推断语义检查完成。提交前后的检查使用只读 `get_cq_semantic_review(project_id)`，它读取正式产物，未提交草案不算已评估。
- 资格、合规、异常、风险、缺件、责任、处置等派生判断必须形成 `business_rule_candidates`。每条规则包含真实 CQ ID、`IF ... THEN ...`、前提/结论谓词、来源和 `POSITIVE`/`NEGATIVE`/`BOUNDARY` 测试。
- 有明确属性比较条件的正向规则，在 S2 的 `condition_contract` 绑定该 DATA_PROPERTY 候选及来源表列，声明固定算子/阈值或业务明确允许的参数；S3仅绑定字段，平台生成执行条件。首次使用读取 [业务计划编制](business-query-plans.md)，不要留到 S3 再猜规则条件。
- 不得把判定问题降级为较弱的事实查询。分别记录业务定义、候选覆盖、实例来源和执行算子的缺口；业务定义缺失或冲突时复用 S3 决策卡，仅对影响业务结果且没有可靠依据的问题请负责人裁决。已有同一依据的决定直接复用。数据缺口交回正式来源登记/理解路径，平台算子或字段合同缺口由平台负责，不让业务用户填写内部 JSON。缺口记录不豁免现有生产门禁。
- `HYBRID` 缺任一路真实证据时在 S2 失败，不能留到 S6 才发现。

## S3 候选映射与可行性

数据库/混合来源的受支持查询与正向规则，默认在 `mapping_draft.business_query_plans` 声明业务选择，交 `compile_mapping_runtime` 生成查询与规则；不要同时手写一套 `realtime_runtime`。读取 [业务计划编制](business-query-plans.md) 核对支持范围。先逐项 `start_business_preview` 验证独立答案，保留局部草稿后再正式预检；原有专家合同用于明确超出自动编译范围的能力。

- 每条 Mapping 有唯一 `id`、`source_refs`、稳定英文/ASCII `target`、中文 `target_label_zh` 和 `target_comment_zh`。
- 规范化表使用 `TABLE_TO_CLASS`、`COLUMN_TO_DATA_PROPERTY`、`FK_TO_OBJECT_PROPERTY`。
- 反规范化宽表使用 `COLUMN_VALUE_TO_CLASS`/`SQL_TO_CLASS` 与 `COLUMN_VALUE_TO_OBJECT_PROPERTY`/`SQL_TO_OBJECT_PROPERTY`，明确 `domain`、`range`，不能把业务对象降级成字符串字段。
- 候选 `mapping.obda` 必须预检目标覆盖、IRI 身份、类型与统一 namespace；v2 在 S4 联合设计中最终绑定本体、映射与执行契约。提前预检不能冒充已经正式批准。
- `query_capabilities` 与 `ontop_queries` 一一对应；声明参数类型、中文说明、必填、边界/允许值、示例问法和至少一个真实 `validation_cases`。
- CQ 绑定放在对应 `query_capabilities.<query>.cq_bindings`；有真实规则的 CQ（含纯资料）可放在 `reasoning_capabilities.<capability>.cq_bindings`，并声明同能力的 `business_question_ids`、`parameters`、`result_fields` 和真实行结果 `validation_cases`。规则 CQ 必须绑定自身能力和真实派生谓词，提供 `cq_sparql`、完整业务维度及边界；`runtime_validation` 的事实计数不能代替行结果断言。平台编译 S4，S6 与发布后问答使用同一事实转换和实例标识。顶层 `realtime_runtime.cq_bindings` 不受支持；不要伪造 Ontop 资产或为普通事实查询制造规则。
- 普通文档事实与聚合 CQ 使用 `document_fact_queries.<query>.cq_bindings`，`answer_mode: FACT_QUERY`，同能力声明只读 `sparql`、`ontology_terms`、`parameters`、`business_question_ids` 和真实 `validation_cases`。`fact_bindings` 按原始事实参数的零基 `field` 索引绑定，参数只渲染查询，不改变来源事实。全量物化图的 COUNT DISTINCT/GROUP BY/HAVING 可计算去重与阈值；不为聚合增加派生规则。S4 冻结相同查询及资产指纹，S6、S7 和本体问答实际回读同一完整事实合同。
- 每个工程明确 `reasoning_requirement: REQUIRED|NOT_APPLICABLE`。存在派生判断时必须为 `REQUIRED`，并声明证据查询、事实绑定、稳定规则、结果谓词、规则来源、`FULL_QUERY_RESULT` 范围和示例问法。
- 只有纯检索/聚合才可 `NOT_APPLICABLE`，并写可审计理由。规则作为独立 SHA-256 发布物，不由 Agent 临时编造。
- `HYBRID` 的数据库与文档事实保留各自回执，在统一 `EvidenceBundle` 中汇合；不得用无关数据库行冒充文档语义事实。

## 建模决策评审

1. 有可靠证据或安全默认值的决定进入 `automatic_decisions`；只有继续会产生明显业务错误且无法安全选择时才进入 `confirmations`。
2. v2 的 `confirmations` 最多 3 项；v1 最多 2 项并由平台追加总体确认。一次只展示一张，按本体范围、核心对象身份、关系含义、生命周期、推理规则排序。
3. 每张决策卡必须填齐 `id`、`title`、`business_question`、`evidence`、`confidence`、`options`，不能用 `question` 代替 `business_question` 或省略标题。`evidence.ai_inference` 必填；数据库事实/资料证据用 `{summary, source_refs}`，HYBRID/DATABASE_ONLY 至少一条 `database_facts`，DOCUMENT_ONLY 至少有资料或客户访谈证据。`confidence` 为 0～1 数字。`options` 恰好两项，每项必须有 `id`、`label`、`summary`、`impact`，且恰好一个 `recommended=true`。按实际填写可选 `affected_mapping_ids`、`technical_impact`、`decision_basis`；映射 ID 必须已存在。字段结构以 `prepare_mapping_review` 的 schema 为准，S3 预检 payload 使用相同结构。
4. 先调用 `preflight_stage_submission`，再用返回的 `preflight_token` 调用 `commit_preflight_stage_submission`；载荷里的 `review_policy` 设置为 `AUTO_APPROVE_EVIDENCE_BACKED`。
5. 已有决定只有在证据、选项和 Mapping 更新的指纹完全一致时才自动复用；历史 v1 的总体 Mapping 审批永不复用；v2 不再单独要求总体 Mapping 批准，完整设计由 S4 集中确认。
6. 返回 `BLOCKED_HUMAN` 后先审阅真实决策；没有当前工程的明确代办授权或有效具体选择时停止并展示卡片。已有授权按主 Skill 的代办边界执行，不重复索要授权，也不越过门禁。用户选择现有方案时调用 `resolve_mapping_option`；专家手工改写才用 `resolve_mapping_confirmation`。
7. v2 有真实未决业务卡时，可先提交 `mapping_draft`、`confirmations`、`automatic_decisions`，省略 `realtime_runtime`；仍先预检再消费 token。`validation_scope=BUSINESS_REVIEW_DRAFT_ONLY` 仅说明映射候选与业务卡合格；提交后为 `BLOCKED_HUMAN`，不生成 REVIEWED 映射或运行通过回执。空卡不能使用此分支，不能为显示卡片补造运行配置。
8. 工程页与对话页都读取同一份 `pending-confirmations.json`。v2 最后一个业务决定记录后仍在 S3 RUNNING，`s3_runtime_review.status=AWAITING_RUNTIME_COMPILATION`；按 `get_next_workflow_action` 编译批准口径对应的完整运行设计，保留原业务卡并再次预检、消费 token，平台按依据指纹复用真实决定。若 S2 规则或用例需要修改，使用正式回退流程；卡片明确为 `RETURN_TO_S2` 时，真实选择会直接留痕退回。
9. 只有完整运行设计通过原有只读查询、映射类型、目标覆盖、规则一致性与能力门禁后，平台才生成 `REVIEWED mapping.yaml` 并通过 S3。v2 的 REVIEWED 不替代 S4 整体设计确认；v1 保留原评审顺序。

每条 `automatic_decisions` 必须含 `topic`、`decision`、`reason` 和非空 `source_refs`；建议填写稳定 `id`，省略时平台按内容生成。`affected_mapping_ids` 可省略或为空，有值时必须引用已有映射。自动决定 ID 不能与其他自动决定或确认卡重复。没有自动决定也传 `[]`。这些是模型编译字段，不应让业务人员补内部 JSON；schema 完整不代表来源真实或业务决定已批准。

详细载荷结构见 [tool-contracts.md](tool-contracts.md)。

## 工具与协作闭环

- S2 的实际 Workflow MCP 是 `record_semantic_candidates`；正常提交走 `preflight_stage_submission(stage="S2", payload=...)` → `commit_preflight_stage_submission`。平台生成能力计划、报告与门禁，模型提供有来源的业务候选和规则草案。
- S3 的实际 Workflow MCP 是 `prepare_mapping_review`，提交同样先预检 S3 再消费 token。证据充分使用 `AUTO_APPROVE_EVIDENCE_BACKED`。真实歧义由负责人选择后调用 `resolve_mapping_option`；只有专家明确调整映射字段时使用 `resolve_mapping_confirmation`。
- 同一来源需要核对编码/字段分布时，S2–S7 可调用 `query_source_evidence(project_id, expected_revision, table, group_by, equals?, max_groups?)`：表名和列来自 S1 Schema；current 视图名与其 physical_version_table 都映射到同一已登记物理版本。支持文件导入 READY 数据集，以及经平台快照中心（ORION_SNAPSHOT_HUB）登记的数据库快照表；快照表可用 S1 物理表名（ms_…）、来源表名（如 plant）或 source_id.来源表名，平台会固定到 S1 的 dataset 版本并核对快照集 manifest 哈希。该输入模式做多列分组计数和等值过滤，不接 SQL，不用于未绑定版本；dataset_id/row_ordinal/row_sha256 等系统列不可分组。完整计数看 `matched_row_count`、`total_group_count`；`truncated=true` 表示返回的 groups 不是全部分组。保留回执的来源/版本/查询哈希，不把补查或数值分布当作业务定义，也不凭此修改 S1。该只读入口不需要 preflight token，不为 SELECT 重开 S1。通用 Chat2DB 在本阶段仍受限，不能据此扩大表范围。
- 数据库快照新增两种互斥模式：`query_source_evidence(..., describe_table="来源表名")` 回读真实业务列、快照存储类型和版本身份，先读结构再编制，不能猜列名；快照的 text 存储类型不能直接代替业务 datatype。`query_source_evidence(..., query_plan={...})` 用公开 schema 中的 sources/from/joins/select/filters/group_by/having/order_by/limit 描述受控跨表计划，支持最多四表等值连接、时间与数值比较、大小写归一和分组聚合。每表固定 S1 版本；完整结果看 total_row_count、返回行看 rows/truncated，不能把截断结果当全量。不支持的算子明确保留缺口，不传任意 SQL。结构回读、分组计数、query_plan 三种输入不能混用。
- S3 草稿先 get_stage_draft 恢复；无草稿时 generate_mapping_skeleton。依据 get_stage_input_contract 明确 derivation.from_snapshot_table/from_snapshot_column/identity_columns，普通映射交 compile_mapping_runtime 生成 OBDA；新增字段再次调用会增量追加并保留已有运行设计，禁止删 runtime 重建。复杂映射由代理按真实快照合同补齐；长文本用 patch_stage_submission 的 replace_text、value={old:唯一非空原文,new:替换文本} 局部修改。正式评审支持最新 payload_file，无需重传整份映射。
- v2 存在未决业务卡而运行设计尚未完成时，文件评审使用 prepare_mapping_review(payload_file=最新引用, expected_revision=当前修订, review_scope="BUSINESS_ONLY")。平台只评审业务卡，保留运行草稿；不得删除 runtime 来让门禁通过。确认后回读新修订及 draft_checkpoint，按批准口径更新运行设计，再使用默认 FULL 完整预检。业务卡登记和决定均不等于 S3 通过；退回 S2 后旧 S3 草稿不得继续使用。
- 查询参数按 execution_semantics 用 {{parameter_name}} 渲染，不能猜成同名 SPARQL 变量注入。明确相对时间的基准、时区与边界，历史验收窗口不作为日常默认日期。验收首行使用确定排序，并列时追加业务键；小数按来源精度处理，必要时用 APPROX 与显式容差，不能把浮点截断当精确值。
- 查询自查必须覆盖：OR 分支依赖的可空字段是否被强制三元组提前过滤；未知是否被误当成不合格/零；连接是否丢掉需要保留的业务记录；零值或无效记录是否混入有效业务统计；重复来源是否改变去重对象。可空输出按 nullable_bindings 合同声明条件与真实原因，不造占位值掩盖来源缺失。边界断言表达业务约束；不能因为本次快照恰好只有某一分支，就写一条排除另一合法分支的 ALL 断言。
- `fact_bindings[].when.missing` 只能消费真实查找后的缺值：证据查询应在同一业务主体上用 OPTIONAL 读取可空字段。UNION 的某个分支没有读取该字段，不能作为“来源字段缺失”的证据；即使另一个分支已取到值，也会误生缺失事实。先核对每条规则的正例、反例、缺值与来源总量，再冻结 CQ 契约，不依据错误推理结果修改验收期望。
- 本阶段优先消费 S0/S1 已保存的真实资料、数据快照和 SQL 回执；不在 S2/S3 擅自扩大源库范围、重做 OCR 或调用 Protégé 改模型。缺来源时由工作流确定回到哪个责任阶段。
- 前端和模型只解释当前决策卡，不自动替用户选择高影响选项。外部事实的读取身份、只读查询及映射编译由平台能力提供；新增执行器必须以真实工具与能力声明为依据。
- 提交响应中已有当前正式状态时直接核对 revision、产物与下一动作；缺失或陈旧才调用 `get_ontology_workflow_status` / `get_next_workflow_action`。S3 通过不意味着 S4 已批准或运行查询已可正式使用。
- 已定位为缺来源、缺业务定义或不支持的算子时，保存当前草案、受影响 CQ、证据和一个正式恢复动作；没有新来源、新决定或可验证的平台能力变化，不重复原样预检、不读取源码猜字段、不重启同一失败循环。需要修改已通过的候选或来源时先预览正式回退影响，不直接改历史产物。
