# MCP 可用性与恢复

在调用任一阶段工具失败，或出现 `Session not found`、`Connection closed`、超时、工具未挂载时读取本文件。

## 就绪判定

端口监听或 HTTP 401 只说明服务存在，不说明当前 Harness 任务可用。可用性必须满足：

1. 当前任务完成 MCP `initialize`；
2. `tools/list` 能看到所需工具；
3. 一个安全、只读、低成本调用成功；
4. 返回结果属于当前工程或当前数据源。

## 恢复顺序

1. 保留原始错误和原始调用参数，不伪造成功回执。
2. 若运行时正在自动重连，等待工具目录恢复；不要在同一失效 session 上高速重试。
3. 工具重新挂载后，只重试原始只读调用一次；状态变更调用先回读当前 revision 和阶段，再决定是否重试。
4. 仍失败则停止当前阶段，报告“失败点、已验证状态、未完成证据、可恢复动作”。

## 禁止的任务内旁路

- 不读取本地 token、datasource registry、credential 或 settings 文件来绕过 MCP。
- 不从模型侧写 settings、重启 Chat2DB/桌面应用、启动备用 headless runtime 或临时 MCP 客户端。
- 不把旧查询结果、缓存、日志文本包装成当前执行回执。
- 不因 MCP 故障跳过门禁、直接写工作流目录或修改 `workflow-state.json`。

这些属于平台维护动作，而不是本体建设动作；只有用户明确要求维护运行环境时，才在独立维护任务中处理。

## 版本化目录恢复

工具重连后重新读取绑定工程的 `get_ontology_workflow_status` 和 `get_next_workflow_action`，核对 `stage_contract_version`、`stage_contracts`、当前 stage 和允许工具。不要沿用恢复前缓存的阶段或把 v1 误判为 v2。新增工具名写在 Skill 中不表示当前会话已经挂载；需要 tools/list 和安全调用证据。

预检 token 或整体设计批准绑定的是具体工程 revision 与资产；重连不延长 token、不恢复旧批准。先查询是否已消费或已推进，再决定是否重新预检。恢复 S4 时应展示同一份 JOINT_DESIGN 摘要与指纹，不能将连接故障解释成用户已经批准。
