# ORION plugins for DeepSeek Harness

**ORION 是一套基于 DeepSeek Harness 的企业本体工程与证据问答工作台。** 它把业务资料、数据库来源、本体建设、校验批准和正式版本问答放进同一个工作流程，让使用者能追溯“这个结论依据什么资料、属于哪个发布版本”。

产品由 **两个插件 + 一套配套 Python 服务** 组成，可选择官方 Mac 桌面端或本机 Web 端。工作台负责工程功能，Aqua 负责玻璃与壁纸，Python 服务执行业务规则；官方 Harness 提供会话、模型与通用工具。无需安装额外 AHS 应用。

使用时，从“本体工程发起模板”描述目标并选择资料/数据库来源，在本体中心推进建设、校验和人工批准，完成正式发布后再进行有证据的问答与分析。首次安装没有客户本体数据，需要使用者提供自己的授权资料、模型账号和所需业务服务。

**先说明体验范围：安装成功不等于业务闭环。** 可以先体验工作台和工程入口；真实建设、发布和问答还需要自己的资料、模型账号及相应外部软件。软件用途与官网见 [使用前准备清单](docs/dependencies-and-acceptance.zh-CN.md)。部分环境仍需接入实施，只有实际完成批准、校验、发布和证据问答，才能说明所选业务范围已经跑通。

## 下载与开始使用

