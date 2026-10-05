# 工程接入、S0 与 S1

仅在创建新工程、固化资料、执行 S0 或 S1 时读取本文件。

## 轻量发起合同

发起入口的三种会话模板与自由输入进入同一流程，不增加第二套建项目接口。只收集业务目标、要回答的 CQ、资料/数据库来源范围，以及用户已知的答案判据或特殊口径；名称和业务域可从完整需求推断。用户不必知道 IRI、Schema、Mapping、SPARQL、工具参数或各阶段实现。

- 模板填入原生消息草稿，由用户审阅后发送；点击模板、填写草稿、上传文件均不等于批准 S4 设计或 S7 发布。
- 模板是可编辑的需求辅助，不是机器资产。未替换的占位符、示例 CQ、示例范围和“预期答案”不能当作来源事实或真实验证结果。答案判据可由模型基于真实证据提议，不能让用户手算全量答案。
- 纯资料仅使用当前获授权的上传/引用批次，数据库使用可验证的连接与只读表范围，混合同时核对两者。上传回执、原生附件引用和数据库选择必须真实可回读；消息中说“已上传”不能代替实际来源绑定。
- 模板的数据源选择器复用 Chat2DB 当前可见目录。每个选中项必须保持数据源 ID、数据库、Schema、表的完整归属；跨源/跨库同名表不能合并。消息中的 `datasource_id` 仅定位 Chat2DB 连接，不能冒充正式 `SourceBinding.source_id`，展示标签也不能直接当作绑定回执中的 `datasource_label`。先回读或通过受管来源接入取得每个库独立的真实绑定，核对实际连接数据库；再将各库分别登记到 `source_scope.sources` 的 DATABASE 项，使用回执中的 `source_id`、`database`、`schemas` 与已选 `table_scope`。同一连接的多个库不能复用同一个绑定 ID，也不能仅修改库名却复用实际指向另一库的连接。S1 用 `list_source_connections` 查看接入就绪状态；用户已选择 Chat2DB PostgreSQL 数据源时，将选择器的 ID 传入 `capture_database_snapshot.chat2db_datasource_id`，复用其连接，不让用户再次配置账号。也可选择已登记的 `connection_env`，两者互斥。平台按用户授权的库/表范围建立正式 SourceBinding 并提升完整快照，取得 `dataset_ids` 后调用 `record_data_understanding_from_datasets`；连接未就绪或未通过只读校验时，明确报告实际原因与修复入口；不自动改权限、不从 Chat2DB 提取密码，不能伪造身份或宣称快照就绪。生产类型必须写明真实授权依据，不得为过门禁把测试数据标成 PRODUCTION。整库选择指本次目录明确枚举的表清单，不包含未来新增表；空表清单不代表整库授权。选择目录不等于 S1 已完成；执行时仍验证真实连接身份和当前范围。目录为空或失败时报告真实缺口，不猜 `public/default` Schema。
- 开始时用一份简短回执说明目标、选定来源模式、来源范围和原始 CQ；信息齐全且已授权时直接执行，不增设一次“是否开始”审批。仅剩关键业务或来源歧义时集中询问。
- CQ 是范围与验收依据：只生成回答它们所需的模型、映射、实例、规则和查询，并保留必要的标识、关联与证据。模型建议的扩展问题单独标注，不擅自变为新增必做范围。
- 用平台判定的来源模式与能力合同选择执行路线。轻量不是另一个可跳门禁的 profile；仍使用 PRODUCTION，并在真实完整来源上验收所有原始 CQ，等待具体版本的人工发布批准。

## 工程绑定与创建

- 可信绑定只有三种：工程页面传入的绑定、用户当前消息明确给出的精确 `project_id`、当前任务本轮创建后返回的 `project_id`。
- 最近工程、相似名称、旧任务、日志、目录扫描、缓存和 `/tmp` 文件都不是绑定。未绑定时用户要求“构建/新建本体”，必须创建新工程。
- 纯资料新工程优先使用 `create_ontology_project(intake_mode="DOCUMENT_ONLY", source_snapshot_path=真实完整批次路径)`，平台完整校验后生成全部来源，勿逐条转写百份 `source_scope`；与显式 `source_scope` 互斥。混合模式或旧入口有文件的新工程先固化资料，再调用 `preflight_workspace_snapshot`，将返回的 `source_scope` 直接用于 `create_ontology_project`，并用稳定 `request_id` 只创建一次。无文件的 `DATABASE_ONLY` 直接按真实数据库来源合同登记，不要求文件快照或 OCR。资料条目使用平台已验证的 `source_path`、`source_name`、`source_sha256`、`kind` 和 `path_scope`，推荐省略 `source_id`，不得补写模型自编 ID。后续始终复用返回的 `project_id`。
- 完整需求已经给出时，直接推断中文工程名、业务域、CQ 和接入路线；只有关键目标、资料或数据源无法安全推断时，最多追问一个具体问题。
- CQ 默认 `USER_PLUS_AI`；用户明确只采用自己的问题时用 `USER_PROVIDED`；完全没有 CQ 时用 `AI_GENERATED`。创建阶段不要求用户写 SPARQL。

