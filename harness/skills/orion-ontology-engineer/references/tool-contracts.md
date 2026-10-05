# ORION S0～S7 工具与提交契约

只在准备当前阶段载荷或处理门禁时读取。实际挂载工具的 schema 是字段、枚举与必填项的唯一依据，本文件用于说明调用关系。不得让业务用户填工具 JSON，不得把示例值当作真实证据。

## 版本与分工

优先消费已绑定工程的当前正式响应；状态缺失或陈旧时调用 `get_ontology_workflow_status(project_id)`，仅在最新响应缺少 `next_action` 时补读 `get_next_workflow_action(project_id)`，不重复查询已有的有效状态。状态里的 `stage_contract_version`、`stage_contracts` 与当前阶段工具元数据共同决定可执行动作；新 v2 为 `s0-s7-stage-contract-v2`，历史缺字段或 v1 保留原职责。

模型按照 Skill 读取需求与证据、提出有出处的业务语义、准备允许的工具输入、解释失败。Workflow MCP 编译设计、预检、存储产物、推进阶段、维护审计；Chat2DB、文档解析、Protégé、HermiT、SHACL、Ontop、Fuseki、Semantica 提供各自真实能力与回执。外部工具的确切名称以当前工具目录为准，不能凭记忆构造工具调用。

| 阶段 | 真实 Workflow MCP 入口 | 关键边界 |
| --- | --- | --- |
| S0 | `snapshot_workspace_sources`, `preflight_workspace_snapshot`, `create_ontology_project`, `start_document_ingestion_job`, `get_document_ingestion_job`, `commit_document_ingestion_job`, `record_s0_scope_decision` | 目标与统一来源登记；既有文档队列仍在 S0 执行；S0 没有通用 preflight token |
| S1 | `record_document_understanding`（v2 文档 S1 重验）、`record_data_understanding_from_datasets`，或预检 `record_data_understanding` 对应载荷 | v2 文档理解结果由平台从 S0 证据生成；数据画像只读并核对全量范围 |
| S2 | `record_semantic_candidates` 对应载荷，`query_source_evidence` 只读补查 | 业务语义和规则草案有真实 source_refs，事实与推断分级 |
| S3 | `prepare_mapping_review`, `resolve_mapping_option`, `resolve_mapping_confirmation`, `query_source_evidence` | v2 确认口径与候选可行性；只为高影响歧义阻塞 |
| S4 | `generate_ontology_design`, `prepare_ontology_design_review`, `resolve_competency_question_review` | v2 整体批准本体、正式映射、规则和 CQ；不能自动批准 |
| S5 | `record_ontology_build` 对应载荷 | 真实构建、导出和 HermiT/SHACL 回执；资产覆盖已确认设计 |
| S6 | `record_quality_validation` 对应载荷 | 真实来源与规则执行、全量覆盖、CQ 语义和边界验证 |
| S7 | `publish_ontology_package`, `defer_ontology_publication`, `resume_ontology_publication` | 明确发布授权，打包与在线就绪分别判断；S7 没有通用 preflight token |

S1～S6 默认不直接重发各 `record_*` 载荷，而使用下一节的预检快照提交。`record_ontology_design` 是内部落库口，不是对话 Agent 的工具。

## 预检 → 一次性提交 → 状态回读

1. 回读当前 `project_id`、`revision`、`current_stage` 与推荐动作。
2. 调用 `preflight_stage_submission`，stage 仅允许 S1～S6。`payload` 和 `payload_file` 二选一，不删减业务内容来缩短载荷。
3. 大载荷完整保存到当前工程 `.submission-drafts/<file_name>.json`，传入 `payload_file={file_name,sha256}`。这是受控草稿，不是直接修改正式资产。
4. 只有 `PASSED` 后，调用 `commit_preflight_stage_submission(project_id, stage, preflight_token, expected_revision)`。不再重发完整 OWL、TTL、SHACL 或画像。
5. token 绑定工程、阶段、revision、内容哈希与有效期；正式操作只执行一次。提交响应丢失时，使用原 token 和原 expected_revision 重试以恢复同一持久回执，不会重复执行阶段；不得改用最新 revision 或另造载荷。真正变更输入或提交新的操作时重新预检；返回需对账／部分写入时停止盲重试，按正式恢复要求处理。
6. 正式调用超时先回读状态与事件，避免重复写入。失败先修正当前责任阶段输入；通过预检后才使用 `retry_failed_stage`，重试改变 revision 后重新签发 token。
7. 回读状态、当前资产和下一允许动作，核对真实工具回执、manifest 与追加审计。预检成功不等于阶段完成；S4 交审不等于设计批准。

## S0 目标与来源

