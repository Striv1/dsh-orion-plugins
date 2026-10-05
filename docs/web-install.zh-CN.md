# ORION RC7：本机 Web 端与后台服务安装

本教程与 [公开下载及桌面教程](install.zh-CN.md) 配套。使用同一批两个插件、Python runtime 和安装辅助 ZIP；选择 Web 无需先安装 DeepSeek Harness 桌面 App。

适用范围：Mac 本机浏览器，官方 Harness **0.2.0-rc.2**，工作台/runtime **1.0.0-rc.7**，Aqua **1.3.1-orion-alpha.4**。Web 默认只监听本机，不是已经验收的公网或多人服务器产品。

## 一、下载文件与目录

直接收到 `orion-rc7-delivery-kit.zip` 和 `SHA256SUMS-delivery-kit.txt` 时，先按主教程第四节校验外层 ZIP，再解压得到 `ORION-RC7` 文件夹，放到自己的 `~/Downloads/`。也可以从 RC7 公开下载页直接下载这两个文件，无需 GitHub 账号或邀请。包内官方 DMG 为桌面用户准备，纯 Web 路径无需安装它。继续检查：

```sh
cd "$HOME/Downloads/ORION-RC7"
shasum -a 256 -c SHA256SUMS-download.txt
shasum -a 256 -c SHA256SUMS-rc7.txt
```

全部 `OK` 后，在同一终端定义本次 Web 的路径：

```sh
export ORION_DOWNLOAD_ROOT="$HOME/Downloads/ORION-RC7"
export ORION_BASE="$HOME/ORION"
export ORION_INSTALLER_ROOT="$ORION_BASE/installer-support"
export ORION_RUNTIME_ROOT="$ORION_BASE/runtimes/dsh-orion-runtime-1.0.0-rc.7"
export ORION_PROFILE_ROOT="$ORION_BASE/profiles/web-local"
export ORION_WEB_HOME="$ORION_BASE/homes/web"
export ORION_WEB_SDK="$ORION_BASE/harness/0.2.0-rc.2"
mkdir -p "$ORION_BASE/runtimes" "$ORION_BASE/profiles" "$ORION_BASE/homes" "$ORION_WEB_SDK"
```

Web Home、Core Profile、SDK 和 runtime 要分开放置，不使用符号链接路径。如果还安装了桌面端，保留独立的 `profiles/local`，不让两个入口共用同一个可写 Profile。相同冻结版本的 runtime 源码可以复用。

仅当相应目标目录尚不存在时解压；若已经按桌面教程解压并校验过，则跳过：

```sh
tar -xzf "$ORION_DOWNLOAD_ROOT/dsh-orion-runtime-1.0.0-rc.7.tar.gz" \
  -C "$ORION_BASE/runtimes"
ditto -x -k "$ORION_DOWNLOAD_ROOT/orion-rc7-install-guide.zip" "$ORION_BASE"
```

## 二、安装 Web 宿主和 Python 环境

### 2.1 Node 与官方 Harness

Web 路径需要独立 Node.js 和 pnpm；桌面自带的私有运行时不作为这里的 Web SDK。

