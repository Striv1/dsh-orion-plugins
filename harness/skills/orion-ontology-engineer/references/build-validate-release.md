# S4 设计、S5 构建、S6 验证与 S7 发布

只在工程进入相应阶段时读取本文件。

## 明确授权代办

用户已明确委托当前工程设计确认、工作台决策或批准发布时，先审阅本次具体候选与版本，再通过原有真实决策工具代办，保留授权人、实际代理操作者、依据和当前版本指纹。下文“等待负责人/用户批准”不要求重复索要已经授予的代办权限；未授权的工程或操作仍须等待。平台续跑通知只传递已保存状态，不赋予额外批准权限。

## S4 联合设计定稿

- 先读 `stage_contract_version`。v2 使用 `review_scope: JOINT_DESIGN`，本体模型、正式映射、规则与 CQ 共同定稿；缺字段或 v1 保留历史 CQ 评审策略。不能自动给旧设计补“已联合确认”结论。
- 基于 S0 目标、S1 理解、S2 语义和 S3 已审候选编译设计。业务类可以来自文档、数据或规则推导，不要求每个类都对应一张物理表；各类来源必须说明依据。
- 本体设计明确类、对象属性、数据属性、IRI、实体身份、公理和数据约束。OWL 逻辑一致性、SHACL 数据完整性、规则派生和查询聚合分别选择适配能力，不能混为一类校验。
- `G-S4-PRODUCTION-LOGIC` / `G-S5-LOGIC` 默认要求有来源的合法公理。v2 的纯结构化事实路径，以及 HYBRID/数据库工程中全部原始 CQ 已由正式事实查询或已审生产规则覆盖、没有 OWL 推理需求的路径，可由服务端计算并冻结 `logical_axiom_applicability=NOT_APPLICABLE`；模型不得自行填写豁免标记或凭空制造公理。待人工评审规则仍须在 S4 联合批准理由中点名确认。S5 会重新核对来源和适用性；型别、映射、实例、SHACL、CQ、规则真实执行与发布验收均不能省略。
- 正式映射与模型一起校准 namespace、类型、IRI 模板、关联和完整范围；规则声明来源、事实范围、前提/结论、边界和执行器。CQ 保留用户原问题，平台生成可执行查询和正确结果判据，不要求业务用户写 SPARQL 或内部 answer_contract。
- 实际工具优先 `generate_ontology_design`；专家导入才用 `prepare_ontology_design_review`。正常链路先 `preflight_stage_submission(stage="S4", payload=...)`，通过后 `commit_preflight_stage_submission`。`record_ontology_design` 是平台内部落库口，禁止对话 Agent 绕过生成与审阅。
- 正常预检载荷为 `payload={"generation_request":{"logical_axioms":[有来源的公理],"review_policy":"HUMAN_REQUIRED"}}`，同时传工程当前 `expected_revision`；省略 `competency_questions`。平台从 S3 结构化、文档事实或规则能力的 `cq_bindings` 选定真实验证用例，渲染查询参数并生成 FIRST 结果断言、已审边界、业务维度及规则绑定。缺失绑定应回 S3 修正，不能空载荷反复正式生成或让模型手填内部答案契约。
- 公理不会自动猜造。仅支持 `SUBCLASS_OF(child,parent)`、`DISJOINT_WITH(class,other)`、`EQUIVALENT_DATA_HAS_VALUE(class,base_class,property,value)`、`EQUIVALENT_OBJECT_SOME_VALUES_FROM(class,base_class,property,filler)`；每项还须 `id/axiom_type/source_refs`，IRI 必须已声明。来源只证明有两个类，不足以证明互斥；不能用自造 `EQUIVALENT_CLASS` 或新类绕过生产门禁。
- v2 必须 `HUMAN_REQUIRED`。预检通过仅表示草案可交审，不表示已批准；平台产生 `BLOCKED_HUMAN` 后展示一份中文整体摘要、来源/设计报告和本次 `joint_design_fingerprint`，等待负责人一次确认。不得用 `AUTO_APPROVE_EVIDENCE_BACKED`、旧 CQ 批准或一般“继续构建”替代本次整体确认。
- 负责人批准或退回时仍调用真实 `resolve_competency_question_review`：`decision` 只能用 schema 的 `APPROVED` / `RETURN_TO_S3`，携带当前 `questions`、真实 `decided_by`、理由和 `expected_revision`。在 v2 中它批准的是整套设计；名称沿用历史，不代表只批 CQ。
- 若批准调用报错，先回读阶段与评审：只有 S4 仍为 `RUNNING` 且同一联合设计评审已是 `APPROVED` 时，才使用当前 revision 再调用一次 `resolve_competency_question_review(APPROVED)` 恢复冻结；沿用已批准问题原文，不重新生成草案或重复做人工决定。若评审仍 `PENDING`，按门禁差异修复后再批准。
- 未取得本次具体设计授权时，展示上述摘要后调用 Harness 原生 `ask_user_question`，提供确认整体设计与退回修改两个真实选项；等待实际回答，再核对 revision 和设计指纹后记录。跳过、取消和有条件的自定义回答不能直接转成 `APPROVED`。
- 调整本体/映射/规则必须先重新形成可审阅设计，不能在对用户展示的设计之外暗改语义。问题编辑由平台重编译并验证；有新高影响冲突应退回，不能据旧设计指纹进入 S5。
- S4 会以正式设计类型核对 S3 规则事实绑定：数据属性和对象属性必须有主体、值两个参数；一元业务分类应声明有业务含义的类，不能借用普通属性或宽泛的上位类蒙混过关。发现不匹配时通过正式修订更新来源绑定和设计，重新整体评审。
- 决策后回读 `get_ontology_workflow_status` 与 `get_next_workflow_action`，核对实际 S4 状态、整体确认资产、指纹与 S5 入口。此阶段不执行 Protégé 正式构建。