文件来自用户当前消息明确 `@` 的引用。`snapshot_workspace_sources` 接收工作区内相对路径，拒绝绝对路径、`..` 与越界链接；后续只使用它返回的受控 `source_path`。`preflight_workspace_snapshot` 给出解析与结构化导入路线，再调用一次幂等 `create_ontology_project`。

浏览器上传使用同一受控入口返回的 `UPLOAD` 批次；工作区引用使用 `REFERENCE` 批次。平台验证真实文件清单与内容回执后返回 `source_snapshot`，已有工程在入队前还必须通过来源范围和当前修订校验。该快照仅证明用户选定的资料，不能扩大已有工程授权；模型不得自填或改写其指纹、文件清单。队列执行前再次校验同一清单。

新建时填写业务目标、CQ 和 schema 支持的 `source_scope`。资料、单库、多库、混合都使用同一来源登记；多个数据库仍是 `DATABASE_ONLY`，没有 `MULTI_DATABASE` 枚举。来源 ID、表/列范围、快照、时间和完整性只使用已确认信息，不推断跨库授权。用户已明确范围不再索要重复批准。

资料队列仍按真实工具执行：

- `start_document_ingestion_job(project_id, source_path, expected_revision, actor)` 启动已有批处理。
- `get_document_ingestion_job(project_id, job_id)` 查询同一任务。
- `commit_document_ingestion_job` 只提交真实 `READY_FOR_REVIEW` 结果。复核人和理由真实，大表需要完整行数据时选择 schema 的 `structured_data_action: IMPORT`。
- 截断、失败、低置信度或范围警告先调查，不默认接受。S0 已提交但导入中断时恢复同一任务，不重做有效解析。
- `record_document_evidence` 只记录已完成真实处理的证据，不负责读取任意本地文件。
- `record_s0_scope_decision` 当前 `intake_mode` 只允许 `DATABASE_ONLY`，传真实 `rationale`、`decided_by` 和 `datasource_refs`。不把它当作任意来源注册工具。

`create_ontology_project.source_scope` 的实际输入是 `{sources, excluded_source_refs?}`。sources 每项的 kind 仅为 DOCUMENT、STRUCTURED_FILE、DATABASE。文件来源以已有 source_id / 受控 source_path / source_sha256 至少一项标识，目录使用 path_scope=DIRECTORY；数据库以已发现 source_id / datasource_label / database 至少一项标识。数据库或结构化文件可以声明 authorized_tables、authorized_columns、excluded_tables、excluded_columns，数据库另有 schemas。空 authorized_tables 不代表授权全部表。模型从现有授权与目录生成这些字段，不要求业务用户手写，也不凭空生成来源 ID。

模型只使用受控工具，不直接调用需要浏览器身份的 `/orion-document-api/jobs` HTTP，也不读取凭据或重启 3081。平台内部路由与模型可用入口是两个边界。

v2 S0 完成后，纯资料工程由平台自动生成 S1 `source-understanding.json` 和报告；只有数据库子任务不适用。历史 v1 仍保留 S0 纯库范围不适用和 S1 纯资料不适用的原判定。

## S1 来源理解

- 文档：保存资料身份、定位、结构、质量、覆盖与冲突。v2 消费 S0 已形成的真实解析结果，不调用 Chat2DB 替代文档理解。
- 数据：已登记 READY 数据集可预检 `payload={dataset_ids:[...]}`，平台重建回执。`record_data_understanding_from_datasets` 也提供自动预检路径。
- 外部库：只读 SourceBinding 与授权范围明确后，平台建立 SnapshotHub 当前快照。Chat2DB 发现连接或读到样例，不代表完成全量理解。
- 生产画像核对 `FULL_IMPORT_WITH_EXACT_COUNTS`、范围表、Schema、逐表行数、总表/总行数、空表数、当前 dataset 身份和来源 SHA-256。
- 每条 SQL 回执来自本次真实 SELECT/WITH，含执行方式、时间、结果哈希、预期/实际行数与来源表。模型不能自行填写“PASSED”以代替执行。

v2 DOCUMENT_ONLY 的 S1 重开时，使用平台推荐的 `record_document_understanding(project_id, expected_revision)` 重新核验当前已保存的资料理解。正常 S0 后平台自动完成该步，无须额外模型提交。该入口直接执行确定性校验与回读，不伪造数据画像载荷或通用 token。

## 修订中的 CQ 变更

在已有工程新增或修改 CQ 时，先核对有效 cq-intake 与用户明确需求，再编制 S2 载荷。当前 S0/S1 已通过且 S2 尚未正式提交时，调用 `amend_competency_questions`，传完整 questions、当前 expected_revision、真实 actor 与 reason。保留未变问题的 ID 和口径；只纳入用户授权变更，不带评测标准名单。平台留存旧需求快照并更新指纹、计数和审计，原发布不变。更晚阶段需先按正式路径重开 S2，不直接改 cq-intake.json，不为绕过门禁删掉新增题。修订后的全部有效 CQ 必须进入 S2 与后续验收。

