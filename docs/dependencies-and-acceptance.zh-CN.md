# 外部依赖与真实闭环验收

适用：ORION 工作台/runtime **1.0.0-rc.8**、Aqua **1.3.1-orion-alpha.4**、官方 Harness **0.2.0-rc.2**；2026-10-05 按发行源码核对。Apple 芯片 Mac 的官方桌面和本机 Web 都适用以下业务条件。

**两个插件 + Python Core 可以独立安装，但不包含完整业务基础设施。** 界面、模板或工具列表可见，只能证明对应入口工作；能否完成本体建设、正式发布和证据问答，还取决于外部工具、真实来源、配置、人工批准及实际执行回执。本指南提供准备和验收路径，不宣称所有专业依赖已经在收件电脑安装或兼容。

## 1. 先选体验范围

| 目标 | 能先做什么 | 完成标准 |
| --- | --- | --- |
| 安装与界面体验 | 安装官方宿主、两个插件与 Core；查看模板、本体中心、图谱入口，设置玻璃或壁纸 | 实际宿主/插件/Core 版本与指纹一致，使用自己的独立 Profile，入口可用；允许无工程、无发布 |
| 资料工程 `DOCUMENT_ONLY` | 用授权资料建立概念、事实及证据 | 格式解析、S5/S6 共同依赖齐备；正式资料发布还需工作流 PostgreSQL、MinIO、Fuseki；不要求 Ontop/业务数据库映射 |
| 数据库工程 `DATABASE_ONLY` | 以只读数据源画像、映射和查询为核心 | 只读来源、S5/S6 共同依赖、Docker/Ontop 候选与部署验证、当前版本登记齐备；通常不要求 OCR 或资料索引链 |
| 联合工程 `HYBRID` | 结合文档证据与业务数据 | 两条链均通过，实体/资料关联经过审阅，查询绑定同一正式版本 |

共同依赖包括：自己的模型账户及操作人、真实来源授权、正式 S5 的 Protégé/MCP/HermiT、当前受管 S6 的 Semantica 环境。默认 S7 还要求 Semantica 本体同步。部分没有规则推理要求的 CQ 可经审阅记为 `NOT_APPLICABLE`，这不取消受管入口的运行条件或其他门禁。

## 2. 哪些随包提供，哪些要另外安装

随包提供官方 Apple 芯片 Mac DMG、两个插件、完整 ORION Python 服务源码、锁定的 Core 依赖描述、工作流脚本、迁移文件、模板、安装辅助文件及教程。首次按教程联网安装 Python 依赖。**Python SDK 不等于运行中的数据库、对象存储、OCR 或图服务。** 不提供模型账户、客户数据、外部服务密码或既有业务结果。