## 版本与路线判定

先读取 `stage_contract_version`。v2 是 `s0-s7-stage-contract-v2`，字段缺失或 v1 保留历史职责，不自动迁移。

| 路线 | v2 S0 目标与来源登记 | v2 S1 资料与数据理解 |
| --- | --- | --- |
| `DOCUMENT_ONLY` | 创建时登记目标与资料范围，仍使用受控 S0 批处理解析 | 平台根据资料证据自动生成理解结果与报告；只有数据库子任务不适用 |
| `DATABASE_ONLY` | 登记一个或多个数据库来源，`record_s0_scope_decision` 完成范围留痕 | 对已授权来源做只读画像与完整性对账 |
| `HYBRID` | 统一登记资料与数据库来源，资料仍走 S0 批处理 | 汇集文档与结构化数据理解证据 |

v1 的 `DOCUMENT_ONLY` 完成 S0 后仍将整个 S1 标记不适用，`DATABASE_ONLY` 的 S0 保留原范围判定。不能把 v1 的阶段跳过冒充 v2 理解验收通过。

一个或多个数据库都使用 `DATABASE_ONLY`，没有 `MULTI_DATABASE` 枚举。创建时在实际 schema 支持的 `source_scope` 中登记来源、边界与用途，多个来源逐项绑定；模型不猜内部数据源编号或授权表。用户已经明确目标和来源时直接记录，不重复请求范围批准。模糊的跨库实体关联留到 S3 语义评审，不把同名字段当作已确认连接。

Excel/CSV 需要结构化导入时必须选择 `HYBRID`；Word/PDF 保留在文档证据链。不得先建 `DOCUMENT_ONLY` 再暗中改变路线。

## S0 目标登记与资料接入

1. 只处理用户当前消息明确 `@` 的文件或文件夹，或当前上传入口已验证且获授权的精确附件批次；不扩展读取同级目录，不把历史附件默认并入，也不再次索要已经绑定文件的绝对路径。
2. 原生附件优先消费宿主注入的 `MESSAGE-…` 候选批次：`get_message_attachment_candidates` 核对当前会话消息的候选，按用户授权选出明确 `attachment_ids`，一次 `snapshot_message_attachments` 固化完整选定批次。候选本身不授予来源授权，排除项不能并入；批次与工作区无关，不能扫描 Harness 附件目录或工作区补找。没有宿主批次时报告附件交接故障，不猜路径。纯资料创建直接传返回的 `source_snapshot_path`，服务器完整预检，不手写来源数组。
   本地引用调用 `snapshot_workspace_sources` 固化到受控资料区；已上传的受控批次直接复用其真实回执并预检，不再复制成第二个输入批次。后续预检、OCR、证据哈希和报告只使用平台返回的 `source_path`。快照与 `preflight_workspace_snapshot` 均返回经完整清单校验的 `source_scope`，其中不生成模型自编 `source_id`；同内容、不同路径的文件分别保留原路径。
3. 对已创建并绑定的 S0 工程调用 `start_document_ingestion_job(project_id, source_path, expected_revision, actor)`，使用资料预检返回的受控路径和工作流当前修订。它直接进入 3081 现有后台队列，不需要 HTTP 登录。返回 `job_id` 后用 `get_document_ingestion_job` 查询。当前工具目录未挂载此入口时报告平台工具待刷新，不自行 curl、不索要认证 URL、不启动 `dsh web`。单图、单页诊断或失败页排障才直接调用 PaddleOCR MCP。
   工作区引用的 `REFERENCE` 和浏览器上传的 `UPLOAD` 批次均由同一平台入口校验真实文件清单、内容回执和已登记来源范围；排除资料必须在解析前被拒绝。已有工程不能把上传动作视为扩大来源授权。尚未创建工程的受控资料批次，在复核创建时仅以平台验证过的精确选定文件清单登记范围。