## S2 业务语义草案

同一工程在 S2–S7 需要补查已登记文件导入数据时，使用 `query_source_evidence`，传当前 revision、S1 Schema 的表名/列、`group_by` 与可选 `equals`、`max_groups`。平台核对 S1 指纹、来源范围和 READY 目录；current 别名固定解析到 S1 的物理版本。SQL 由平台生成，只用专用只读连接；此调用不需要提交 token、不改变阶段、不重开 S1。完整匹配行数和分组数与返回组数分开，`truncated` 为真时不能宣称 groups 覆盖全部值。支持已导入文件版本和快照中心登记的数据库快照表（表名可用 S1 物理名或来源表名），不开放通用 Chat2DB、未登记外部库和任意 SQL。查询结果只作新候选的来源证据，不能自行决定冲突口径或替代 S6 验收。

每个候选有稳定 ID、业务名称、类型、`source_refs` 和来源状态：`DOCUMENT_EVIDENCE`、`DATABASE_FACT`、`AI_INFERENCE` 或 `NEEDS_HUMAN_CONFIRMATION`。混合工程需要两路证据。

新增 S2 提交使用 `cq_semantic_assessments` 逐题补全；此数组与 `ontology_candidates`、`business_rule_candidates` 同放在预检 payload。每条 S0 原始 CQ 恰好出现一次，不能删题、重复或自行重编号。每项字段：

| 字段 | 含义 |
| --- | --- |
| `question_id` | 当前 S0 原始 CQ ID |
| `answer_kind` | `FACT_LOOKUP`、`AGGREGATION`、`RELATION`、`RULE_INFERENCE` 或 `DOCUMENT_EVIDENCE` |
| `business_definition`、`definition_source_refs` | 作答需要的业务定义、时间/范围、输出口径及已登记证据；定义为空必须由 `missing_semantics` 解释 |
| `requires_business_confirmation` | 是否仍有影响结果且不能由现有依据解决的业务歧义 |
| `required_candidate_ids`、`required_rule_ids` | 本次候选/规则的真实 ID；普通查询无需强行编造规则 |
| `missing_semantics` | 未确定的业务定义、口径或条件；无缺口传 `[]` |
| `required_fact_descriptions`、`missing_data` | 需要哪些真实事实、当前缺少哪些来源或字段；分别传字符串数组 |
| `premise_bindings`（可选） | 每项为 `{rule_id, predicate, source_refs}`，说明规则外部前提的已登记依据，不能用定义文档冒充个体观测 |

除 `premise_bindings` 外以上字段均必填。列表省略仅为历史兼容，不能视为评估完成；不传 `READY`、`VALIDATED` 等状态。平台检查 CQ、候选、规则和证据引用，按各维度计算状态。`get_cq_semantic_review(project_id)` 只读返回正式产物的逐 CQ 业务、模型、数据与验证状态、缺失规则/前提和下一步；它不会审批或推进阶段。建议的 S3 业务卡须经现有 `prepare_mapping_review` 预检和真实决定流程落账。历史 `UNASSESSED` 项先检查，若需补写已通过的 S2 使用正式修订路径，不直接覆盖产物。

CQ 只定义要回答什么，不自动决定规则。逐题检查候选后继续追溯到基础属性、关系路径、时间参数、聚合/去重范围、空值和边界。已有同依据决定复用；没有可靠来源的阈值保留待确认。模型覆盖不等于实例齐全，资料定义与历史总数也不证明本轮真实结果；规则测试永不提升为生产事实。明确缺口后保存恢复动作，没有来源、决定或平台能力变化时不重复预检或读源码猜字段。

派生判断形成 `business_rule_candidates`，包含来自当前 CQ intake 的 `business_question_ids`、形式化条件、前提/结论谓词、来源与 POSITIVE/NEGATIVE/BOUNDARY 用例。平台先检查规则形状与具体事实的参数匹配，能力计划提前暴露缺口。材料目录不等于某申请人的提交全集，`NOT` 判断必须有封闭世界范围和完整性；不得改弱问题来让规则通过。

规则字段使用以下现有合同，不猜格式：