从 [Node.js 官方入口](https://nodejs.org/en/download) 安装 Node **22 LTS**。本地隔离验收采用 22.23.2。终端确认 `node --version` 为 v22.x，`npm --version` 可用；不要替换其他项目明确固定的 Node 环境。

将官方 Harness 和 pnpm 安装到这个独立 SDK 目录，避免修改全局安装：

```sh
npm install --prefix "$ORION_WEB_SDK" --save-exact \
  @deepseek-ai/dsh@0.2.0-rc.2 pnpm@11.19.0
export PATH="$ORION_WEB_SDK/node_modules/.bin:$PATH"
export ORION_WEB_NODE="$(node -p 'require("node:fs").realpathSync(process.execPath)')"
export ORION_DSH_ENTRY="$ORION_WEB_SDK/node_modules/@deepseek-ai/dsh/lib/bin.js"
"$ORION_WEB_NODE" "$ORION_DSH_ENTRY" --version
pnpm --version
```

应分别是 `0.2.0-rc.2` 和 `11.19.0`。普通 Web 的插件管理需要在 PATH 中找到 pnpm，仅安装 `@deepseek-ai/dsh` 不保证 pnpm 命令存在。首次需要网络；官方原生依赖若要求安装脚本授权，应先核对提示的包再按官方流程处理。

官方 Web 的启动与插件管理机制见 [固定版本 CLI 文档](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/apps/cli/README.md)。

### 2.2 Core Python 环境

按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv。不要复用桌面端可写的 Core Profile，也不把 Wren 安装进 Core。

```sh
uv python install 3.12
uv venv --python 3.12 "$ORION_PROFILE_ROOT/.venvs/core"
UV_PROJECT_ENVIRONMENT="$ORION_PROFILE_ROOT/.venvs/core" \
  uv sync --project "$ORION_RUNTIME_ROOT" --locked
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_RUNTIME_ROOT/scripts/orion_runtime_manifest.py" \
  --root "$ORION_RUNTIME_ROOT" --check
```

预期 runtime 为 RC7、279 个文件、指纹 `7964a84a604a94dbb7d8e45e5b09a4ead7997ac68e77c42624785bd47e2690f3`。

本教程使用 Web **3092**、Core **8092**。先分别检查：

```sh
lsof -nP -iTCP:3092 -sTCP:LISTEN
lsof -nP -iTCP:8092 -sTCP:LISTEN
```

没有监听结果后再准备；已占用则选空闲端口，并同步修改后续命令。不要为了安装结束不属于自己的进程。

```sh
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_RUNTIME_ROOT/scripts/prepare_orion_runtime.py" \
  --runtime-root "$ORION_RUNTIME_ROOT" \
  --profile-root "$ORION_PROFILE_ROOT" \
  --python "$ORION_PROFILE_ROOT/.venvs/core/bin/python" \
  --port 8092
```

## 三、安装两个插件到独立 Web Profile

确保这个 Web Home 没有正在运行的宿主，执行：

```sh
DSH_HOME="$ORION_WEB_HOME" "$ORION_WEB_NODE" "$ORION_DSH_ENTRY" \
  plugin --profile web add \
  "$ORION_DOWNLOAD_ROOT/dsh-orion-workbench-1.0.0-rc.7.tgz"
DSH_HOME="$ORION_WEB_HOME" "$ORION_WEB_NODE" "$ORION_DSH_ENTRY" \
  plugin --profile web add \
  "$ORION_DOWNLOAD_ROOT/dsh-client-ui-aqua-1.3.1-orion-alpha.4.tgz"
```

官方 CLI 会初始化这个新 Home 的 `profiles/web`。它与桌面流程不同，不要求先打开桌面 App。若是在已有 Web Home 上安装，先停止它并保留 Home/业务 Profile 的备份，不覆盖既有登录、工作区和会话。

## 四、生成并应用四条完整业务配置

配置生成器原名为 `configure_orion_desktop.py`，但它输出的四个插件配置对象也适用于 Web：业务路径来自准备好的独立 Core Profile，没有桌面 IPC 字段。**脚本只输出方案；它在终端提示的 desktop 目标文件不用于本节。这里应合并到 `profiles/web/cordis.patch.yml`。**

需要在自己电脑建设工程时，将操作人标识替换为自己的值，执行：

```sh
export ORION_OPERATOR_ID='请替换为你的操作人标识'
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_INSTALLER_ROOT/scripts/configure_orion_desktop.py" \
  --environment "$ORION_PROFILE_ROOT/runtime-environment.json" \
  --dsh-home "$ORION_WEB_HOME" \
  --allow-writes --actor "$ORION_OPERATOR_ID" \
  --enable-document-ingestion --enable-workflow-mcp --enable-realtime-mcp \
  > "$ORION_PROFILE_ROOT/web-overrides.json"

"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B -c \
  'import json,sys,yaml; print(yaml.safe_dump(json.load(open(sys.argv[1])),allow_unicode=True,sort_keys=False),end="")' \
  "$ORION_PROFILE_ROOT/web-overrides.json" \
  > "$ORION_PROFILE_ROOT/web-overrides.yml"
```

只做只读界面验收时，生成器仅保留 `--environment` 与 `--dsh-home`，不加写入/MCP 开关。此时无法创建工程是预期行为。GitHub Read 权限与这些本地业务选项没有关联。

备份目标文件，在纯文本编辑器中将四条完整覆盖项合并到：

```text
~/ORION/homes/web/profiles/web/cordis.patch.yml
```

合并规则与 [主教程第七节](install.zh-CN.md#七生成完整配置并合并) 相同：空列表可替换；已有其他条目必须保留；同 ID 整项替换且不重复；不能把整个 JSON 数组直接追加到 YAML 后，也不能只拷贝部分 `config` 字段。

四项是 `orion-runtime-manager`、`orion-workbench`、`mcp-orion-workflow`、`mcp-orion-realtime`。生成配置始终关闭 S7 自动部署，真实写入仍经过业务授权、阶段批准和正式发布规则。

## 五、启动 Web，使用自己的账号

先进入自己的资料工作目录，避免把程序源码目录设为默认工作区：

```sh
cd "$ORION_PROFILE_ROOT/state/references"
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_RUNTIME_ROOT/scripts/launch_orion_harness.py" \
  --environment "$ORION_PROFILE_ROOT/runtime-environment.json" \
  --dsh-home "$ORION_WEB_HOME" \
  web --node "$ORION_WEB_NODE" --dsh-entry "$ORION_DSH_ENTRY" \
  --port 3092 --no-open
```

保留终端窗口运行。从**当前启动输出**打开官方给出的本机登录链接。新浏览器直接打开裸 `http://127.0.0.1:3092/` 可能返回 401，这是缺少当前浏览器会话认证；不应因此关闭认证。首次链接可能带启动令牌，正常登录后官方会清除地址栏中的令牌参数。该链接只在自己电脑使用，不贴到聊天、截图或交给别人。

首次可能出现官方预览版说明，阅读后点 Continue 进入。新 Home 可能默认英文，在官方设置选择中文；Aqua 的背景和材质参数也需要按自己的偏好设置，不会继承维护者的个人设置。

Web Home 不会自动继承桌面账号或维护者凭据。在 Web 官方设置里配置自己的账号/模型。后台服务由工作台管理，不能同时手动启动另一个 8092。

日常停止时，在这个启动终端按 **Ctrl+C**，等宿主和它管理的 Core 退出。要保持本机 Web 运行，应保留启动进程；浏览器关页与关闭宿主进程不是同一件事。以后启动重用本节命令，不重复创建 Python 环境、执行 prepare 或覆盖配置。换终端时需先恢复第二节的环境变量和 PATH。

## 六、检查结果与限制

启动后按主教程第八节检查本体中心、工程模板、Logo、Aqua、资料引用和插件开关。Web 的文件选择发生在浏览器/Host 环境，不以“出现原生桌面文件选择器”为验收要求。

另开一个终端可只读检查 Core：

```sh
curl --fail --silent --show-error 'http://127.0.0.1:8092/health'
```

空目录的 `NO_PUBLISHED_RUNTIME` 表示没有正式发布，本体问答暂不可用；不等同于服务错误，也不等同于业务验收通过。完整后台源码、外部业务依赖、Core/Wren 隔离及 S0–S7 验收要求见主教程第九节。

本版本本地 Web 默认绑定 `127.0.0.1`。不能把本教程的地址直接改成公网地址就对外提供多人服务，也不关闭认证来让其他机器访问。完整服务器模式尚需补齐远程工作流 API、权限、资料传输、多人状态隔离、任务恢复及业务验收。

验证范围：此前 RC6 在独立空 Web Home 中完成官方 CLI 安装、Web/Core 启动及首页 Logo、本体中心导航、工程模板检查。RC7 的具体复核记录见公开发布页。新机器仍应逐项检查本节列出的功能；没有执行的模型调用、本地写入、Aqua 个性化或客户业务链不得标记为通过。
