# Neuro Code

**简体中文** · [English](../en/README.md)

Neuro Code 是一个可扩展的 Python 终端编码智能体。项目使用 Python 原生内部架构，
并以 CLI、配置、会话、工具、MCP 和 ACP 等边界上的稳定行为为目标。

当前实现处于 pre-alpha 阶段。无头代理运行时与第一版最小 Textual TUI 已成为受支持的
纵向切片；其余界面和协议工作记录在[兼容矩阵](compatibility-matrix.md)中。

## 开发环境

项目要求 Python 3.12 或更高版本，并统一使用 `uv` 管理环境：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

在工程引导阶段，也可以不安装软件包而直接运行：

```bash
PYTHONPATH=src python -m neuro_code version
PYTHONPATH=src python -m unittest discover -s tests
```

查看已生效且不会暴露密钥的配置：

```bash
PYTHONPATH=src python -m neuro_code inspect --json
```

## 交互式 TUI

开发依赖组已经包含 Textual。配置好供应商后，不带子命令即可启动交互界面：

```bash
uv sync --extra dev
uv run neuro-code
```

在开发环境之外安装构建产物时，使用 `pip install 'neuro-code[tui]'` 加入可选 UI 依赖。
第一版 TUI 提供提示输入、滚动记录、assistant 流式文本、供应商/工具状态，以及本地
`/help`、`/status`、`/settings`（别名 `/setting`）、`/provider`、`/model`、
`/sessions [QUERY]`、`/resume`、`/cancel`、`/rename TITLE`（别名 `/title`）、`/clear`、
`/quit` 和 `/exit` 命令。同一次启动中的提示
会共享一个持久会话；`--resume SESSION_ID` 会在工作区校验通过后打开已有会话。

全屏界面采用中性深色配色，只用克制的暖色表示焦点和系统状态。由于 `Ctrl+P` 已用于
供应商选择，Textual 自带的另一套命令面板会被禁用；会话搜索继续使用纯文字
`/sessions QUERY` 流程，不显示表情符号搜索图标。终端未正常送达尺寸变化通知时，应用还
会校准真实 TTY 单元格尺寸，因此最大化或缩放窗口会重绘整个视口，不会把旧画布留在
左上角。

用户提示显示为占满整行的低对比度块，助手输出使用独立回答块，不再依靠 `You:` 与
`Assistant:` 日志前缀区分。每条流式回答只在对话流中挂载一次，后续增量直到最终文本都
原地更新，因此完成时不会再从临时区域移动到滚动记录；用户主动向上滚动后也不会被强制
拉回底部。

使用 `Ctrl+,`、`/settings` 或 `/setting` 可以选择英语或简体中文。切换会立即更新应用
自有的控件、对话框和状态文案，但不会翻译用户提示、模型回答或工具内容。选择结果与
供应商配置分开保存到 `$NEURO_CODE_HOME/ui-preferences.json`（通常为
`~/.neuro-code/ui-preferences.json`），后续启动 TUI 时会继续使用。

使用 `Ctrl+P`、不带参数的 `/provider` 或 `/model` 可以打开已配置 profile 选择器；
`/provider PROFILE` 与 `/model PROFILE` 可以直接选择。选择器只展示 profile 名称、模型、
协议和就绪状态；不可用或缺少凭据的 profile 会被禁用。它选择的是已配置 profile，而非
任意远程模型 ID，也不会修改配置。轮次运行期间禁止切换。切换到不同 profile 时，旧的
SQLite 会话仍可恢复，下一条提示使用全新会话，从而避免把供应商亲和或加密上下文带到
另一个供应商。

使用 `Ctrl+R`、`/sessions` 或不带参数的 `/resume` 可以打开当前工作区最近 50 条会话
的选择器；`/resume SESSION_ID` 可直接恢复。`/sessions QUERY` 会先按保存标题和可见对话
内容执行工作区全文搜索。选择器显示确定性的首提示标题（或导入标题）、缩短的 ID、
更新时间、保存的供应商/模型、恢复 profile，以及搜索时的有界摘要。查询、标题和摘要均
按纯文本渲染；系统消息、供应商私有推理/原生项、图片 URL、工具参数/元数据和原始工具
结果内容不进入搜索索引。
恢复时优先使用
名称匹配且就绪的来源 profile；否则使用当前就绪 profile，并继续由保存的来源/模型/亲和
元数据失败关闭地过滤供应商原生上下文。原活动会话保持不变。