4. 批处理固定使用“原生文字层 → 150 DPI PP-OCRv6 → 表格/复杂版面时 200 DPI PP-StructureV3”。相同 `project_request_id` 复用已有任务。
5. 无警告且复核解析结果后，调用 `commit_document_ingestion_job` 提交 `READY_FOR_REVIEW`，传入真实复核执行者和理由；大表 HYBRID 指定 `structured_data_action: IMPORT`，由平台全量导入本地数据库，不让模型逐行转写。存在警告或失败时暂停检查，不能默认接受。调用超时后先查同一任务状态，不立即重复提交。旧修订任务禁止写入新修订。
6. 每份资料保存 `document_id`、原始文件名、页数和 `source_sha256`；每条证据以 `evidence_id`、`document_id`、页码/定位和 `markdown_section` 回到原文。
7. 失败页、未处理页或未复核的低置信度页任一大于 0，S0 不得通过。
8. 批处理通过 `record_document_evidence` 保存资料、Markdown、质量报告、证据索引和真实 `processing_trace`；不得另写临时文件拼装载荷。
9. 纯数据库工程调用 `record_s0_scope_decision`，记录 `DATABASE_ONLY`、判定人、原因和数据源引用；不得无记录跳过。

已写入 S0 后若结构化导入中断，保留 S0 和已完成的数据集。确认原执行者已停止后，使用同一 `job_id`、回读的当前 S1 `expected_revision` 和 `structured_data_action: IMPORT` 调用 `commit_document_ingestion_job` 恢复；平台校验 S0 资产与修订绑定，并互斥导入。仍在运行时只查询，不并发重试，不重开 S0。

已有 S0 工程因自编 `source_id` 与平台真实文档标识不一致而被来源门禁拒绝时，先回读工程与受控完整批次，再调用 `reconcile_document_source_identities(project_id, source_path, expected_revision, actor, reason)`。平台只移除经真实 `DOC-hash` 依据确认不匹配的自编 ID，保留原范围文件及前后差异，不改变路径、哈希、名称、类型、排除项或授权范围。完成后回读 revision，再继续同一工程；禁止删工程重建、手改 `workflow-state.json`、`source-scope.json` 或审计账本。路径、哈希或业务授权本身不匹配时，该工具不能用于放宽范围。

## S1 资料与数据理解

`DOCUMENT_ONLY` 不调用 Chat2DB。v2 由平台从真实资料证据生成 `source-understanding.json` 和报告，S1 的数据库子任务不适用；v1 仍是整个 S1 留痕跳过。`DATABASE_ONLY` 和 `HYBRID` 才使用 Chat2DB。

- 已挂载的 Chat2DB MCP 仅提供当前 S1 的只读数据能力；调用仍遵守本 Skill 的阶段顺序、工程绑定、来源授权和门禁，不扩大数据范围。
- 仅使用工作台挂载的 `mcp__chat2db__*`：发现数据源、读取表/字段/PK/FK、执行 `SELECT`/`WITH`，明细样例不超过 100 行。
- 生产画像使用 `FULL_IMPORT_WITH_EXACT_COUNTS`。表集合、逐表 `row_count`、`table_count`、`total_rows`、`empty_table_count` 必须闭合，并绑定当前 `project_id`、`dataset_id`、源文件 SHA-256 和 `READY` 状态。
- 每条证据 SQL 必须有本次真实执行回执：`status: PASSED`、`executed_via`、`executed_at`、`result_sha256`、预期/实际行数和 `source_tables`。
- 两个用户入口分别是 `@ 本地文件夹` 和 Chat2DB/RDB 连接发现数据源。文件表格走受控全量导入；外部数据库先确认只读 SourceBinding 与授权表/列，再形成正式 SnapshotHub 快照。Chat2DB 的连接成功或抽样查询不能替代正式快照登记，不要求用户拼装内部 JSON。
- 已登记数据集调用 `preflight_stage_submission(stage="S1", payload={"dataset_ids": [...]})`：平台自动识别文件导入目录或当前 SnapshotHub 快照集并生成真实回执；快照集必须完整选择，不混入旧版本。预检通过后用 `commit_preflight_stage_submission` 消费令牌。也可使用自动预检的 `record_data_understanding_from_datasets`。失败工程先修复并预检，再 `retry_failed_stage`；修订改变后重新预检签发令牌，不复用旧令牌。
- 无法安全选择数据源、疑似生产库、只读会话不可恢复或证据范围不闭合时，停止并说明阻塞，不把旧结果包装成新回执。

Chat2DB 会话问题按 [mcp-recovery.md](mcp-recovery.md) 处理。

## 工具与协作闭环