- 谓词名使用稳定 ASCII 标识，匹配 `[A-Za-z_][A-Za-z0-9_:-]{0,127}`，区分大小写。中文业务含义保留在 `name`、`description` 和来源说明；当前不支持中文谓词名。这一限制不要求改写中文参数值或包含连字符的业务编码。
- `formal_expression` 填完整的单结论 Horn 规则，例如 `IF ParameterObserved(?x) AND ThresholdExceeded(?x) THEN NeedsReview(?x)`。参数写在原子括号内；具体变量、阈值和绑定必须保持来源语义，不从这个格式示例推断业务条件。
- `premise_predicates` 只填名称，如 `["ParameterObserved", "ThresholdExceeded"]`；`conclusion_predicate` 只填 `"NeedsReview"`。不要填 `"NeedsReview(?x)"`，也不要漏掉正文中的匹配或 NOT 谓词。
- 自动比较规则在 S2 的 `condition_contract` 一次声明各前提对应的 DATA_PROPERTY 候选、比较算子与固定阈值，或业务明确允许的参数及范围；S3只绑定字段并由平台生成条件。缺少来源定义先补齐业务语义，不能将固定条件偷偷改为可变参数。详见 `business-query-plans.md`。
- `test_cases[].facts` 每项是一个完整原子，如 `ParameterObserved(sample_1,SC-JYH999,1.02)`，不能只填名称、自然语言或嵌套原子。用例是有明确假设的候选逻辑场景，不是生产观测或验收证据。
- POSITIVE 期望 FIRE，NEGATIVE 期望 NO_FIRE，BOUNDARY 根据真实边界选择。S2 按下述参数匹配语义核对前提与绑定；NOT 仅在当前有界场景范围内判定，不是全局禁止出现同名谓词。缺少前提时先检查场景设计、事实依据和期望，不能为通过门禁随便补造“已达标”等事实。

S2 使用与运行链路共用的参数匹配函数检查单条 Horn 场景：?变量绑定、常量值、跨前提同一记录关联和有界测试快照的 NOT。裸参数名是常量，反例可以保留完整编码字典和其他记录的事实。S2 **不执行数值比较、窗口密度、聚类或其他业务算法**，参数匹配通过不代表这些算子正确或规则已经生产执行。具体数据绑定和算子可行性由 S3 能力编译核对，在 S4 联合设计中确认，并由 S6 真实执行与全量/边界证据验收。`NOT` 仍须满足当前闭世界能力、完整快照、required 集合和来源绑定合同；此候选场景检查不扩展生产执行器能力。

预检的每项问题包含 `path`、`reason_code` 和 `details`：优先按精确字段修复。同一响应会合并中文谓词、整原子误填、测试原子和参数绑定问题。不得因格式失败把数值条件替换为结果枚举、重命名业务常量，或自行改变事实含义。

## S3 语义与候选映射

完整结果验收可使用 `expected_row_fields` + `expected_rows`，同时用于查询样例和正式CQ。按无序多重集合核对所声明字段的完整行元组，额外行、错误关系配对、重复次数不符都失败；显式空数组要求空集。没有声明不能默认空集，不能从实际结果倒填答案；已有首行与边界断言仍执行。详见 `business-query-plans.md` 的独立期望合同。

先按当前 `intake_mode` 读取 `get_stage_input_contract` 的 S3 合同。`DOCUMENT_ONLY` 使用有来源的文档事实与文档查询能力，禁止 Ontop 字段；`DATABASE_ONLY` / `HYBRID` 的结构化一路才使用下述 OBDA 合同。推理能力仅在业务问题需要派生判断时声明，普通已有事实查询不要求人为增加规则。

`realtime_runtime` 使用 `prepare_mapping_review` 展开的正式输入 schema。结构化一路的 `mapping_obda` 是包含 `[PrefixDeclaration]`、`[MappingDeclaration]` 和 `source SELECT` 的完整 Ontop OBDA 文本；不要把 R2RML/Turtle 塞进此字段，也不传路径/制品描述对象。`ontop_queries` 为查询名到 SPARQL SELECT 字符串的对象；`query_capabilities` 的键须完全一致，每项写 `description_zh`、`parameters` 对象（无参 `{}`）、`result_fields` 字符串数组、`question_examples` 字符串数组及 `validation_cases` 对象数组。用例字段为 `id`、`question`、`parameters`、`expected_fields`、`min_rows`、`expected_first_row`；统计值写在真实首行对应字段，不能把人数当返回行数。参数合同 type/description_zh 与真实模板绑定一致。 OBDA 只使用一个 MappingDeclaration collection 容纳所有 mappingId/target/source，target 三元组以句点结束；reasoning_capabilities 为能力名到能力对象的字典，不是数组。声明 FULL_QUERY_RESULT 的 evidence_query 不得用 LIMIT/OFFSET 截断样例代替已审范围的完整事实。不得用测试例子或历史数字填生产断言。