`/rename TITLE` 会更新当前已保存会话的标题，`/title TITLE` 是其别名。首个会话尚未创建
或轮次正在运行时会拒绝重命名。标题会合并多余空白并限制为 200 个字符；SQLite 会在
同一个事务中更新规范摘要和 FTS 标题。

选择器还会显示保存的沙箱 profile。由于进程沙箱已经生效，在不同 profile 下创建的会话
会被禁用，必须用 `--resume SESSION_ID` 重启 Neuro Code 后打开。在 profile 元数据出现
以前创建的会话仍可按当前活动 profile 选择。

启动参数恢复和应用内恢复都会回放规范的可见用户/助手消息、图片占位和仅含名称的工具
生命周期。保存的推理、供应商原生项、工具参数、图片 URL 及原始工具结果不会进入记录；
每条恢复消息也有 20,000 字符的界面上限。详见
[ADR 0018](adr/0018-workspace-scoped-interactive-session-resume.md)。

轮次运行期间可使用 `Ctrl+C` 或 `/cancel` 请求取消。运行时会记录取消，把当前以及同批
尚未启动的本地工具调用补齐为错误结果，重载持久会话，并让同一个会话继续接受下一条
提示。当前切片会在会话历史中保留被取消的用户消息；尚未实现首个 token 之前的无痕
回退与草稿恢复。

具有副作用的工具判定为 `ask` 时，TUI 会打开失败关闭的审批模态框，默认焦点是拒绝。
可以选择仅允许本次、在本进程会话中允许完全相同的工具/参数操作，或者拒绝；`Esc` 也会
拒绝，模态框打开时 `Ctrl+C` 也会拒绝。编辑摘要显示工作区路径但隐藏替换/patch 内容，
Bash 则显示待授权的有界命令。
会话批准只保存内存中的精确操作摘要，并且在策略判定之后生效，因此永远不能覆盖显式
deny。关闭或取消审批都不会启动工具。

审批模态框内的 `Ctrl+C` 只拒绝当前请求，不会触发整轮取消。

只有确实希望工具在该工作区不受限制地执行时，才应使用 `--always-approve`。脚本和机器
可读输出继续使用无头路径；其中未解决的审批仍会被拒绝：

```bash
neuro-code -p "Explain this repository"
neuro-code agent -p "Explain this repository" --output-format jsonl
```

## 受管后台命令

普通 CLI/TUI 组合会向模型提供由进程所有权约束的后台命令契约。`bash` 使用
`is_background=true` 时立即返回任务 ID；`task_output` 可以不等待就返回当前状态和输出，
也可以用最大 30 秒的 `wait_seconds` 执行事件驱动的有界等待。`wait_tasks` 最多接受 20 个
ID，并在有界的 30 秒事件等待中选择任意一个或全部已知任务。未知或跨会话 ID 会报告为
`not_found`；超时只返回部分状态，不会终止任务。`kill_task` 会终止整棵受控进程树，并在
需要时从 TERM 升级到 KILL。启动与终止仍是有副作用的操作，和其他本地动作一样经过权限/
审批策略。

后台任务省略 `timeout_seconds` 时会一直运行到自然退出、被终止或应用关闭；显式正数会
为任务设置截止时间。输出只是在内存中有界保留的首尾预览，同时记录总字节数，并非持久
完整日志。一个应用进程最多同时运行 16 个任务，每个会话作用域最多保留 64 条记录。

后台记录不会写入 SQLite。任务可以在仍运行的 TUI 同一会话绑定中跨轮次使用，但不能跨
profile 切换、进程内会话恢复、重启或启动恢复。切换绑定会终止其中的活动任务并显示数量；
单次无头运行会在返回前终止剩余任务，TUI 退出时也一样。

在 TUI 中使用 `/tasks` 可以只读查看当前绑定的任务 ID、状态、退出码、有界输出大小和开始
时间。每个任务进入终态时，TUI 会发出一次本地通知，但不会打印命令文本或原始输出。
`/tasks` 不能终止任务；应让模型使用 `kill_task`，使该操作继续经过权限/审批策略。应优先
使用 `is_background=true`，而不是在 Shell 内部追加 `&`；完整的跨平台后代进程所有权仍需
Windows Job Object 支持。详见
[ADR 0021](adr/0021-owned-background-shell-tasks.md) 和
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。多任务等待语义由
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) 定义。