## S5 构建与装配

1. 回读绑定工程状态与 `get_next_workflow_action`，确认 S4 已通过、S5 为 `RUNNING`。在 3081 调用 `start_managed_stage_execution(project_id, stage="S5", expected_revision=当前 revision)`；返回 `job_id` 后用 `get_managed_stage_execution(wait_seconds=60)` 反复等待到终态（服务端有界等待，不用 bash sleep）。已是 `ALREADY_RUNNING` 时只观察，不重复启动；`FAILED` 或 `NEEDS_RECONCILIATION` 时先定位原因与正式执行租约，不原样重跑。
2. 平台受管任务复用已有 Protégé construction 角色路由、施工租约与正式预检/token 提交。它从批准设计确定性生成 RDF/SHACL，调用真实 Protégé `load_ontology`，设置本体标识与中文注释，运行 HermiT/SHACL，导出 OWL/TTL 并往返校验；模型不读取/显示凭据，不手工转录大体积本体文本。
3. 成功后回读当前工程状态与 `05-ontology-build/protege-build-report.json`，确认真实 S5 `PASSED` 且 S6 已开放；`STARTED` 或 Protégé 工具调用成功都不是阶段验收。只有受管入口不可用且工具链确实闭合时才按已挂载 MCP 工具分解施工，收集完整真实产物后走 S5 预检/token 提交。禁止临场改变已批准的业务语义。

## S6 质量与业务验收