- [产品简介：功能、组件与使用流程](docs/product-overview.zh-CN.md)
- [完整安装教程：官方 Mac 桌面、Python 服务、权限与恢复](docs/install.zh-CN.md)
- [外部依赖、下载入口与闭环验收](docs/dependencies-and-acceptance.zh-CN.md)
- [本机 Web 安装教程](docs/web-install.zh-CN.md)
- [让 AI 协助部署：可复制的任务说明](docs/ai-deployment.zh-CN.md)
- [RC10 下载页面](https://github.com/Striv1/dsh-orion-plugins/releases/tag/plugins-v1.0.0-rc.10)：选择具名交付包，页面上的 `Source code (zip)` 不等同于完整安装包。

建议交付 `orion-rc10-delivery-kit.zip`：两个插件、完整 Python runtime 源码、安装辅助文件、上述教程，以及适配版本的官方 Apple 芯片 Mac 安装包。也可以直接将 ZIP 交给使用者，无需开放维护仓库。首次安装仍需联网获取 Python/Node 和依赖。

**当前仓库为私有（2026-10-06）。** 从 GitHub 下载需要登录已获仓库访问授权的账号；没有访问权限时，可直接接收维护者提供的完整 ZIP 和校验文件。仅下载使用不需要授予仓库写入权限。

当前交付面向 **Apple 芯片 Mac 本地部署**。RC10 修复插件反复启停时的工具清理，并改善流体资源释放、视频暂停后恢复、运行环境隔离、后台启动状态及页面加载超时处理。验证范围以 RC10 发布页为准，安装者仍需验收自己的环境和业务链。完整远程多人服务器模式尚未交付。

## RC10 包含什么

| 交付物 | 当前候选 | 职责 |
| --- | --- | --- |
| `dsh-orion-workbench` | `1.0.0-rc.10` | 本体中心、工程发起模板、工程资料、本体管理、行业模板、图谱、正式发布绑定的证据问答和分析；工程/问答技能与预设；本地 Core 的受管生命周期 |
| `dsh-client-ui-aqua` | `1.3.1-orion-alpha.5` | 液态玻璃、流体背景及图片/视频壁纸；保留上游 MIT 许可与来源 |
| `dsh-orion-runtime` | `1.0.0-rc.10` | 配套 Python 业务服务，执行 S0–S7、来源与规则、资料处理、批准/发布及问答证据契约；它不是第三个界面插件 |

当前适配官方 DeepSeek Harness **0.2.0-rc.2**。后续升级须重新验证官方 Slot、Cordis、桌面 Profile、MCP 与进程生命周期，不通过版本豁免强行安装。

## 官方应用和本地安装

第一次部署请按 [完整安装教程](docs/install.zh-CN.md) 或 [Web 教程](docs/web-install.zh-CN.md) 操作，其中有全部命令、备份、完整配置合并和恢复步骤。普通使用者只需完整交付 ZIP，无需取得源码维护仓库。

桌面流程是：校验交付文件 → 安装包内固定版本的官方 App → 准备独立 Python 环境与业务目录 → 用官方 CLI 安装两个插件 → 使用辅助 ZIP 中的脚本生成完整配置 → 合并并启动验收。Web 使用独立官方 CLI、Home 和端口。

配置生成器默认只读，资料写入入口和 MCP 默认关闭。需要建设工程时，按教程设置自己的操作人和本地写入参数；这不授予 GitHub 写入权限，也不绕过业务批准和正式发布。工作台负责启动、停止自己管理的 Core，不另行重复启动同端口后台。

## 服务边界

首次安装允许空工作区和空发布目录。Core 的 `NO_PUBLISHED_RUNTIME` 表示尚无正式发布，不能当作实时问答验收。已发布问答的当前版本库需显式配置 `DATABASE_URL`，不提供部署用户名或密码；空连接串被拒绝，避免隐式连接本机数据库。凭据只属于部署配置，不进入源码、包、日志和回执。

Protégé、Semantica、数据库、存储、查询和 OCR 等能力按实际业务范围接入，外部服务没有全部随包提供；Wren 为可选分析能力，使用独立环境。名称、用途与官网见 [准备清单](docs/dependencies-and-acceptance.zh-CN.md)。当前以 Apple 芯片 Mac 本地部署为验收范围，完整跨平台业务链尚未验收。

[配套 runtime 说明](docs/runtime-install.md) 提供服务环境与契约要点；首次部署请按 [完整安装教程](docs/install.zh-CN.md) 完成宿主、两个插件和服务配置。

## 源码维护和构建

构建需要 Node.js 22 或更高版本，以及 Python 3.11+（标准库 `tomllib` 用于核对发行版本）；依赖由 `package-lock.json` 固定：

```sh
npm ci
npm test
npm run check
node scripts/build_orion_plugins.mjs --pack --output-dir dist/rc10-new-build
```

已有依赖和官方 SDK 时可显式指定它们，构建工具不会将本机路径写入产物：

```sh
node scripts/build_orion_plugins.mjs --runtime-root /path/to/official-runtime --dependency-root /path/to/node_modules --check-only
node scripts/test_plugins.mjs --runtime-root /path/to/official-runtime
```

打包生成两个插件 tgz、完整 Python runtime 源码包、SHA-256、文件清单和构建回执；不自动安装、启动或上传。已交付的同版本包保持冻结，源码修改后应提升候选版本并重新生成两个 `contracts/` 清单和匹配包，避免覆盖旧包。

`contracts/runtime-source-manifest.json` 固定 279 个运行文件及兼容指纹；`contracts/backend-source-fingerprint.json` 与其对应。运行源码、脚本、依赖锁、迁移、模板和报告资源都必须一致。Python 测试另行列入源码包，不能用测试通过替代真实安装、人工批准、业务执行和外部回执。

运行关系：

- `harness/plugins/orion-workbench`：官方 Host/Client、Slot、skills provider 与 runtime-manager。
- `harness/plugins/branded-web-runtime`：工作台调用 Python 工作流的本地 gateway，构建后随插件交付。
- `services/`、`harness/` 中的 Python、`scripts/`、`database/`：受契约约束的业务运行实现。
- `harness/web/assets`：工程、模板、图谱和证据页面；`harness/skills` 与 `harness/profile`：工程/问答方法和预设。
- `harness/plugins/dsh-client-ui-aqua`：独立可选的外观插件。

## 分发和服务器模式

此源码导出不包含既有本机 Git 历史、旧 tag、截图、验收记录、用户 Home、会话、业务资料、数据库、缓存、虚拟环境或凭据。本仓使用独立整理的源码历史，源码和 Release 当前按私有仓库权限访问，也可单独交付完整 ZIP。`private: true` 仅防止误发 npm，不提供仓库访问控制。本仓未启用 npm 发布；私有 npm 包需要另行决定 scope、身份和安装认证。

当前正式候选采用**本地服务**。虽然 runtime-manager 有 `external` 接入配置，现有工作流 gateway 仍需本地源码和 Python，因此不能仅填写远程 URL 就声称支持完整服务器模式。服务器模式还需远程业务 API、认证与权限、资料上传/下载、运行版本配对、任务与日志生命周期、多人隔离、网络错误恢复及实际部署验收。此候选不包含已验证的多用户服务器部署。

## 许可

ORION 自有代码当前仍标记 `UNLICENSED`；仓库可见性与文件交付不改变代码许可。Aqua 保留上游 MIT 和作者来源，官方 preset 保留 DeepSeek MIT 和版本来源；模板保留 IOF MIT、AutoMatCE CC BY 4.0、OpenEPCIS Apache-2.0 等原许可。详情见 [第三方来源说明](THIRD_PARTY_NOTICES.md)。