自然完成还会在下一次明确模型边界报告一次：工具执行后的下一模型步骤，或空闲时由下一条
用户提示触发的轮次。仅供模型使用的每批通知最多包含 20 个任务，只携带经过转义的状态
元数据，不包含命令文本、cwd 或输出。终态 `task_output`、`wait_tasks` 或 `kill_task` 结果
会消费对应通知，防止重复。只有供应商返回有效完成后才确认通知；通知不会作为会话消息
持久化，也绝不会自主启动付费模型轮次。详见
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md)。

## 操作系统沙箱 profile

沙箱需要主动启用，默认值为 `off`。可以在用户或项目配置中选择持久 profile，使用
`NEURO_CODE_SANDBOX`，也可以只覆盖本次运行：

```toml
[sandbox]
profile = "workspace"
```

```bash
neuro-code -p "Inspect and test this repository" --sandbox workspace
```

Linux 上的非 `off` profile 要求存在可用且不受工作区控制的 `bwrap`；`read-only` 与
`strict` 还需要 `unshare`。Neuro Code 会探测这些能力，并在打开会话存储或开始模型工作
之前重新执行自身。显式请求之后绝不会回退到未启用沙箱的运行。

| Profile | 文件系统 | 本地 Bash 网络 |
|---|---|---|
| `off` | 不启用操作系统沙箱 | 可用 |
| `workspace` | 宿主可读；工作区、状态目录与临时路径可写 | 可用 |
| `read-only` | 宿主/工作区只读；状态目录与临时路径可写；编辑工具不可用 | 隔离 |
| `strict` | 仅暴露必需的系统/运行时路径和工作区；工作区、状态目录与临时路径可写 | 隔离 |

父进程仍可访问模型 API。权限系统依然会在工具之前运行且仍有必要：沙箱限制获批操作的
范围，但不负责决定是否批准。项目文件不能弱化用户级 profile；CLI 与环境变量是新会话
中显式且优先级更高的选择，但恢复时不能改变已保存会话的 profile。
`neuro-code inspect` 会报告规范 profile 及其来源。
本地 Bash 还会移除已配置的供应商 API Key 变量和标准/显式代理变量，不继承其中的密钥值。

macOS 与 Windows 当前会对显式非 `off` profile 失败关闭。每个新会话（包括 `off`）都会
保存规范 profile。恢复时若未显式指定沙箱，就还原保存值；显式 `--sandbox` 或
`NEURO_CODE_SANDBOX` 经规范化后若不同，会在执行沙箱和组合模型之前失败。没有该字段的
旧会话使用普通配置。自定义 profile 仍不受支持。详见
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) 和
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md)。

## 模型供应商

Neuro Code 不再隐式绑定任何云供应商。必须在 `~/.neuro-code/config.toml` 或
`.neuro-code/config.toml` 中配置至少一个命名 profile；否则模型运行会返回配置指引，
不会静默连接 xAI。

```toml
[routing]
default = "deepseek"
fallbacks = ["anthropic"]

[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
auth = "env"
api_key_env = "DEEPSEEK_API_KEY"
max_output_tokens = 8192
timeout_seconds = 120
proxy_mode = "environment"

[providers.anthropic]
protocol = "anthropic-messages"
model = "replace-with-an-api-model-id"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

支持的线路协议为 `openai-chat`、`openai-responses`、`anthropic-messages` 和
`gemini-generate-content`。配置只保存环境变量名称。Neuro Code 不写入原始 API Key，
不自动读取项目 `.env`，并在检查输出和异常中隐藏凭据。

无需修改默认项即可检查和临时选择 profile：

```bash
neuro-code providers list
neuro-code providers inspect deepseek --json
neuro-code -p "Explain this repository" --provider deepseek
```

`--provider`、`--model` 与 `--base-url` 仅覆盖本次运行。对应的
`NEURO_CODE_PROVIDER`、`NEURO_CODE_MODEL` 和 `NEURO_CODE_BASE_URL` 环境变量会先于 CLI 覆盖生效。
迁移期间仍可读取旧 `[provider.default]` 和 `[model.default]` 配置。

### 安全供应商故障转移

`[routing] fallbacks` 用有序列表声明备用 profile。供应商实例按需创建，因此备用项暂时
不可用不会阻止主 profile 运行。每个候选项至多尝试一次；备用项一旦成功，同一次运行
后续的模型步骤会继续使用它，不会自动切回此前的候选项。

故障转移被严格限制在候选供应商产生第一个模型事件之前。一旦已经出现文本、推理、
工具调用、供应商托管工具生命周期事件、用量或完成事件，后续错误会直接上抛，不会再
尝试其他供应商。这个提交边界避免重复已经产生输出、副作用或费用的工作。尝试失败与
选择结果会分别产生 `provider_attempt_failed` 和 `provider_selected` 运行时事件。可用
`--no-failover` 强制本次运行只使用选中的 profile：

```bash
neuro-code -p "Explain this repository" --no-failover
```

所有候选项均失败时，Neuro Code 会返回有界的汇总错误。目前还不会重试同一候选项、
持久化健康状态或实现熔断。详见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)。

### HTTP 代理策略

每个供应商 profile 都有显式 HTTP 传输策略：

- `proxy_mode = "environment"` 是保持兼容的默认值。HTTPX 会读取
  `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其证书环境；Neuro Code
  会在所选 profile 启动前按需校验已配置代理 URL 的 scheme。