- 回读绑定工程 status/next_action，确认 S5 `PASSED` 且 S6 为 `RUNNING`；在 3081 调用 `start_managed_stage_execution(project_id, stage="S6", expected_revision=当前 revision)`，随后用 `get_managed_stage_execution` 观察。平台后台使用既有 `scripts/run_quality_validation_stage.py`、manifest 检查、执行租约与心跳；模型不读取/打印凭据、不改阶段状态。入口不可用才按已挂载工具目录拆解；已有运行任务时不重复启动。
- HermiT 回执引用 S5 已完成的真实 Protégé run_id，属于复用本体一致性证据，不代表 S6 对全量实例重新运行 HermiT。S6 在本地完整物化图执行 SHACL、CQ 和关系完整性验收；实际规则能力按声明向 Semantica 提供本轮事实并验证推理。完整实例图的持久化导入不是已启用事实查询/规则调用的默认前提，不能据此省略本地完整图验收。
- 完成后读取真实 `quality-summary.json` 的 `validation_plan.checks`，分别解释本次执行、复用已验证证据、不适用、带说明通过或失败。复用必须引用来源阶段及证据；带说明通过必须保留原说明（如批准条件下的未知值），不得改写为无条件通过。历史报告缺少这些字段时明确“未记录执行方式”，不推测重跑范围。
- `validation_plan.deployment_tasks` 是 S7 部署交付事项；本体到 Semantica 的发布版本同步以 S7 实际回执判定，不把待同步当作 S6 失败，也不提前宣称已同步。
- 数据库/混合工程先启动绑定当前 S0～S5 指纹、正式 OBDA 和 ontology.ttl 的临时只读 Ontop 候选运行时，再全量构造实例并批量验证 CQ。
- 按已声明能力调用实际规则执行器。真实来源结果与受控正反例分别留证；业务规则在真实数据上未触发时，不能把测试正例的结论加入正式实例图，也不能宣称真实数据触发了规则。
- 生产只接受 `FULL_SOURCE_VALIDATION` 或 `FULL_SOURCE_VALIDATION_VIA_BASE_RELEASE_RUNTIME`；代表性探针只能诊断。
- `production_coverage` 必须闭合：数据库 evaluated 等于 S1 `total_rows`，文档 evaluated 等于 S0 `evidence_count`，`failed=0`，并绑定当前正式输出指纹。
- 受管 S6 脚本已包含完整载荷预检和 `preflight_token` 提交，模型不再重复提交一次。执行过程中观察当前命令任务、工作台与 `06-quality-validation/run-status.json` 的子门禁、进度和 heartbeat；结束后回读绑定工程状态及真实报告。工具分解路径也遵守相同预检/token 协议。HermiT、SHACL、Mapping、语义、CQ、Semantica 或覆盖任一失败都不得进入 S7。
- 同轮预检结果由平台校验身份、输入和验证器指纹后，可在正式提交时复用已验证回执。仅实际 `validation_execution.mode=REUSED_PREFLIGHT` 且 `execution_reused=true` 时说明此复用；这不等于未验证，也不表示正式提交重新执行了全部检查。回执失配由平台拒绝，模型不得修改哈希、密封回执或自行选择复用。

## S7 交付与发布

- 只消费 S6 已签发的 `production_ready`，不在 S7 补写证据。缺失门禁时退回最早失真阶段。
- 先展示 S4～S6 质量结论、风险和版本建议；没有明确发布授权时等待批准。当前工程已经明确委托代审并批准发布时，审阅具体版本及真实回执后沿用该授权，记录授权人与实际操作者，不重复询问。
- 获得授权后调用 `publish_ontology_package`，传入语义版本、`APPROVED`、真实 `approved_by` 和发布说明。
- 本次版本尚未获授权时，用原生 `ask_user_question` 提供批准该版本与暂缓两个选项，并先展示实际版本和质量结论。仅明确批准且当前待审依据仍一致时调用发布；原生卡片关闭、跳过或失败不推进发布。
- 返回实际生成的工程包、发布资产路径、版本、manifest 与主要资产。工程包供交付、审计和修订；运行发布资产供部署和调用。以真实包清单判定包含项，不声称只有文档或 ZIP 就代表在线服务就绪。
- 正式包已经生成、但 Ontop/Fuseki/Semantica runtime 验证失败时，保持 S7 为 `RUNNING`、工程为 `PACKAGE_READY_RUNTIME_BLOCKED`，仅重试运行时部署与读回；不得回滚或重建 S4～S6。
- 已绑定工程的现有发布包需要恢复运行时，优先在仓库根执行 `make ontology-s7-resume ONTOLOGY_PROJECT_ID="已绑定工程ID"`。入口读取并校验原 `publication.json` 的批准、版本和包清单，回读 status/next_action，只通过正式 `publish_ontology_package` 同参数幂等重试；同一进程等待部署及工作流状态回写后返回。已发布且就绪时只读返回；没有原批准或现有包则拒绝，不要求重新批准，也不新建发布包。失败时按输出定位运行时原因，修复后再执行该入口，禁止反复无改动重试。
- 恢复期间观察受管命令和 S7 工作台的业务查询验收计数、当前案例及耗时。不要使用短生命周期的 `harness.orion_workflow_action` 或未绑定工作流回写器的 `scripts/run_s7_realtime_deployment.py` 替代这个受管入口；进程必须保留至终态。