S3 预检失败若返回 `repair_contract`，直接使用其 `realtime_runtime` schema 修复 `payload_file` 草稿；原 gate、来源和全部 CQ 保留，不再查源码逐个猜字段。DOCUMENT_ONLY 禁止 Ontop 字段；DATABASE_ONLY 无文档能力传 `document_query_capabilities:[]`。 query_capabilities.source_ids 指 Source Service 绑定 ID（`^[a-z][a-z0-9_-]{2,63}$`），不是 dataset/DS-/证据/table: 标识。纯文件 SNAPSHOT_ONLY 未绑定 Source Service 时传 source_ids=[]、source_tables_by_id={}、source_columns_by_id={}，真实表列仍保留在 source_tables/source_columns 和映射中，禁止编来源 ID。REQUIRED 推理能力仍须满足正式规则/事实/结果与运行校验，不能改 NOT_APPLICABLE 逃避数据或算子缺口。

规范化表可使用 TABLE_TO_CLASS / COLUMN_TO_DATA_PROPERTY / FK_TO_OBJECT_PROPERTY；宽表可用当前 schema 支持的 COLUMN_VALUE_TO_CLASS / SQL_TO_CLASS 与关系映射；文档用 EVIDENCE_TO_CLASS / EVIDENCE_TO_OBJECT_PROPERTY / EVIDENCE_TO_DATA_PROPERTY。每条映射有身份、目标、中文业务解释、定位来源，关系明确两端，属性明确主体与类型。

结构化一路的候选 OBDA 预检查询只读性、IRI 身份、类型、目标覆盖与 namespace。规则绑定来自 S2，能力声明 FULL_QUERY_RESULT 和真实事实来源；文档与数据库不能相互伪造。v2 的 S3 REVIEWED 说明口径与候选可行性已审，正式设计在 S4 联合冻结。

证据充分记录 `automatic_decisions`；只有高影响歧义进入 `confirmations`，v2 最多三项、v1 最多两项并追加总体确认，一次展示一张。`prepare_mapping_review` 即使无自动决定也传空数组，正常使用 AUTO_APPROVE_EVIDENCE_BACKED。用户选择真实选项后用 `resolve_mapping_option`；专家调整才用 `resolve_mapping_confirmation`，必须有 selected_option_id 与明确受影响映射。决定和证据指纹不一致不能复用。

`prepare_mapping_review` 与 `preflight_stage_submission(stage=S3, payload=...)` 使用同一业务载荷。模型须一次填齐以下字段，不把 schema 缺字段转成用户确认问题：

| 对象 | 必填字段 | 附加约束与可选字段 |
| --- | --- | --- |
| `confirmations[]` | `id`, `title`, `business_question`, `evidence`, `confidence`, `options` | 卡 ID 唯一，不用保留 ID `S3-OVERALL-MAPPING-REVIEW`；置信度为 0～1 数字。可选 `affected_mapping_ids`、`technical_impact`、`decision_basis`。 |
| `evidence` | `ai_inference`，以及当前来源模式要求的证据 | HYBRID/DATABASE_ONLY 的 `database_facts` 至少一项；DOCUMENT_ONLY 的 `business_materials` 或 `customer_interviews` 至少一路非空。数据库事实、资料事实逐项含 `summary` 和非空 `source_refs`；访谈保留可追溯的既有字符串或结构化记录。 |
| `options[]` | `id`, `label`, `summary`, `impact` | 恰好两项，ID 不同，恰好一个 `recommended=true`。可选 `mapping_updates` 数组，每项 `id` 引用已有映射；`action` 默认 `APPLY`，需要修改 S2 候选时可提供 `RETURN_TO_S2`。推荐不是审批。 |
| `automatic_decisions[]` | `topic`, `decision`, `reason`, 非空 `source_refs` | 建议填稳定 `id`，省略时平台按内容生成；ID 在自动决定中唯一且不与确认卡冲突。`affected_mapping_ids` 可省略或为空，有值时必须存在；没有自动决定传 `[]`。 |

`title` 与 `business_question` 都要提供；只给 `question` 不符合输入契约。卡片已回读的 `status`、`recommended_option_id`、依据指纹和决定元数据可保留供完整运行设计二次预检，不能自行写成已批准以代替正式 resolver。跨项唯一性、真实来源、推荐数量、映射 ID 和批准依据仍由现有门禁验证。

v2 的提交顺序：有真实未决高影响业务卡时，允许先省略 `realtime_runtime`，提交映射候选、完整来源卡和选项。`preflight_stage_submission(stage=S3)` 返回 `validation_scope=BUSINESS_REVIEW_DRAFT_ONLY`、`runtime_validated=false` 后消费 token，只保存评审草案并 BLOCKED_HUMAN，不能称 S3 或运行验证通过。空卡、空运行对象不能借此跳过门禁。没有待确认卡时仍须提交完整运行设计。