- S0 原生附件：`get_message_attachment_candidates` → 明确授权子集 `snapshot_message_attachments` → DOCUMENT_ONLY 引用 `source_snapshot_path` 幂等创建。
- S0 本地引用 Workflow MCP：`snapshot_workspace_sources` → `preflight_workspace_snapshot` → 使用真实 `source_scope` 幂等 `create_ontology_project`；已有工程仅技术标识失配时用 `reconcile_document_source_identities` 留痕恢复。资料用 `start_document_ingestion_job` / `get_document_ingestion_job` / `commit_document_ingestion_job`，纯库用 `record_s0_scope_decision`。`record_document_evidence` 是处理结果记录口，不是任意文件读取器。S0 不在 `preflight_stage_submission` 的枚举内，不伪造 S0 preflight token。
- S1 实际 Workflow MCP：已登记数据集用 `record_data_understanding_from_datasets`，或 `preflight_stage_submission(stage="S1", payload={"dataset_ids": [...]})` → `commit_preflight_stage_submission`。外部来源的真实画像载荷也可预检。文档 v2 自动验收由平台推进，模型只回读，不重复调用。
- 外部职责：PaddleOCR/文档解析器提供真实解析与定位；Chat2DB 只提供当前已授权数据源的只读发现和查询，确切工具名以当前会话目录为准；结构化全量导入和证据存储由 ORION 管道负责。
- 模型职责：补齐业务目标、解释资料与字段含义、识别缺口。平台负责来源身份、全量行数、不可变快照、报告与门禁。缺内部回执时修复平台输入，不要求用户编写 JSON。
- 写入响应已有当前正式状态时直接核对阶段、revision、当前资产、子任务是否不适用和下一允许动作；信息缺失/陈旧或外部受管任务结束时，再按需调用 `get_ontology_workflow_status(project_id)` / `get_next_workflow_action(project_id)`。超时先查询同一任务，不能凭调用成功猜阶段已前进。

- v2 DOCUMENT_ONLY 留痕重开 S1 后，平台会推荐 `record_document_understanding(project_id, expected_revision)`。该工具从已保存文档证据重新验证理解并落库，正常 S0 后的自动 S1 不需要再调用；它不接受自造 dataset_id，也不重做 OCR。

## 优化后从哪里继续

- 仅优化工具合同、草稿提交或报告，不自动重开任何正式工程。新 Skill 和新入口可用于下一次合规动作；已通过产物仍按原回执处理。
- 同一目标、来源与 CQ 不变，且需要重新核验资料理解时，v2 纯资料工程可保留已通过 S0：先 `preview_stage_rollback(project_id, target_stage="S1")` 展示影响，再按已获修订授权调用 `reopen_stage_for_correction`，传真实 preview token、project revision、原因与操作者。回读后使用 `record_document_understanding`，它验证 S0 指纹并重建 S1 理解回执，不重做解析，也不自行产生更丰富的业务实体抽取。后续重新进入 S2 建模并保留 S4 整体审阅。
- 只改业务对象、规则或映射时，应预览其责任阶段（通常 S2 或 S3），不默认从 S1 重来。`changed_components` 仅按实际变更与平台依赖图声明；不得靠缩小变更声明保住实际已失效的产物。
- 来源、目标或原始 CQ 本身改变时，S1 重验不能改变 S0 授权范围和初始需求；按真实平台支持的 S0 修订路径处理。需要独立对照或明确新建时，重新固化本次授权资料、预检 source_scope、幂等创建新工程并走受控资料队列。不能复制旧工程目录或只复制旧 S0 报告冒充新工程验收；是否复用解析由平台回执决定，不承诺免解析。
- `preview_stage_rollback` 会保存短期预览令牌，虽不改阶段，也不是零写入操作；只读审计先读代码/状态，不实际发起预览。正式回退受 revision、令牌期限和发布状态约束；已发布版本不能原地覆盖，按发布控制与新修订路径处理。v1 纯资料工程不能重开 S1，不能借此自动迁移 v2。

## 误传、补充与移除资料

资料更正是平台能力。S0 未验收且无活动/待复核解析任务时，重新固化用户选定的完整资料批次，使用 `replace_document_sources`，带当前 revision、操作者及原因。完整批次代表更正后全部文件，遗漏文件会被移除，不能将单个新增文件误当完整批次。回读新 revision 后重新受控解析。保留目标、原始 CQ、旧产物审计和混合模式数据库授权。后续阶段先预览影响并按现有授权规则回退 S0；已发布版本沿用正式修订路径。此接口不授权数据库范围变更。

Markdown 证据按段落、列表和表格行定位；这些是原文陈述，不等于事实已核验。文档自称“真实/已批准”、阈值或示例规则不能替代来源核验、业务批准或有效时间判断。保留实际缺口，不把动态观测值固化为永久本体类别。