- `proxy_mode = "direct"` 会设置 HTTPX `trust_env = false`，只对该 profile 忽略代理
  和证书环境变量。
- `proxy_mode = "explicit"` 要求设置 `proxy_url_env`；由指定环境变量提供代理 URL，
  不把它持久化到 TOML。

例如：

```toml
[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "NEURO_DEEPSEEK_PROXY_URL"
```

配置检查只暴露模式、环境变量名和“是否已配置”布尔值；代理 URL 与认证信息会从异常中
脱敏。含义不明确的 `socks://` 会被拒绝，不会擅自猜测；应在安装 HTTPX 可选 SOCKS
依赖后使用 `socks5://` 或 `socks5h://`，或者改用 HTTP 代理。详见
[ADR 0012](adr/0012-provider-http-proxy-policy.md) 和 HTTPX 官方
[环境变量文档](https://www.python-httpx.org/environment_variables/)。

### CC Switch 兼容

将 `NEURO_CODE_CC_SWITCH_CONFIG` 指向 CC Switch 导出的 TOML 文件后，Neuro Code 会
以最低优先级只读加载。活动 `[models] default` 与 `[model."<profile>"]` 会转换为名为
`cc-switch:<profile>` 的内存 profile，映射规则如下：

| CC Switch `api_backend` | Neuro Code 协议 |
|---|---|
| `responses` | `openai-responses` |
| `chat_completions` | `openai-chat` |
| `messages` | `anthropic-messages` |

`env_key` 会作为环境变量引用使用。只有基础地址为普通 HTTP 回环地址（例如
`http://127.0.0.1:15721/provider/v1`）时才接受 `PROXY_MANAGED` 占位凭据。其他内联
密钥不会被复制或使用，对应 profile 会显示为不可用并附带修复提示。CC Switch 始终是
可选项：Neuro Code 不读取其私有数据库、不管理它的进程，直连供应商也不依赖它。
CC Switch 最终仍需合法的上游 API Key、OAuth 授权、中继 Token 或本地模型。

自动识别的代理 profile 默认使用 `native_context = "disabled"`，因为代理可能切换
上游。只有端点和 profile 稳定且可信时，才能在项目覆盖配置中显式设置
`native_context = "profile"`；此时不透明推理也只会在会话中保存的 profile 亲和指纹
完全一致时回放。

### 可选 xAI 方言

xAI 现在是可选 Responses 方言，而不是应用默认项：

```toml
[providers.xai]
protocol = "openai-responses"
dialect = "xai"
model = "replace-with-an-xai-model-id"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
native_context = "profile"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

通用 Responses 适配器使用本地 `store: false` 历史。xAI 方言会额外请求加密推理、
支持 xAI 托管工具，并保留经过校验的推理/后端工具项目。托管工具由 xAI 执行，不进入
本地工具链；运行时会为其产生独立后端生命周期事件，并且它们可能产生额外费用。目前
尚未实现有状态 `previous_response_id` 链接和压缩项。

DeepSeek 及其他 Chat Completions 服务使用 `openai-chat`。思考模式工具调用关联的
assistant 推理会被持久化并在下一请求回放；没有工具调用的已完成推理只保留在本地。
运行所选 profile 前，应通过 Shell 或密钥管理器加载 `DEEPSEEK_API_KEY`。

### 可选在线回归测试

在线测试需要访问网络并可能产生供应商费用，因此使用两层门禁。普通测试命令会排除
`live` 标记；即使显式选择 `-m live`，如果没有设置
`NEURO_CODE_RUN_LIVE_TESTS=1`，收集阶段仍会把用例标记为跳过。凭据只从当前进程环境读取，
测试套件绝不会自动加载 `.env`。

在调用 Shell 中导出 `DEEPSEEK_API_KEY` 后执行：

```bash
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

可以用 `NEURO_CODE_LIVE_DEEPSEEK_MODEL` 和
`NEURO_CODE_LIVE_DEEPSEEK_BASE_URL` 覆盖 `.env.example` 中的安全默认值。当前 DeepSeek
检查覆盖真实流式响应、从人为制造的主供应商输出前失败中恢复，以及只读本地工具往返；
测试不会启用 Bash 或写入工具。在线测试默认继承标准代理环境变量；设置
`NEURO_CODE_LIVE_PROXY_MODE=direct` 可以忽略它们，或者同时设置
`NEURO_CODE_LIVE_PROXY_MODE=explicit` 与临时环境变量 `NEURO_CODE_LIVE_PROXY_URL`，让代理仅
作用于这些检查。该方式适用于本地 `ALL_PROXY` 使用 HTTPX 拒绝的 URL scheme 的情况；
不能把代理凭据写入项目配置。

恢复、列出、重命名、导出和导入会话：

```bash
neuro-code -p "Continue the work" --resume SESSION_ID
neuro-code sessions --json
neuro-code sessions search "sqlite migration"
neuro-code sessions search "sqlite migration" --json --include-content --limit 20
neuro-code sessions rename SESSION_ID "手动会话标题" --json
neuro-code export SESSION_ID --format markdown --output transcript.md
neuro-code import-session /path/to/upstream/session --json
```

`import-session` 既可以接收受支持的上游 Rust 会话目录，也可以直接接收其中的
`summary.json`。它只读解析 JSONL 文件，不会修改源文件；随后在单个事务中创建
SQLite 会话，并保留源会话 ID、工作区、模型和时间戳。已有相同会话 ID 时会拒绝导入，
而不是覆盖数据。JSON 报告会列出跳过的损坏记录或暂不支持的记录。规范消息模型目前
会按原顺序结构化保存推理记录、后端工具记录和图片 URL。JSON 导出格式版本 4 在普通
`messages` 投影之外提供完整的 `conversation_items` 序列，并报告规范保存的沙箱 profile
与可选标题；旧沙箱 profile 会话仍为 `null`。上游摘要中可识别的内建 profile 和
`generated_title` 会被保留；不支持的自定义 profile
会被拒绝而不会静默降级。受支持的图片引用会通过
供应商原生内容块回放：OpenAI 兼容和 Gemini 的用户消息，以及 Anthropic 的用户消息
和工具结果。无效引用及不受支持的角色会收到明确图片占位文本。只有来源标记可信且
目标为 xAI 官方 HTTPS 端点时，恢复导入会话才会回放可见推理与有序
后端工具摘要。不透明的 Responses 加密状态绝不会复制到 Chat Completions，非亲和
供应商只接收普通消息投影。改用带 xAI 方言的 `openai-responses` profile 后，会原生
回放经过校验的推理项和受支持的后端工具项，在输入时剥离仅供输出使用的推理状态，并
继续拒绝非亲和的不透明
状态。
旧 assistant 中的 `raw_output`、单体推理和 v0
`reasoning_content` 会在内存中升级；后端工具 ID
可阻止内嵌副本与此前的独立记录重复。导入报告会分别统计恢复项、去重项、损坏项和
不支持的内嵌项。

无头 Bash 权限支持兼容的 `Bash(...)` 写法。简单命令链中的每个命令都会独立判定，
因此允许 `git status` 不会隐式允许后续命令：

```bash
neuro-code -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

显式 deny 规则优先于 `--always-approve`。在限制性 Bash 策略下，替换、重定向、
多行脚本以及当前无法安全分解的其他结构会在无头模式下被拒绝。命令使用空 stdin、
有界输出、禁用的分页器/交互提示，并在超时或取消时清理整个进程树。

## 项目状态

- 最低 Python 版本：3.12
- 目标平台：Linux、macOS、Windows
- 许可证：Apache-2.0；来源要求见 [`NOTICE`](../../NOTICE)