| 软件或服务 | 用在哪里、缺了会怎样 | 准备与配置入口 |
| --- | --- | --- |
| 模型账户/额度 | Agent 工程交互；无可用账户不能执行真实模型请求 | 在官方 Harness 配置自己的账户/模型，做一次真实请求验证；下载仓库不附送额度 |
| Swift / PDFKit | 当前 Mac PDF 文字层提取和页面处理；缺编译工具无法构建辅助程序 | 先运行 `xcrun --find swiftc`；缺失时按 Apple 工具安装流程准备 Command Line Tools；这不提供扫描件 OCR |
| PaddleOCR MCP | 扫描 PDF、图片或低质量文字层解析；缺少时不能假装已有文字证据 | [PaddleOCR 官方项目](https://github.com/PaddlePaddle/PaddleOCR)。分别接入结构/识别 MCP：`ORION_PADDLEOCR_STRUCTURE_URL`、`ORION_PADDLEOCR_OCR_URL`（默认本机 10826/10827）；需匹配 `pp_structurev3`、`ocr` 实际工具/结果合同，普通 OCR URL 不保证兼容 |
| Protégé + MCP 扩展、HermiT 与 JVM | 正式 S5 建设及推理/校验；只有普通 Protégé 窗口仍不足以执行 | [Protégé 官方下载](https://protege.stanford.edu/software/)、[MCP 扩展上游](https://github.com/hakjuoh/protege-mcp)。设置 `PROTEGE_CONSTRUCTION_APPLICATION`、`PROTEGE_ROUTING_FILE`、`PROTEGE_MCP_SECRET`；需要兼容 JAR、broker、construction 角色、令牌与真实工具回执 |
| Semantica CLI / Python / MCP / REST | 当前受管 S6 的前置；声明的规则推理、默认 S7 模型同步及相应问答 | [Semantica 项目](https://github.com/semantica-agi/semantica)、[MCP 文档](https://github.com/semantica-agi/semantica/blob/main/docs/reference/mcp_server.md)。配置 `SEMANTICA_CLI`、`SEMANTICA_PYTHON`、`SEMANTICA_MCP_COMMAND`、`SEMANTICA_ACTIVE_RUNTIME_CONFIG`，规则服务用 `SEMANTICA_API_URL/SEMANTICA_API_KEY`；准备脚本只建目录，不安装服务或生成有效运行选择 |
| 源数据库与只读账户 | DATABASE_ONLY/HYBRID 的来源画像、映射、S6 对账与查询 | 使用自己的 PostgreSQL/MySQL 等受支持来源；配置 `ORION_SOURCE_DATA_READER_URL`、登记 SourceBinding 表/列授权范围；禁止用应用写账户替代只读源账户 |
| Chat2DB MCP / CLI | 当前原生数据库选择器需要 MCP；`chat2db://` 来源采集还需兼容 CLI | [Chat2DB 官方入口](https://chat2db.ai/)。在本 Harness Profile 接入 MCP，实测 `mcp__chat2db__list_all_datasources` 等调用；CLI 用 `ORION_CHAT2DB_CLI`，必须符合代码要求的 JSON 合同。底层可用 PostgreSQL/MySQL 直接 reader，但这不自动替换原生选择器 |
| PostgreSQL 工作流/当前版本库 | 当前已发布 QA runtime 需要非空数据库连接；资料 S7 发布也需要当前资料版本登记 | [PostgreSQL 官方下载](https://www.postgresql.org/download/)。准备独立业务库、权限及包内 `database/migrations/orion_workflow/` 迁移；`DATABASE_URL` 和 `ORION_WORKFLOW_DATABASE_URL` 应指向同一受控工作流库。不要把库初始化直接施加到未知既有库 |
| Docker + Ontop + JDBC | 结构化 S6 候选校验与 S7 部署；没有可用镜像/网络/驱动则不能验证实时来源 | [Docker Mac 安装](https://docs.docker.com/desktop/setup/install/mac-install/)、[Ontop 官方 CLI/部署说明](https://ontop-vkg.org/guide/cli.html)。显式配置 `ORION_ONTOP_IMAGE` 与 `ORION_DOCKER_NETWORK`；确认镜像摘要、JDBC 驱动与只读来源连接。官方基础镜像不自动等价于项目所需的受管运行合同 |
| Fuseki + MinIO | 当前 DOCUMENT_ONLY/HYBRID 的正式资料发布：原件存储、命名图检索与版本回读 | [Jena Fuseki 官方文档](https://jena.apache.org/documentation/fuseki2/)、[MinIO 项目](https://github.com/minio/minio)。准备数据集、对象桶、账号和最小权限；配置 `FUSEKI_URL`、`MINIO_ENDPOINT/ROOT_USER/ROOT_PASSWORD/SECURE`、`ORION_ARTIFACT_BUCKET`。S0 原件快照可关闭，不代表正式资料发布可以缺 MinIO |
| Wren 独立 Python 环境 | 可选结果分析与项目能力；不属于每个基础工程都必须启动的服务 | 按包内 `scripts/requirements-wren.txt` 安装锁定版本到 `.venvs/wren`，配置 `ORION_WREN_PYTHON`；该适配接收有绑定回执的有限结果集，不等于安装整套 WrenAI Web 产品 |

截至核对日，MinIO 社区仓库已归档并声明停止维护；这里保留它作为现有适配来源，不将其浮动最新版作为新部署建议。实际对象存储版本或替代方案需要部署者评估，并通过现有原件存取、权限和发布回读合同验证。

这些链接是上游获取入口，**不是 ORION 对上游任意最新版的兼容承诺**。部署者需记录所选版本、校验值和实际接入结果。不要把他人电脑的路径、密钥、路由文件或容器网络名称复制过来。

可选 Wren 环境按已安装教程的变量约定准备：

```sh
uv venv --python 3.12 "$ORION_PROFILE_ROOT/.venvs/wren"
uv pip sync --python "$ORION_PROFILE_ROOT/.venvs/wren/bin/python" \
  "$ORION_RUNTIME_ROOT/scripts/requirements-wren.txt"
```

仅在该目录尚不存在时创建；随后将其 Python 绝对路径写入对应 Profile 的 `ORION_WREN_PYTHON`。当前精简 Makefile 没有 `wren-runtime` 目标。依赖安装成功后仍需验证实际分析回执，不能混入 Core 环境。

## 3. 当前未完成的交付部分

这几项需要接入实施，不能给收件人一个上游下载链接就当作完成：

- **Protégé 适配发行**：专用 MCP JAR/broker、令牌初始化、角色绑定与兼容版本组合未全部随包提供；S5 脚本默认还检查特定 construction App 身份。普通官方 App 不自动满足该身份与工具合同。
- **Semantica 安装与选择配置**：兼容 CLI/MCP/REST 组合及有效 `active-runtime.json` 需要准备并验证，ORION 的目录初始化不代办这些内容。
- **Ontop 自有镜像与网络**：源码默认 `ontology-workorder-agent/ontop:5.3.0` 和旧项目网络名；发行仓内没有配套 Dockerfile/Compose 构建资料。不能假设新机器能拉取该镜像或已有该网络。没有明确镜像来源和身份回读前，数据库闭环记为阻塞。
- **OCR/Chat2DB 服务合同**：兼容服务安装配置和真实工具读回需要另行落实，不能把产品名称相同视为合同一致。
- **PostgreSQL/Fuseki/MinIO 初始化**：需建立本部署自己的库、迁移、数据集、桶和账号权限，并完成连通及版本回读；包内不存在完整一键基础设施部署。

因此当前推荐先安装体验，再选一个有负责人、可核对人工预期的小范围业务工程补齐接入。尚未补齐上述条件时，准确说明阶段和缺口；不承诺新机器已能跑通 S0–S7。完整远程多人服务器模式也未交付。

## 4. 把配置接到正在运行的 Profile

安装教程的配置生成器默认只读、资料写入入口与 MCP 关闭、数据库为空、自动发布关闭。它用于安全建立自有空环境。若要建设工程，按教程用自己的稳定操作人标识生成完整配置并启用所需入口；S4 与 S7 的人工批准仍须按工程分别完成。

**外部环境参数应显式进入 `runtime-manager.environment` 和实际调用这些能力的 MCP 配置 `env`，并与工作台使用同一个业务 Profile。** 受管进程会清理环境继承；仅在终端 `export` 不能证明从 Finder 启动的官方 App 得到了配置。合并时保留生成器所需的四项完整配置和原有其他条目，Cordis 配置是整项替换；不要覆盖用户整份 YAML。受管模式会将 `SEMANTICA_ACTIVE_RUNTIME_CONFIG` 派生为 `<ORION_PROFILE_ROOT>/state/semantica/active-runtime.json`，应在这个派生位置准备有效配置并核对 MCP 使用的选择，不以任意终端路径覆盖它。

| 参数 | 作用与核对方式 |
| --- | --- |
| `DATABASE_URL` | 问答当前资料版本注册库；与实际服务配置、schema 和权限核对 |
| `ORION_WORKFLOW_DATABASE_URL` | 工作流元数据与资料发布库；与问答侧保持同一受控版本事实 |
| `ORION_SOURCE_DATA_READER_URL` | 授权业务来源的只读连接；与工作流写库分开，不回退到应用写账户 |
| Protégé / Semantica / OCR / Ontop / Fuseki / MinIO 参数 | 指向本次部署自己的路径、服务、实例与凭据；读取正确身份后再执行获授权的能力检查 |

配置文件和凭据只保存在部署者本机受限目录。验收报告记录服务身份、版本、作用及脱敏结果，不记录密码/令牌/完整带密钥连接串。不能通过关闭必要同步、修改工作流状态、伪造批准或编造外部工具回执让检查“变绿”。

## 5. 如何取得可复核的闭环

建议先选一个规模小、真实获授权、有人工预期答案的工程。提前列出代表性问题，以及正例、反例、空结果、未知和边界情况的预期；业务负责人确认来源范围和验收责任。

| 步骤 | 实际要做的事 | 必须留下的证据 |
| --- | --- | --- |
| 安装验收 | 在自己的 Home/Profile 启动官方宿主、插件与 Core，查看模板和实际版本 | 宿主/插件/Python distribution 版本、runtime 指纹、实际路径与后台身份；空环境 `NO_PUBLISHED_RUNTIME` 如实记录 |
| 来源与语义 S0–S3 | 导入授权资料或登记只读来源；核对解析、范围、画像、概念及映射 | 来源/资料指纹、行数/字段/页码、解析或查询回执、来源范围、审阅决定；模式不适用的阶段也由工作流记录原因 |
| 设计批准 S4 | 审阅概念、关系、映射、规则和验收问题，冻结设计基线 | 人工批准人与时间、批准对象及基线指纹；模型建议不是人的批准 |
| 构建 S5 | 按工程版本真实调用 Protégé/HermiT 等构建和校验 | 本体文件及哈希、工具实例和构建/推理回执、不可满足类与验证结果；不是单独一个“工具存在” |
| 质量验收 S6 | 实际对账、约束/规则执行、结构化 Ontop 候选与各类 CQ 验证 | 来源对账、候选运行身份、实际执行结果、问题用例回执和失败解释；合成单元测试不替代客户数据 |
| 正式发布 S7 | 对指定不可变版本取得人工批准，执行相应部署/同步/资料晋升并回读 | release ID/指纹、批准、部署与同步回执、正式 runtime 注册；复制文件或端口健康不是发布成功 |
| 发布后问答 | 用真实问题查询已发布版本并核对人工预期；含未知/越权范围的合理拒绝 | 答案、同一 release 指纹、来源/查询/文档版本证据、完整结果与解释；需能追溯到所批准的版本 |

只有所选业务范围的这些结果均真实取得、异常得到解释，才能在该范围内写“闭环验收通过”。不能对所有场景或任意数据保证零错误。某项未执行、未配置或失败，就分别标为“未验证”“缺依赖”“失败”，写明下一步和责任人。

以下都不单独代表业务闭环：进程在线、HTTP 200、工具列表、模板存在、图谱可见、单元测试通过、模型自述完成。Aqua 外观验收与业务验收也分别记录。

## 6. 给实施人员或 AI 的交付记录模板

```text
目标与模式：界面体验 / DOCUMENT_ONLY / DATABASE_ONLY / HYBRID
运行环境：宿主、插件、Core/Python 版本与指纹；独立 Home/Profile/端口
授权范围：操作人、资料/数据源、可读取范围、业务负责人
依赖逐项：所选版本与身份 / 配置位置 / 实际验证 / 缺口与下一步
阶段结果：S0–S7 逐项结果、实际回执位置、批准对象与人员
发布后问答：问题、人工预期、实际答案、release 指纹与来源证据
尚未验证：缺依赖 / 未执行 / 失败 / 不适用及理由
维护交接：启动、停止、备份、恢复；凭据由部署者自己保管
```

先读 [桌面安装教程](install.zh-CN.md) 或 [Web 教程](web-install.zh-CN.md)；需要 AI 协助时使用 [部署任务说明](ai-deployment.zh-CN.md)，把本指南同时交给它。

本指南的 ORION 适配判断来自本候选源码：`Makefile`、`scripts/protege_role_router.py`、`scripts/run_protege_build_stage.py`、`scripts/semantica_runtime_config.py`、`scripts/run_quality_validation_stage.py`、`services/ingestion/s0_batch_executor.py`、`services/realtime_qa/deployment_automation.py`、`services/realtime_qa/runtime.py`、`harness/plugins/orion-workbench/lib/runtime-manager.js` 及 `harness/plugins/branded-web-runtime/lib/chat2db-catalog-api.js`。上游软件文档只证明其自身安装与接口说明，不代替本项目兼容验收。