v2 最后一个决定后回读 S3 RUNNING / `s3_runtime_review.status=AWAITING_RUNTIME_COMPILATION`，按 `COMPILE_RUNTIME_FROM_BUSINESS_DECISIONS` 准备完整运行设计并保留原业务卡，再预检和消费 token；平台复用指纹一致的已确认决定。若批准口径要求修改 S2 规则或测试，先 `preview_stage_rollback` → `reopen_stage_for_correction` 回到 S2，或执行卡中真实选择的 `RETURN_TO_S2`。S3 resolver 只更新映射，不能修改 S2 规则。最终 S3 通过仍要求全部原有运行、类型、能力及规则一致性门禁；缺资料或参数时停下来澄清，不编造默认值。v1 保持原有完整运行设计先行的顺序。


参数替换返回完整 RDF 词项：string/code 等字符串参数会包含引号，iri 参数会包含尖括号。不要将 `{{cell_code}}` 直接拼进 `<.../{{cell_code}}>`；字符串拼成 IRI 时使用 `IRI(CONCAT("已审命名空间", {{cell_code}}))`，或者声明 iri 参数并直接写 `{{cell_iri}}`。必传且无默认值的参数声明 `required=true`，每个真实 validation_case 提供值；不要为通过空参数预检硬编码某个实例。

### S3 提交前的 CQ 执行契约

支持范围内优先使用 `mapping_draft.business_query_plans` 编制，按当前 `get_stage_input_contract(S3)` 取得结构；每次 patch 一项计划，再调用 `compile_mapping_runtime`。模型负责业务对象、关系、条件、S2规则引用和有来源的独立验收期望，平台生成 SPARQL、事实绑定、规则结论查询及重复执行字段。具体约束见 [业务计划编制](business-query-plans.md)。超范围返回能力缺口，不通过删 CQ 或改业务定义来适配编译器。

受管生成内容从业务计划修改后重新编译；不要直接改生成的查询或事实绑定，否则下次编译会报告冲突并保留原内容。历史无清单运行设计沿用保守增量。S3 预检会核对业务计划与实际执行内容，过期结果不能进入正式审批。编译只保存检查点，仍需业务审批和真实 S6/S7 验收；首版计划为 `SNAPSHOT_ONLY`，不能声称实时数据库查询。

完整运行设计应在 S3 草稿中一次准备全部原始 CQ 的执行绑定：对应能力、真实验证用例、查询返回字段、业务输出维度、结果及边界断言。`query_capabilities`、`document_fact_queries` 与 `reasoning_capabilities` 均按实际执行类型填写 `cq_bindings`；同一 CQ 不得被多个能力重复认领。事实供应能力不应仅因提供输入就认领最终业务问题。

关系题的维度须表达业务要求中的主体与关联对象，并按实际问题补足触发条件等维度；不能为达到数量凑字段。维度和断言引用实际 SELECT 返回变量，关联路径、术语及证据应能回溯。结果断言来自授权来源和真实验证依据，不能编造候选人名单或从评测答案反推。

S3 预检若指出 CQ 编译或输出契约缺项，保留当前草稿；业务计划生成的能力修改对应计划再编译，历史手工能力在 `realtime_runtime` 对应绑定中局部修补，再预检提交。不能先让 S3 通过、再在 S4 临时补造另一套契约。S4 仍独立校验冻结后的完整设计、公理与审批，S3 预检不替代 S4 审批和 S6 实际运行验收。

## S4 联合设计整体确认

默认 `generate_ontology_design` 从工程现有目标、证据、候选映射和能力编译。必要专家导入用 `prepare_ontology_design_review`；禁止模型自己创造无 S0 来源的 CQ 或让用户填 SPARQL binding。

当前生产公理门禁默认要求非空且有来源的合法公理；仅 v2 DATABASE_ONLY 全部原始 CQ 已正式证明为完整结构化事实查询、无规则或 OWL 需求时，服务端可计算并冻结空公理适用性，S5 继续核验同一证据。无依据时报告设计/平台适配缺口，不发明公理、不编“不适用”绕过，也不转成技术字段审批。正常先预检 `stage="S4", payload={"generation_request":{"logical_axioms":[有来源公理],"review_policy":"HUMAN_REQUIRED"}}` 并携带当前 `expected_revision`，通过后提交 token；上述事实路径确无公理时可传空数组让平台判定，但不能修改 CQ 类型以获得通过。不要空载荷正式生成、失败后继续猜字段。公理支持四类：SUBCLASS_OF 必须 child/parent；DISJOINT_WITH 必须 class/other；EQUIVALENT_DATA_HAS_VALUE 必须 class/base_class/property/value；EQUIVALENT_OBJECT_SOME_VALUES_FROM 必须 class/base_class/property/filler。共同必填 id/axiom_type/source_refs，IRI 必须已声明且关系有真实业务依据。

S3 `query_capabilities.<查询名>.cq_bindings` 按原 S0 问题 id 登记：必填 `validation_case_id`、`answer_mode`、中文 `answer_scope_zh`、`source_refs`、有据 `boundary_assertions`，关系题另填现有 `required_business_dimensions`。平台继承所选 validation_cases 的参数、min_rows、expected_first_row，后者只编译 FIRST EQ，绝不冒称精确总量或完整集合。边界断言必须针对同一用例查询，不能借用另一参数用例的结果。

