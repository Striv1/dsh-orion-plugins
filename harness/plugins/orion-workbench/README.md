# ORION 原生工作台插件

候选包：`dsh-orion-workbench@1.0.0-rc.6`，不带 scope 前缀。当前用于本地私有验收；适配目标为官方 DeepSeek Harness `0.2.0-rc.2`。

工作台由官方 `sidebar.panellist` 和 `main` 插槽加载。工程、资料任务、本体管理、行业模板、图谱、证据问答及分析视图在包内保持模块分工；S0–S7、来源授权、正式发布、实时查询和证据规则继续由匹配版本的 ORION Python 后端执行。插件安装成功不等于这些外部业务链条已经验收。

## 本地私有产物

从项目根目录运行 `node scripts/package_orion_plugin.mjs`，使用输出的本地 tgz 安装。源目录中的 runtime.js 是开发引用；打包器会携带现有 Host 网关和已编译的界面资源，安装者无需读取源码目录。包不含密钥、会话、真实工程状态、用户资料、Python 虚拟环境或官方 Harness 安装。

网页 Profile 可使用 `dsh plugin --profile web add /path/to/package.tgz`。原生桌面先启动完成初始化，再完整退出，用桌面自带的 dsh 命令执行 `plugin --profile desktop add /path/to/package.tgz`，随后重开。替换包代码需要重启；正常启停通过官方插件管理执行。

## 随包技能与模式

安装补丁同时注册工程和本体问答模式，以及 `dsh-orion-workbench/skills` 子入口。子入口使用官方 `@deepseek-ai/dsh-skill-filesystem@0.2.0-rc.2`，以唯一 provider 名称 `orion-workbench-bundled` 注册包内 `skills/`。目录按安装后插件的位置解析，不依赖启动目录、既有 Profile 技能或 `ORION_BACKEND_ROOT`，也不向用户的技能目录复制文件。标准模式的默认技能来源仍保留；卸载/关闭子入口时官方生命周期撤销注册。

`orion-ontology-engineer` 保留 `harness/skills/` 中的原技能及全部引用资料；`orion-ontology-qa` 由当前问答模式 persona 的完整规则生成，工程和问答规则的来源及哈希记录在 `notices/skills-provenance.json`。生成的问答模式要求在回答正式本体问题前加载该技能。技能提供方法和权限边界，实时事实、状态转换、查询执行与正式回执仍依赖另行部署的匹配后端；技能可发现和读取不表示这些业务功能已验收。

该注册方式依据官方 [filesystem skill provider](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/packages/skill/skill-filesystem/README.md) 的 `bundledSkillDir` 契约；工程/问答的原生工具组合依据官方 [standard preset](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/packages/bundle/web-app/presets/standard.patch.yml)。上游模板的 MIT 许可随包保存。

## 后端和权限

插件默认只读。配置 `backendRoot` 为匹配版本的 ORION 后端源码或部署目录，`python` 为该部署的 Python，`workflowHome` 为明确选择的业务数据目录；资料服务、实时问答和专业工具按实际部署分别配置。未配置后端时仍能打开插件，但业务页面会明确报告不可用。

可通过 Profile 覆盖 `orion-workbench` 配置行。环境变量入口包括 `ORION_BACKEND_ROOT`、`ORION_WORKFLOW_PYTHON`、`ORION_WORKFLOW_HOME`、`ONTOLOGY_AGENT_API_URL` 和 `ORION_ACTOR`。凭据由使用者在其环境中配置，不放入插件。切换业务写入需显式配置操作人和数据范围；设计批准、发布及外部操作继续经过既有门禁。

Aqua 液态玻璃是独立的可选界面插件。开启后它覆盖原生布局和工作台的材质；关闭后恢复原生材料。工作台本身不把官方 root 替换成另一套应用。

## 私有分发

`private: true` 用于防止误发布，并非访问控制。当前只生成本地产物，不上传或发布。已建立独立本地 Git 仓库；后续若分发，单独确定仓库权限、安装认证和发布方式。

当前验收记录须分别列出安装、界面、业务、外部回执及未验证项。应以“约定版本和路径中没有未关闭的阻断性缺陷”描述质量，不能承诺绝对没有 bug。
