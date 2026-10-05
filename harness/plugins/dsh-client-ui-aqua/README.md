# ORION Aqua 界面插件

- 上游项目：[DSH-Transparent-UI-Plugin](https://github.com/WYH66666666/DSH-Transparent-UI-Plugin)
- 固定版本：`dsh-client-ui-aqua@1.3.1-orion-alpha.4`，当前私有候选禁止直接发布。
- 上游许可：MIT，原始许可见 [LICENSE](LICENSE)
- 正式配置目标仍是 DeepSeek Harness `0.1.7-rc.2`，以[运行声明](../../runtime/deepseek-harness.json)为准；本轮增加精确候选 `0.2.0-rc.2`，不以此宣称其他 0.2 版本兼容。
- 源码核对：2026-09-20。运行验证以[验证记录](../../../docs/current-project/verification.md)为准；本说明不重复记录端口和进程状态。

本地只做与当前工作台直接相关的兼容和产品化调整：

1. 用 Alpha 的 `@deepseek-ai/dsh-client-store` 替换已删除的 `@deepseek-ai/dsh-client-runtime/client`，避免客户端模块表加载失败。
2. 插件独立注册到 `settings.plugins.tab`，入口为“设置 → 内置插件 → 界面插件”；它不是 MCP，也不进入 MCP 管理。
3. 首次安装默认关闭。构建层仍由 `ORION_ENABLE_AQUA` 决定是否装载插件，用户开关只写浏览器本地 `localStorage`，不改变服务端配置和业务数据。
4. 所有总开关和材质参数都集中在“界面插件”页签；关闭时隐藏参数并立即卸载 Aqua 视觉层，恢复原生 ORION。
5. 主题、浮动对话框标题和树节点统一使用工作台的 `--owa-font-stack` 字体，移除额外的 Space Grotesk 内嵌字体与专用样式注册；保留 WebGL 流体背景和鼠标辉光。
6. 上游品牌鱼形图、粒子鲸鱼、鱼群、气泡和浮游粒子的渲染代码、样式、设置项与状态字段已移除；互动网格默认关闭，用户可按需开启。
7. 由 `prepare_frontend.mjs` 固定复制本目录，不在每次启动时联网解析最新版本。
8. 工程中心、S0-S7、实时状态、报告入口和所有工程弹框统一使用 Aqua 语义主题变量，浅色不出现脱离流体背景的纯白页面，深色不出现白色卡片或按钮残留。
9. 壁纸图片与视频统一使用嵌入式浏览器可用的文件输入；选择后先校验格式、大小和解码能力，视频保存到 IndexedDB，界面明确显示处理中、成功或失败，不再静默吞掉文件选择和存储错误。
10. 勾选图标同时兼容新版的 `IconCheckOutlineRegular` 与旧版的 `IconCheckOutline16`，避免设置页因为删除的组件导出而崩溃。原生桌面各背景层的材质调整以隔离界面验收为准。

后续 DeepSeek Harness 升级时，按[独立产物流程](../../../docs/deployment/harness-artifact-workflow.md)选择隔离 Home 与端口（预览工具排除 3081/3082），先核对 `dsh.client.inject`、`settings.plugins.tab` 和 `defineStore` 契约，再验证：启动、默认关闭、设置页签、开关回退、壁纸图片、壁纸视频、刷新后恢复、浅色/深色、工程中心、会话切换和长列表滚动。通过后才能修改 3081 的构建开关。

修改源码目录，不手改运行 Home 或共享 Harness 安装。预览流程只明确支持已列出的固定版本；旧 peerDependencies 范围不证明其他版本兼容。上游浮动安装器不是本项目部署入口，不能覆盖本地适配。浏览器 localStorage/IndexedDB 偏好不属于服务器账号配置。主题继续遵守[主题合同](../../web/UI_THEME_CONTRACT.md)。