`required_business_dimensions[].path` 当前合同是查询中实际引用且本体已声明的**单个属性完整 IRI**（对象或数据属性），不是局部名、类前缀、点号链或 SPARQL `/` 属性链。多跳关系由 SPARQL 完整表达；此字段引用支撑该维度的实际属性，不声称单个 IRI 已验证整条关联。不得靠更换维度名称绕过要求。`evidence_refs` 引用已审映射 id 或映射来源；回答范围的 `source_refs` 还可引用正式规则 id。原文证据存在不等于已纳入该映射或规则：未绑定时补有依据的映射来源，不随意换成其他证据号。

有明确来源的“无法计算”仍保留 NULL 和业务状态，不填 0。可在 S3 同一 CQ 绑定中声明 `nullable_bindings:{输出字段:{when:{其他必填输出字段:具体值},reason_zh:中文原因,source_refs:[实际来源]}}`；例如零跨度时密度缺失，条件由真实 UNKNOWN 状态限定。when 是同一行其他非空必填字段的等值 AND，不允许空条件、自引用、可空条件字段或通配规则。平台编译并回读此已审约定，S6 只对匹配行免该字段的非空要求，记录条件空值计数；返回列、其他必填字段、行数、结果/边界断言仍严格验证。不能在 S4 临时加可空标志绕过 S3。

已有判定检索、统计与追溯可明确 FACT_QUERY/EVIDENCE_QUERY；显式规则推理不得降级。RULE_INFERENCE/OWL_INFERENCE 另须 `reasoning_capability`、`derived_predicates`、`cq_sparql`：此查询在 S6 真实事实与派生图上执行，实际关联规则结论，原 Ontop 查询仍只提供事实。S6 全量规则回执、正反例与轨迹门禁不变。平台回读 S3 绑定、用例和查询哈希，拒绝在 S4 偷改 query 或答案契约；修改业务口径必须重新审阅 S3。

设计包括业务类/属性/关系、公理、数据约束、IRI/版本、正式映射、规则、查询能力与 CQ。SELECT CQ 有业务维度、正确结果和边界断言；推理 CQ 绑定正式规则与结果谓词。每项都回溯来源。

v2 使用 HUMAN_REQUIRED，平台生成 `review_scope: JOINT_DESIGN`、`joint_design_summary` 与 `joint_design_fingerprint`。负责人审阅的是本体、映射、规则、CQ 整套设计，不能由自动 CQ 策略跳过。

提交 token 优先携带 `response_mode="SUMMARY"`，完整回执可保留默认 FULL。精简回执的 `review_ref` 提供正式审阅文件、文件 SHA、问题 SHA、当前 revision 与联合设计指纹；按引用审阅整套设计。通过审批后，`questions` 可仅含每条原始 `id`、`question`、`expected`，严格保留原序和原文；直接使用 `approval_questions`，平台从同一正式草案复用完整查询与答案契约。不得为了简短改写问句、预期、顺序，或用摘要代替实际审批。

审批仍调用 `resolve_competency_question_review`，真实字段为 `project_id`、`questions`、`decision`、`decided_by`、`rationale`、`expected_revision`。decision 使用 APPROVED 或 RETURN_TO_S3，不自造 APPROVE/REJECT 枚举。修改后的设计必须经过平台重编译/校准；回读整体设计凭证与阶段后才进入 S5。历史 v1 按其原 CQ 策略。

## S5 构建与装配

先回读明确绑定工程的 status/next_action；当前可执行 S5 时，在实际代码仓库根优先运行 `make ontology-s5 ONTOLOGY_PROJECT_ID="已绑定工程ID"`。平台脚本从已确认设计生成 RDF，通过 Protégé load_ontology、注释、HermiT/SHACL、save_ontology 导出并回读；这是整体导入路径，不声称执行了 preview_change_set/commit_change_set。最终调用正式 service.record_ontology_build 内部完整验收门禁，而非显式 MCP token。只传当前工程 ID，不读/展示凭据或覆盖端点，不编辑阶段状态、不重复运行已有任务。

受管入口确实不可用时，才按当前工具目录分解调用；批量变更遵守实际 preview/commit revision，整体导入遵守实际 load/save 协议。自行收集的 S5 MCP 预检载荷包含 ontology_owl / ontology_ttl / shapes_ttl 与 protege_build_report，预检通过后提交 token。平台核对真实构建身份、轨迹、HermiT、SHACL、设计、公理、中文说明与 NodeShape；命令结束后回读当前工程真实状态。

## S6 质量与业务验收