## 阶段调整与修订

1. 先识别变化组件（如 `RUNTIME_RULES`、`COMPETENCY_QUESTIONS`、`ONTOLOGY_SCHEMA`、`DEPLOYMENT_CONFIG`），把 `changed_components` 传给 `preview_stage_rollback` 获取影响与一次性令牌，再以同一组件集合调用 `reopen_stage_for_correction`。
2. 冻结调整前资产，将受影响阶段及后续阶段恢复为待验证；不得覆盖旧资产后继续显示 PASSED。
3. 每次调整生成 `revision_id`、`before-state.json`、`before-manifest.json`、`diff.json` 和 `change-report.html`。
4. 用 `get_project_revision_history` 展示原因、发起人、失效阶段、新旧值、受影响报告和重新验证状态。
5. `events/agent-trace.jsonl` 是追加式哈希审计链，禁止删除或重写。

## S5～S7 的实际工具与责任

| 阶段 | Workflow MCP | 专业工具/平台职责 | 模型与人工边界 |
| --- | --- | --- | --- |
| S5 | 3081 `start_managed_stage_execution(S5)`；受管脚本完成正式预检/token 提交 | Protégé 真正导入、验证并导出，HermiT 检查本体逻辑，平台核对设计覆盖、SHACL 与资产身份 | 模型使用已绑定 ID 发起受管入口并回读，不重复手工施工或捏造 preview/commit |
| S6 | 3081 `start_managed_stage_execution(S6)`；受管脚本完成正式预检/token 提交 | Ontop/Fuseki 供事实，Semantica 按已声明能力执行规则，平台执行 SHACL/CQ、核对 S5 HermiT 回执及全量覆盖与版本 | 模型观察租约心跳/进度，定位最早失败责任阶段，保持业务问题原意 |
| S7 | `publish_ontology_package`；暂缓/恢复分别 `defer_ontology_publication` / `resume_ontology_publication` | 平台生成不可变包与 manifest，按运行契约部署、回读，保存批准与失败证据 | 发布需要明确授权；暂缓、撤回、修订不默认替用户决定 |

所有阶段写入后核对正式响应；响应已含当前 revision、状态和下一动作即构成回读。缺失/陈旧、外部受管执行结束或决定依据需重验时，按需调用 `get_ontology_workflow_status` / `get_next_workflow_action`；确需完整性核验时用 `verify_ontology_project_integrity`。`sync_published_ontology_to_semantica` 只用于已经发布资产的同步，不是 S6 生产验证的替代品。S7 不在通用 preflight 的阶段枚举中，不伪造 S7 token。

SWRL/SQWRL 可以保存在设计或规则工程资产中，但当前生产执行按实际 capability-plan / runtime contract 声明。只有接入真实执行器、S6 验证并绑定发布版本，才可说它能用于在线推理；不能因为 Protégé 有插件就宣称 ORION 已集成新引擎。
