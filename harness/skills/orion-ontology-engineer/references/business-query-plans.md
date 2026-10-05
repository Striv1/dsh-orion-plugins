# S3 业务计划编制（首批支持范围）

唯一编制位置：当前 S3 检查点的 `mapping_draft.business_query_plans`。
先读 `get_stage_input_contract`，用 `patch_stage_submission` 每批增加/修改一项，再调用 `compile_mapping_runtime`。
不要要求业务用户填技术 JSON；从用户已确认语义与实际来源中提出计划。不能猜身份、关系、单位、阈值或验收答案。

## 平台负责生成的内容

- `entities` 每项 `{as,mapping_ref}` 引用已编译的类映射，最多4个。
- `relations` 每项 `{mapping_ref,from,to}` 引用已有对象属性，端点必须匹配 domain/range，所有对象须连通。首版为必需关系（inner join），不适用于缺失关系判定。
- `fields` 每项 `{as,mapping_ref,entity,optional?}` 引用已编译数据属性。`select` 投影这些名称/对象。平台去重后按返回的业务对象 IRI 排序；同一对象的多值属性不承诺内序，内部条件字段不参与排序。纯标量投影无默认顺序，不接受非空 expected_first_row；用 CQ 的 ANY/ALL 边界断言核验成员，或保留对象标识。首批不支持数值/时间排名算子，不能用首行替代最大/最小值问题。过滤与规则比较仍按声明类型执行。
- `filters` 是最多8项 AND 条件 `{field,op,value}` 或 `{field,op,parameter}`，op 为 EQ/NE/GT/GE/LT/LE。字符串/布尔只支持 EQ/NE，数值和日期按属性类型比较；参数必须必填且类型匹配。参数沿用原 `parameters` 合同。
- `description_zh`、`question_examples`、`validation_cases`、`cq_bindings` 沿用既有业务语义与验收合同。事实 CQ 使用 FACT_QUERY。source_refs 可引用本能力实际依赖的映射 ID、这些映射已有的来源证据，规则 CQ 还可引用绑定的 S2 规则及其来源证据；不能引用其他能力的无关证据。执行用 source_ids、表和列由平台从实际解析的快照生成，不由模型重复填写，也不将文档证据 ID 当数据源 ID。
- 无默认截断 LIMIT；执行资源上限仍由原运行服务治理。暂不自动编译聚合、可选关系、模糊关联、跨库或任意程序表达式。

预览与正式编译使用同一映射依赖闭包。当前自动编译要求所用目标 IRI 在全草稿中只有一个生成映射；多条映射产生同一目标时须先明确多来源合并语义，平台不会在预览中只选一条、正式执行时又混入其他条。无关映射不进入单项试运行；同名表按已解析 source_id、物理快照和 dataset_id 区分。

平台不会根据执行结果倒填期望值。验收用例应来自已核对来源中的正例、反例、边界和缺失数据；没有依据就保留待办。运行不成功也不缩减原 CQ。

完整答案用 `expected_row_fields` + `expected_rows`：字段来自真实投影，每个预期行恰好填写这些字段，按无序多重集合核对，行关系配对和重复次数均参与比较。例如两笔订单及其客户须以两条完整配对元组声明，不能用“至少两行+首行正确”证明全体正确。显式 `expected_rows:[]` 要求严格空集；省略此字段不代表空集。不能从当前实际结果自动生成这些期望。

来源明确未核定的字段仍保留在完整行预期中，值写 `null`。SPARQL 的未绑定只有符合用例 `nullable_bindings:{字段:{when:{同一行其他非空输出字段:具体值},reason_zh,source_refs}}` 时才按该显式 null 比较；条件须引用本能力已审来源。该合同随用例进入正式 CQ，冲突的 CQ 空值声明会被拒绝。不要为通过检查删除未知行、删减比较字段或把 10 行拆成仅 9 行已知值的完整集合。预览、S6 和 S7 均核对完整结果及 RDF 类型。

完整集合用例可不提供首行；纯标量 CQ 因而也能声明完整结果。若同时提供首行、FIRST/ANY/ALL 边界断言，所有检查仍执行。只在所引用用例有完整集合时，CQ 的 boundary_assertions 才可省略/为空。字符串、布尔和数值类型区分，数值1与1.0相等；null须显式声明，不能把缺字段自动当null。原必填字段与条件空值合同继续执行。

## 正向条件规则

首批支持 `IF P(?x) [AND Q(?x)] THEN R(?x)`：同一对象、正向、一元、非递归。规则表达式及每个前提的条件都来自 S2，不在 S3 再写一份。

S2 规则的 `condition_contract` 为唯一条件定义。示例形状：

```json
{"version":1,"premises":[{"predicate":"HighBalanceOrder","property_candidate_id":"S2正式数据属性候选ID","op":"GE","value":100}]}
```

每个前提必须精确覆盖 `premise_predicates` 并绑定同一 S2 的 DATA_PROPERTY；该候选须有明确来源表、列、数据类型。固定常量用 `value`；只有业务来源明确允许改变的条件才用 `parameter`，并在 condition_contract.parameters 声明类型、范围、默认值。不能将固定阈值换成同默认值的可变参数。历史规则缺此定义时先正式预览重开 S2；不能在 S3 猜值或反复编译。