确认绑定工程 S5 已过且当前可执行 S6 后，在实际代码仓库根优先运行 `make ontology-s6 ONTOLOGY_PROJECT_ID="已绑定工程ID"`。脚本复用平台配置并管理执行租约/心跳，包含全量来源、Semantica 规则验证、服务端预检和 token 提交；模型不重复提交或读取凭据。脚本 HermiT 报告引用 S5 实际 run_id，不冒称新的 S6 HermiT 执行。入口不可用才分解工具调用；始终回读当前任务进度和正式阶段状态。

`record_quality_validation` 的完整对应载荷包括 materialized_ttl、hermit_report、mapping_report、semantic_quality_report、competency_question_report、semantica_report，先 S6 预检后 token 提交。

逐项执行范围以正式 `quality-summary.json.validation_plan.checks` 为准：`execution_mode=EXECUTED_IN_RUN|REUSED_PRIOR_STAGE|NOT_APPLICABLE`，`status=PASSED|PASSED_WITH_NOTES|NOT_APPLICABLE|FAILED`，并保留 `source_stage`、`evidence_refs` 与 `reason_zh`。HermiT 复用 S5 本体一致性回执，不宣称 S6 全实例重跑；完整物化图的 SHACL、CQ、关系验收仍由平台真实执行。S7 的 Semantica 发布同步等事项单列 `deployment_tasks`，不能冒充已执行。历史缺字段不补造。

平台同轮预检/提交复用公开记录为 `validation_execution`：仅 `mode=REUSED_PREFLIGHT` 且 `execution_reused=true` 表示正式提交复用本次已验证回执；`EXECUTED_AT_COMMIT` 表示提交时执行。模型不重复发完整载荷，不手工填写或修改验证回执；输入、验证器或身份变化由平台核验拒绝。

只接受当前资产与全量来源验证：生产覆盖把 S1 数据行数、文档证据数和 S0～S5 指纹逐项对账；规则结果包含正式规则哈希、执行范围、输入/输出、正反边界与真实轨迹。代表性样例只能诊断，不能填成 FULL_SOURCE_VALIDATION。平台重跑 SHACL 并核对 CQ 语义，不把非空结果当成正确答案。

SWRL/SQWRL 可作为设计/规则资产，只有真实能力契约声明并通过 S6 的执行器才可用于正式发布。Protégé 有插件不证明 ORION 已经接入它的在线规则服务。

## S7 交付与发布

先回读 S6 已签发 production_ready 及全部必要证据；S7 不补写生产验证。用户明确批准后，`publish_ontology_package` 传 release_version、APPROVED、真实 approved_by 和 release_notes。

工程包用于复核建设依据和后续修订；发布资产包含运行需要的模型、映射、规则与查询契约。按实际 manifest 说明包含范围，实例、原文、密钥和完整审计不默认对外导出。包生成、负责人批准、目标部署和在线问数/推理回读是不同状态；只有全部适用门禁真实通过才报告可用。

包生成后运行验证失败保留包与批准记录，进入 PACKAGE_READY_RUNTIME_BLOCKED，只恢复部署/回读；不能重做 S4～S6 来掩盖部署故障。`sync_published_ontology_to_semantica` 只同步已发布版本，不代替 S6 验证。

## 恢复、修订与状态

- 失败修复：读 last_error，修复并预检当前责任阶段，再按推荐工具 retry_failed_stage。阶段进行中先读真实执行租约/进度，不能并发重复构建。
- 留痕修订：`preview_stage_rollback` 获得影响与 token，再以同一 changed_components 调用 `reopen_stage_for_correction`；`get_project_revision_history` 展示原因、失效产物和差异。
- 已发布新修订用 `create_revision_from_release`；撤回、归档、恢复分别用 `revoke_ontology_release`、`archive_ontology_project`、`restore_ontology_project`，需要具体授权。
- `verify_ontology_project_integrity` 核验资产和审计；`get_workflow_storage_status` 查询持久化。不能直接改 workflow-state、manifest、Mapping 或追加式事件。
- BLOCKED_HUMAN 表示当前决策待处理；S4 v2 是联合设计关口。S1_S3_READY 是前序完成、可进入设计，不表示 S4 完成。PUBLISHED 与包生成、运行失败分开回读，不根据对话推断。


## 大草稿局部修正

首次 `save_stage_submission` 返回 `payload_file` 后，后续修正优先 `patch_stage_submission(project_id, stage, expected_revision, payload_file, operations)`。operations 为 JSON Pointer 的 add/replace/remove/test，数组追加用 /-；先 test 旧值可避免改错位置。服务端核对基线 SHA-256、修订，保存新草稿并完整预检；失败不修改旧草稿或正式阶段。不要重传未变的大段规则、查询和来源事实。通过后只提交新令牌，不能复用旧草稿令牌。
