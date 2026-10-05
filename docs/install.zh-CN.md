# ORION RC7：完整交付、桌面/Web 与本地服务安装教程

核对日期：2026-10-05。适用于官方 DeepSeek Harness 0.2.0-rc.2、工作台/runtime 1.0.0-rc.7、Aqua 1.3.1-orion-alpha.4。

目标：维护者交付完整版本包；使用者在自己的 Mac 上选择桌面或 Web 入口，部署配套服务，使用和保存自己的业务资料。

**当前可以直接从 [RC7 公开下载页](https://github.com/Striv1/dsh-orion-plugins/releases/tag/plugins-v1.0.0-rc.7) 下载 `orion-rc7-delivery-kit.zip` 和 `SHA256SUMS-delivery-kit.txt`，无需登录、添加协作者或接受邀请。** 也可以直接接收维护者提供的这两个文件。解压后得到 `ORION-RC7` 文件夹，放到自己的 `~/Downloads/`。

包内已经准备官方 Apple 芯片 Mac 安装包；首次安装仍需联网获取 Python/Node 和依赖，Web 还需获取官方 CLI。这不是全部依赖都内置的断网安装包。

先了解功能可看 [产品简介](product-overview.zh-CN.md)；让具有本机操作能力的 AI 协助，可使用 [AI 部署任务说明](ai-deployment.zh-CN.md)。

| 使用方式 | 阅读路径 |
| --- | --- |
| 官方 Mac 桌面端 | 按本文第四节至第八节安装 |
| 本机浏览器 Web 端 | 先读本文版本与下载说明，再按 [Web 安装教程](web-install.zh-CN.md) 安装；无需先安装桌面 App |
| 后台服务源码与业务依赖 | 两种入口都需要同一 RC7 runtime；见第五节、第九节 |

两个入口可以自由选择。若同时安装，使用不同 Home、业务 Profile 和端口，不让两个宿主同时占用同一后台数据目录。

## 一、公开下载与写入权限

仓库当前公开：使用者可直接读取源码、查看提交历史、下载 Release，也可复制到自己的电脑或 GitHub 副本。公开并不授予对本仓库的推送、合并或 Release 修改权限，无需为了下载添加协作者。[GitHub 仓库可见性说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)

GitHub 写入权限与本地业务写入是两回事。使用者可按第七节为自己的本地工程启用资料导入和建设功能，这不会让其获得本仓库写权限，也不会绕过人工批准或业务发布规则。

## 二、交付文件

在 [RC7 Release](https://github.com/Striv1/dsh-orion-plugins/releases/tag/plugins-v1.0.0-rc.7) 下载完整 ZIP 和配套 SHA-256 文件。不要选择页面自动生成的 `Source code (zip/tar.gz)` 代替完整安装包。ZIP 内包含：

| 文件 | 用途 |
| --- | --- |
| `dsh-orion-workbench-1.0.0-rc.7.tgz` | 工作台插件 |
| `dsh-client-ui-aqua-1.3.1-orion-alpha.4.tgz` | 玻璃、流体和壁纸插件 |
| `dsh-orion-runtime-1.0.0-rc.7.tar.gz` | 配套本地 Python 服务源码 |
| `SHA256SUMS-rc7.txt` | 三个冻结包的原始校验值 |
| `orion-rc7-install-guide.zip` | RC7 原有配置生成器及最小依赖脚本、教程 |
| `official/deepseek-harness-0.2.0-rc.2-mac-arm64.dmg` | 官方 Apple 芯片 Mac 安装包，保留原文件与签名 |
| `official/来源与校验.md` | 官方来源、版本、架构、签名与校验信息 |
| `README-先读.txt`、四份中文说明 | 产品简介、桌面/Web 教程和 AI 部署任务 |
| `SHA256SUMS-download.txt` | 目录内交付文件的校验清单 |

辅助 ZIP 不是第四个插件。runtime tar.gz 原本不含 `configure_orion_desktop.py`；辅助 ZIP 提供同一 RC7 tag 的配置生成器、启动和指纹脚本，不必再下载整个源码仓库。脚本只生成配置方案，不自动改 Profile、启动应用或获取账号密钥。

## 三、使用者：确认安装范围

当前交付范围是 **Mac 本地部署：官方桌面或官方 Web 宿主 + 两个插件 + 一套本地后台**。桌面流程在本文，Web 使用独立教程。优先在 Apple 芯片 Mac 上安装；Windows/Linux 的完整业务链尚未验收，当前不能直接把本教程照搬为服务器或多人部署。

| 项目 | 本教程版本/要求 |
| --- | --- |
| DeepSeek Harness 官方 App | **0.2.0-rc.2** |
| 工作台 | **1.0.0-rc.7** |
| Aqua | **1.3.1-orion-alpha.4** |
| ORION Python runtime | **1.0.0-rc.7** |
| Mac | Apple 芯片；官方 App 声明最低 macOS **13.0**，系统和权限仍按官方安装要求检查 |
| Core Python | 建议 **3.12**；包声明允许 3.11–3.13 |
| Python 环境工具 | uv |
| 模型访问 | 使用者自己的可用 DeepSeek 账号或 API 配置 |

这些包不含维护者的账号、模型额度、API Key、历史会话、本体工程或数据库。下载权限不等于模型服务授权。首次业务目录为空是正常状态。

当前工作台锁定宿主 0.2.0-rc.2。如果官网日后提供更高版本，先核对兼容范围，不要使用版本豁免强行安装，也不要把已有新版 Home 原地降级。

## 四、下载安装文件和官方 App

### 4.1 下载并核对附件

直接收到完整 ZIP 和校验文件时，先将两者放到自己的 `~/Downloads/`。若通过 GitHub 下载，直接打开 **RC7 公开下载页**，下载 `orion-rc7-delivery-kit.zip` 和 `SHA256SUMS-delivery-kit.txt`。先检查外层 ZIP：

```sh
cd "$HOME/Downloads"
shasum -a 256 -c SHA256SUMS-delivery-kit.txt
```

显示 `OK` 后解压，得到：

```text
~/Downloads/ORION-RC7/
```

直接接收完整 ZIP 不需要 GitHub 账号。公开下载也无需 GitHub 写入权限。Release 页面自动显示的 **Source code (zip/tar.gz)** 只是那个仓库的源码快照，不是插件安装包；安装应使用上述具名附件。

打开 Mac“终端”，运行：

```sh
cd "$HOME/Downloads/ORION-RC7"
shasum -a 256 -c SHA256SUMS-download.txt
shasum -a 256 -c SHA256SUMS-rc7.txt
```

全部应显示 `OK`。如果缺文件或校验不一致，重新下载对应附件，不继续安装。

### 4.2 安装官方 App

包内安装文件为 `official/deepseek-harness-0.2.0-rc.2-mac-arm64.dmg`，无需再找最新版。其原始来源是 [DeepSeek 官方固定版本下载](https://download.deepseek.com/dsh-desk/bin/mac-arm64/deepseek-harness-0.2.0-rc.2-mac-arm64.dmg)，官方入口为 [DeepSeek Harness](https://www.deepseek.com/harness/)。2026-10-05 已核对 DMG 完整性、应用版本/架构、签名与系统评估；详细记录见包内 `official/来源与校验.md`。

1. 打开上述 DMG，把 **DeepSeek Harness.app** 拖入 Mac“应用程序”。如果已有其他版本，先停下核对兼容性与备份，不覆盖当前应用或降级已有 Home。
2. 正常启动，完成自己的账号/模型配置，让官方应用初始化 desktop Profile。
3. 在应用“关于”中确认版本为 `0.2.0-rc.2`。也可只读核对：

```sh
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
  '/Applications/DeepSeek Harness.app/Contents/Info.plist'
```

4. 使用 **⌘Q 完全退出应用**。只关闭窗口可能仍在后台运行。

此后插件安装使用应用自带的 CLI，不要求另装 Node/pnpm，也不使用另一个 AHS 应用。桌面插件由官方 desktop Profile 管理，普通 npm 安装的 `dsh` 不能代替这个入口。[官方桌面文档](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/apps/desktop/README.md)

## 五、准备本地目录、备份和 Python

下文命令按顺序在**同一个终端窗口**执行；遇到错误先停在当前步骤。每个人使用自己的用户目录，不复制维护者电脑上的绝对路径。

### 5.1 目录约定

```sh
export ORION_DOWNLOAD_ROOT="$HOME/Downloads/ORION-RC7"
export ORION_BASE="$HOME/ORION"
export ORION_INSTALLER_ROOT="$ORION_BASE/installer-support"
export ORION_RUNTIME_ROOT="$ORION_BASE/runtimes/dsh-orion-runtime-1.0.0-rc.7"
export ORION_PROFILE_ROOT="$ORION_BASE/profiles/local"
mkdir -p "$ORION_BASE/runtimes" "$ORION_BASE/profiles" "$ORION_BASE/backups"
```

`installer-support` 是配置辅助文件；`runtimes` 是冻结服务源码；`profiles/local` 保存环境、资料、工程和缓存。三个目录职责不同。Profile 必须在 runtime 目录之外，路径不要经由符号链接。

若已存在这些目录并且用过 ORION，先按升级/恢复流程处理，不重复解压覆盖或删除旧 Profile。

### 5.2 在应用完全退出后备份

```sh
export ORION_BACKUP_ROOT="$ORION_BASE/backups/before-rc7-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$ORION_BACKUP_ROOT"
ditto "$HOME/.dsh" "$ORION_BACKUP_ROOT/dsh-home"
if [ -d "$HOME/Library/Application Support/@deepseek-ai/dsh-desktop" ]; then
  ditto "$HOME/Library/Application Support/@deepseek-ai/dsh-desktop" \
    "$ORION_BACKUP_ROOT/desktop-user-data"
fi
```

备份包含自己的会话和账号配置，只留自己电脑，不上传到 GitHub，也不发送给安装协助者。

### 5.3 解压已校验的文件

仅在对应目标目录尚不存在时执行：

```sh
tar -xzf "$ORION_DOWNLOAD_ROOT/dsh-orion-runtime-1.0.0-rc.7.tar.gz" \
  -C "$ORION_BASE/runtimes"
ditto -x -k "$ORION_DOWNLOAD_ROOT/orion-rc7-install-guide.zip" "$ORION_BASE"
```

结果应包含：

```text
~/ORION/installer-support/scripts/configure_orion_desktop.py
~/ORION/installer-support/scripts/launch_orion_harness.py
~/ORION/installer-support/scripts/orion_runtime_manifest.py
~/ORION/runtimes/dsh-orion-runtime-1.0.0-rc.7/pyproject.toml
~/ORION/runtimes/dsh-orion-runtime-1.0.0-rc.7/contracts/runtime-source-manifest.json
```

### 5.4 安装 uv 和 Python

先执行 `uv --version`。已有 uv 就跳过安装。已使用 Homebrew 的 Mac 可执行 `brew install uv`；其他安装方式见 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)。不需要另外创建全局 Python 环境。

```sh
uv python install 3.12
uv venv --python 3.12 "$ORION_PROFILE_ROOT/.venvs/core"
UV_PROJECT_ENVIRONMENT="$ORION_PROFILE_ROOT/.venvs/core" \
  uv sync --project "$ORION_RUNTIME_ROOT" --locked
```

首次安装需联网获取依赖。不要把 Wren 的依赖混入这个 Core 环境。[uv Python 安装说明](https://docs.astral.sh/uv/guides/install-python/)

### 5.5 核对 runtime 并准备空业务目录

```sh
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_RUNTIME_ROOT/scripts/orion_runtime_manifest.py" \
  --root "$ORION_RUNTIME_ROOT" --check
```

预期版本为 `1.0.0-rc.7`，文件数 `279`，指纹为：

```text
7964a84a604a94dbb7d8e45e5b09a4ead7997ac68e77c42624785bd47e2690f3
```

确认 `8091` 未被占用：

```sh
lsof -nP -iTCP:8091 -sTCP:LISTEN
```

没有监听结果时再执行下步。如果已被占用，选择其他空闲端口，并在下方 `--port` 及后续健康检查中同步替换；不要关闭不属于自己的进程。

```sh
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_RUNTIME_ROOT/scripts/prepare_orion_runtime.py" \
  --runtime-root "$ORION_RUNTIME_ROOT" \
  --profile-root "$ORION_PROFILE_ROOT" \
  --python "$ORION_PROFILE_ROOT/.venvs/core/bin/python" \
  --port 8091
```

这一步只准备路径、目录和空发布注册表，不启动服务。成功后有 `runtime-environment.json` 和 `runtime-manager.json`。提示已有配置时先核对原配置，不删除它们来强行重跑。

## 六、用官方方式安装两个插件

再次确认官方应用已完全退出，执行：

```sh
DSH_HOME="$HOME/.dsh" \
  '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' \
  plugin --profile desktop add \
  "$ORION_DOWNLOAD_ROOT/dsh-orion-workbench-1.0.0-rc.7.tgz"

DSH_HOME="$HOME/.dsh" \
  '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' \
  plugin --profile desktop add \
  "$ORION_DOWNLOAD_ROOT/dsh-client-ui-aqua-1.3.1-orion-alpha.4.tgz"
```

先把公开 Release 附件下载到本地，再把本地 tgz 路径交给插件安装器；Release 网页地址不是插件文件路径。

## 七、生成完整配置并合并

### 7.1 为自己的本地工程启用建设功能

把下面操作人标识改为自己的稳定标识，例如 `zhangsan`。它用于本地业务留痕，不是 GitHub Token，也不会授予仓库写入权限。

```sh
export ORION_OPERATOR_ID='请替换为你的操作人标识'
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B \
  "$ORION_INSTALLER_ROOT/scripts/configure_orion_desktop.py" \
  --environment "$ORION_PROFILE_ROOT/runtime-environment.json" \
  --dsh-home "$HOME/.dsh" \
  --allow-writes --actor "$ORION_OPERATOR_ID" \
  --enable-document-ingestion \
  --enable-workflow-mcp \
  --enable-realtime-mcp \
  > "$ORION_PROFILE_ROOT/desktop-overrides.json"
```

此配置允许在自己电脑创建工程、接入资料和调用工程 MCP。授权、阶段批准和正式发布门禁继续执行；实时问答 MCP 始终只读。

如果只看界面、暂不创建工程，可改为仅保留 `--environment` 和 `--dsh-home` 两项；生成器默认只读，资料写入入口及两个 MCP 默认关闭。两个模式任选一个，不要生成相互冲突的重复配置。

### 7.2 转为可粘贴的 YAML 列表

```sh
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B -c \
  'import json,sys,yaml; print(yaml.safe_dump(json.load(open(sys.argv[1])),allow_unicode=True,sort_keys=False),end="")' \
  "$ORION_PROFILE_ROOT/desktop-overrides.json" \
  > "$ORION_PROFILE_ROOT/desktop-overrides.yml"
```

生成器只输出四项覆盖方案，不会自动完成合并。这里转换的只是新生成的 JSON，**不读取或重写原 Profile 中的账号、模型和 `!!js` 配置**。

### 7.3 在文本编辑器中合并

目标文件是：

```text
~/.dsh/profiles/desktop/cordis.patch.yml
```

先在访达按 **⇧⌘G** 输入上述路径，定位文件；编辑前再保留一份副本。使用纯文本/代码编辑器，禁止保存为富文本格式。

按以下规则合并 `desktop-overrides.yml` 的完整内容：

1. 原文件若为顶层 YAML 列表，且没有下列四个 ID，把新文件的四个 `- id:` 条目放在同一层级；每个 `-` 从行首开始。
2. 原文件若为 `[]`，以新 YAML 列表替换这个空列表；不要保留 `[]` 后再粘贴另一段列表。
3. 若已有任意相同 ID，用生成的该条**完整对象**替换旧条目，每个 ID 只留一项。
4. 保留其他账号、模型、插件及个人设置。不要用四项配置覆盖整份已有文件，不要只复制单个字段。
5. 如果原文件是其他结构、复杂嵌套或不确定条目边界，请由部署维护者完成这一步；不要通过删除原配置解决。

四个覆盖 ID：

```text
orion-runtime-manager
orion-workbench
mcp-orion-workflow
mcp-orion-realtime
```

Cordis 会替换条目的整个 `config` 对象，因此路径、参数和环境映射必须完整保留。JSON 本身虽然也是 YAML，但不能把一个 JSON 数组直接拼到既有 YAML 列表后面。

保存后可以做**仅语法检查**，此命令不会执行 YAML 中的 `!!js`：

```sh
"$ORION_PROFILE_ROOT/.venvs/core/bin/python" -B -c \
  'import pathlib,yaml; p=pathlib.Path.home()/".dsh/profiles/desktop/cordis.patch.yml"; n=yaml.compose(p.read_text()); assert isinstance(n,yaml.SequenceNode), "顶层必须为列表"; print("YAML syntax OK; still verify IDs and complete configs")'
```

语法通过不等于路径或业务能力正确，下一节继续检查。生成的配置含本机路径，留在本机，不上传到仓库。

## 八、启动、设置和验收

1. 从 Mac“应用程序”正常打开 **DeepSeek Harness**，首次阅读并关闭官方预览版说明；如默认英文，在官方设置选择中文。
2. 左侧“插件 → 已安装”中确认工作台 RC7 和 Aqua alpha.4，启用两者。
3. 工作台应显示 AHS、`探索属于你的智能宇宙`、本体中心和工程发起模板；本体中心包含工程、本体管理、行业模板入口。
4. 在 Harness 新建/选择自己的资料工作区，目录选择 `~/ORION/profiles/local/state/references`，可命名“本体资料工作区”，按官方工作区界面设为默认。名称和默认选择属于当前机器的工作区设置，不会随安装包复制维护者的设置。
5. 把自己获授权使用的一份小资料放进这个目录，新会话里用 `@` 确认能找到它。看到候选只证明引用入口可用；提交工程后仍需检查资料接入回执。
6. Aqua 设置里选择流体、图片或视频背景，检查 Logo、左右区域、文字可读性；媒体应使用本机可访问文件。
7. 关闭两个插件，确认恢复所安装宿主的官方原生界面；重新开启，确认工作台和玻璃恢复。开关不会自动删除自己的资料和历史会话。

后台由工作台管理，不另开一个终端手动重复启动同端口服务。可在工作台启用后只读检查：

```sh
curl --fail --silent --show-error 'http://127.0.0.1:8091/health'
```

首次没有正式本体发布时，响应可能是 `DEGRADED` / `NO_PUBLISHED_RUNTIME`，表示发布目录为空。若请求连不上，应检查应用/插件是否启用、四项配置和端口，而不是宣称安装已通过。HTTP 可达也不是完整业务验收。

安装检查与业务验收分别记录：

| 层次 | 必须确认的结果 |
| --- | --- |
| 安装 | 官方宿主和三个交付物版本匹配；runtime 指纹通过 |
| 桌面与后台 | 模板、Logo、资料引用和插件开关可用；本地 Core 使用自己的 Profile |
| 模型与工具 | 自己的模型账号可用；工程/问答模式、Skill 与 MCP 发现正常，实际工具调用取得结果 |
| 业务 | 用真实授权资料建立工程，按 S0–S7 提交证据、校验并取得人工批准；正式发布后问答引用对应版本和证据 |

目前交付完成的是维护者 Mac 上的安装、插件和相关运行验证；没有宣称已经完成下载者新机器或客户真实资料的全业务验收。

## 九、按业务需要补齐服务

后台服务代码已经包含在 `dsh-orion-runtime-1.0.0-rc.7.tar.gz` 中，不需要访问维护仓库才能取得。它是完整的受版本契约约束的 Python 源码交付，包含：

| 目录/文件 | 内容 |
| --- | --- |
| `services/` | 本体工程、资料处理、Core API、证据问答与分析等业务实现 |
| `harness/` | Workflow/QA MCP、业务操作入口、工程技能与本体模板 |
| `scripts/` | runtime 准备、校验，以及构建/质量/发布等业务脚本 |
| `database/` | 数据库迁移及所需模式定义 |
| `pyproject.toml`、`uv.lock` | Python 依赖及锁定版本 |
| `contracts/` | runtime 版本与279个运行文件的完整内容指纹 |
| `tests/`、`vendor/` | 配套测试及第三方资源许可；包内共337个文件 |

服务需要安装自己的运行环境并配置实际业务依赖；它不包含数据库服务器、全部外部专业工具或维护者的数据。

| 使用范围 | 额外准备 |
| --- | --- |
| 文件资料与本地工程 | 真实授权的资料和可用模型；扫描件/OCR 依实际资料配置 |
| 数据库资料 | 自己的只读数据源和明确连接配置；RC7 不含数据库默认账号密码 |
| 本体构建、质量验证 | 按对应阶段配置 Protégé/HermiT、Semantica 及其实际接口 |
| 正式发布与证据问答 | 按选用架构配置 Ontop/Fuseki、正式资产和运行注册表，完成真实发布回读 |
| Wren 分析 | 单独创建 Wren 环境；不混入 Core |

需要 Wren 时执行：

```sh
uv venv --python 3.12 "$ORION_PROFILE_ROOT/.venvs/wren"
uv pip sync --python "$ORION_PROFILE_ROOT/.venvs/wren/bin/python" \
  "$ORION_RUNTIME_ROOT/scripts/requirements-wren.txt"
uv pip check --python "$ORION_PROFILE_ROOT/.venvs/wren/bin/python"
```

具体业务连接和凭据由部署负责人按实际数据范围配置；同时使用工作台和 Workflow MCP 时，要保持两者配置中的对应业务环境一致。不要把账号或密码写进教程、源码或 Release。

**服务器模式尚未完成。** 当前工作流 gateway 会执行本地 Python；不能只把 Core 地址换成服务器 URL 就称为完整远程部署。多人权限、远程文件、业务 API、任务恢复和发布问答需要另做实现与验收。

## 十、常见问题与恢复

| 现象 | 处理 |
| --- | --- |
| GitHub 显示 404 | 当前仓库和 RC7 已公开；核对链接是否完整及网络是否可访问 GitHub，无需申请协作者权限 |
| 能看仓库但找不到版本 | 管理员确认 Release 已发布、不是 Draft；打开指定 RC7，而非只看 Latest |
| 找不到配置生成器 | 确认下载并解压 `orion-rc7-install-guide.zip`；runtime tar.gz 内原本没有它 |
| 提示插件版本不兼容 | 核对官方 App 是否为 0.2.0-rc.2；不使用兼容豁免 |
| CLI 提示 desktop Profile 不存在 | 官方 App 先正常启动初始化，再 ⌘Q 退出；使用该 App 自带 CLI |
| 只能看，不能创建工程 | 检查第七节是否启用了本地业务写入与操作人；这和 GitHub Read 没关系 |
| 本体中心为空 / 没有正式问答结果 | 第一次没有自己的工程和发布；先建设和正式发布，不导入维护者旧数据冒充结果 |
| 后台无法启动 | 检查 Core Python、源码指纹、绝对路径、端口和完整覆盖项；不要把问题归为“缺 npm” |
| 修改配置后仍异常 | 完全退出应用，先恢复刚才的配置副本再启动；保留新业务数据用于排查 |
| 升级后需要回退 | 先停应用并备份升级后新增资料，再使用匹配版本的包与升级前 Profile；不要把新版 Home 直接交给旧宿主 |

停用工作台会停止它自己管理的 Core；Aqua 只负责外观。仅需要官方原生外观时，可以在插件界面关闭两个插件，无需删除本地业务目录。

## 十一、维护者交付核对表

- 直接文件交付：收件人拿到完整 ZIP 和校验值，无需源码仓库权限。
- GitHub 自助下载：确认未登录也能打开公开仓库、RC7 Release 和具名附件。
- 没有为了下载向使用者授予本仓库写入权限。
- 三个 RC7 冻结包 SHA-256 与原始 Release 一致；辅助 ZIP 单独记录校验值。
- RC7 Release 已发布并标记预发布，README 指向当前教程。
- 下载教程、版本包及校验清单对应同一候选版本。
- 新机器实际安装和所需业务链由安装者验收，不能沿用维护者机器的测试结论。

本教程及安装辅助包可以独立修订；不得因此覆盖原 RC7 插件和 runtime 归档。任何运行逻辑变更应采用新的候选版本并重新验证。