在一个事实证据计划内设置 `rule`：

```json
{
  "rule_id": "S2真实规则ID",
  "subject": "已在select投影的对象名称",
  "conclusion_mapping_ref": "已声明的规则结论类映射ID",
  "premises": [
    {
      "mapping_ref": "已声明的规则前提类映射ID",
      "field": "已有属性字段名"
    }
  ],
  "runtime_validation": {
    "parameters": {}, "min_input_facts": 1, "min_result_facts": 1,
    "require_rules_fired": true, "expected_live_outcome": "POSITIVE"
  }
}
```

上例阈值和验证数量仅展示字段形状，不能用于真实工程默认值。S3字段的映射必须通过 `derivation.from_candidate` 和真实来源表列引用 S2 所声明属性。兼容 `when` 写法时其算子和值须与 S2 完全一致；优先只填写 field。参数从 S2 自动继承；真正变更业务含义须走正式上游修订。

前提与结论都是已有 `RULE_TO_CLASS` 映射：`derivation.rule_id` 引用 S2 规则；前提还须 `derivation.premise_predicate`。平台自动生成事实绑定、ontology_terms 和规则包，结论复用业务主体的实例 IRI。不得为了适配一元规则把关系或数据属性改造成不合理的类；超范围需独立评审/扩展。

规则证据计划不能有 `filters`，数据属性必须 `optional:true`，确保反例与未知数据保留。缺属性的条件不成立为事实，但不会生成“否定”结论。关系仍是显式必需关系，孤立对象不在该证据范围内，不能据此推断全域缺失。

规则 CQ 放 `rule.cq_bindings`，answer_mode 为 RULE_INFERENCE；提供自己的 `validation_cases`（输出仅规则 subject）。平台生成 `cq_sparql`、`reasoning_capability`、`derived_predicates`，不要手写。规则 CQ 用例参数必须等于 runtime_validation.parameters。结论查询只取确实推导出的类成员；事实查询不能伪装推理。

两层用例验证不同结果：计划顶层`validation_cases`验证完整事实证据，须保留反例与未知对象，不能只填会触发规则的对象；`rule.validation_cases`验证推导后的结论，字段仅为`rule.subject`，值是其已确定身份映射下的实例IRI，不是证据计划的业务编号或名称字段。独立预期业务对象先由其身份映射表示为IRI，不从实际输出生成预期，也不删除原预期让门禁通过。

平台在规则证据查询中自动保留每个条件实际使用的来源属性及条件布尔值，即使业务select没有列出该属性。这样可在回执中核对判断依据；属性缺失仍保持未绑定，不补零或false。规则结论查询的输出范围不变。

## 修改、恢复与边界

小范围试运行：先 `get_business_preview(project_id)` 读取当前 S3 的能力与草稿引用；用 `start_business_preview(project_id,expected_revision,payload_file,plan_id,case_id)` 启动单项能力。平台从当前计划依赖编译实际 OBDA，使用独立只读 Ontop 查询快照；规则走原生只读 Semantica，再执行规则 CQ。没有调用者可指定的 SQL、数据连接或任意执行器。

返回 STARTING/RUNNING 后只通过 `get_business_preview` 查看进度，刷新页面仍可恢复；不要连续发起任务或让模型重新生成整份设计。FAILED 的 `diagnostics` 指向局部修订位置；INCONCLUSIVE 表示环境、预算或证据范围不足，不是通过。相同输入默认复用回执，`retry=true` 最多允许第二次尝试；后续须修复输入或运行条件，不能循环继续。草稿、修订或来源改变后旧回执标为 STALE。

首批试运行每工程一项、整个平台最多两项并发，180秒上限；查询超过200行返回不足以判断，不在截断证据上作规则结论。仅展示前5行并标明来源、实际返回数和规则轨迹。可缩小到有独立预期的一个业务用例，但不能缩减正式CQ或把样例结果冒充全域验证。草稿类型词表用于预览，不是已评审的S4/S5本体。正式阶段、审批、发布及实时问答仍独立验收。

- 同一输入重复编译不生成新检查点；修改计划会更新对应受管查询与规则。
- 编译失败与完整预检失败共享当前修订的持久预算：同一问题3次或合计8次停止原生自动续跑。换会话、无关改动、保存检查点、成功编译及重载校验器都不清零；只有完整预检通过才重置。停止后保留草稿与诊断，不重写业务条件争取通过。
- 手工修改已生成内容会明确冲突，原内容保留。回读差异后把业务改变写回计划；不删除整个运行设计来重做。
- S3 正式预检会重新核对来源/计划与执行内容，禁止沿用旧执行内容。业务卡仍先按原流程批准。
- 编译清单仅用于生成产物归属，不能当批准凭证。原 revision、检查点与 S4批准机制不变。
- 首版计划仅生成快照能力。纯文档、实时查询和更复杂规则沿用原合同，但不属于本批自动编译范围。静态通过、隔离执行与3081独立闭环分别验收。
