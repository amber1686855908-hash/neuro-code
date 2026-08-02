# Neuro Code

**简体中文** · [English](../en/README.md)

Neuro Code 是一个可扩展的 Python 终端编码智能体。项目使用 Python 原生内部架构，
并以 CLI、配置、会话、工具、MCP 和 ACP 等边界上的稳定行为为目标。

当前实现处于 pre-alpha 阶段。无头代理运行时、第一版最小 Textual TUI 和 partial ACP
v1 stdio 核心已成为受支持的纵向切片；其余界面和协议工作记录在
[兼容矩阵](compatibility-matrix.md)中。

## 安装与启动

正式发行后，只需用能暴露全局 Python 控制台命令的工具安装一次；以后使用时无需激活
虚拟环境：

```bash
uv tool install neuro-code
# 或：pipx install neuro-code
```

随后在任意目录打开终端，以下三种形式都会启动 TUI，并把该目录作为工作区：

```bash
neuro
neuro code
neuro-code
```

Textual 已是普通依赖，因此标准安装会包含 TUI。源码开发阶段使用 `uv run neuro`。

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

## Partial ACP v1 stdio

`neuro-code acp` 通过官方 Python SDK 的换行分隔 stdio 传输提供绑定单一工作区的
partial ACP v1 实现：

```bash
uv run neuro-code acp --cwd /absolute/workspace
```

本切片实现 `initialize`、`session/new`、`session/list`、`session/load`、
`session/delete`、`session/fork`、`session/resume`、`session/prompt`、
`session/cancel`（notification）和 `session/close`，发送 `session/update`
notification，并通过 `session/request_permission` 请求交互授权。它声明
`loadSession: true` 和 list/delete/fork/resume/close session capability。Text、内嵌
Image、ResourceLink 与内嵌文本资源提示块会保持输入顺序并受数量/字节限制。Image 只接受一小组
光栅 MIME 类型的原始 base64，最多八张、单张 5 MiB、总计 10 MiB；关联 URI 绝不会被读取或
解引用。内嵌 `TextResourceContents` 只接受客户端已提供的文本，最多八个、单个 64 KiB、合计
128 KiB；URI 只是来源标签，绝不会被解析。ResourceLink 元数据采用字段白名单，`_meta` 不会
进入模型，提示转换期间也绝不会下载或解引用链接。

每条连接固定绑定到启动工作区。`session/new` 会拒绝不同或非绝对的 `cwd`；`off` profile
的 binding 最多还可声明四个已存在、绝对且互不重叠的 `additionalDirectories`，所有已启用
sandbox 都会在建立 binding 前拒绝它们。它接受有界的 stdio、Streamable HTTP 与 legacy SSE
`mcpServers`，在发布 session 前完成初始化与工具枚举，只拒绝 ACP 传输 MCP server。ACP session ID 稳定且独立于首次
prompt 时才按需创建的内部 SQLite ID；持久映射允许后续进程加载同一个 ID。load 会重新
校验工作区、固定 sandbox 和 provider 亲和，然后只回放有界/脱敏的可见用户、助手和工具
历史。图片会保留在持久化模型上下文中，但其 ACP 历史仅显示安全的文本占位符；图片字节与
URL 不会回放。内嵌文本资源会作为有界、带标签的用户文本留在历史中。系统提示、私有
reasoning、供应商原生上下文、任意参数与 raw 工具数据不会回放。list 只返回连接工作区的
安全元数据，为旧会话分配持久 ACP ID，并使用有界 opaque cursor
分页。不同 session 可以并行，同一 session 不能并发 prompt。取消、关闭、stdin EOF 和
连接故障都会取消受控工作与 session 作用域后台任务；close 不会删除持久化历史。

每个接受的 MCP server 及其工具都由对应 ACP session 独立持有。官方 MCP Python SDK
负责 Schema、`ClientSession`、协商与 JSON-RPC 调度。stdio 使用 Neuro Code 有界的
`ProcessTree` 桥接；远程传输使用 SDK 的 Streamable HTTP 或 legacy SSE client，并校验
HTTP/HTTPS URL 与 header、不继承环境代理、不跟随重定向且限制响应体。本切片只投影 MCP
工具。每次调用都按有副作用操作处理，即使本地处于 bypass 模式也必须请求 ACP client 审批；
本地显式 deny 仍优先。`_meta` 被忽略，显式环境变量/header 值会被脱敏。取消远程请求会
在本地关闭并令该连接不可再用；远程 server 不是本地持有的进程，因此不会把可能仍在执行的
远程副作用表示成已成功取消。

ACP 会话 resume/delete/fork 已按工作区范围持久身份、事务 fork/delete，以及回放 load
与静默 resume 的不同语义、额外目录、MCP HTTP/SSE 工具，以及由能力协商控制的客户端
`fs/read_text_file` 和精确替换式 `fs/write_text_file` 实现。声明 `terminal: true` 的客户端在
`off` 沙箱 binding 中还会获得直接前台 `terminal_exec`；它接收可执行文件和参数向量，而不是
Shell 命令，也不会收到已配置的 Neuro Code 环境值。它也暴露有界的标准后台生命周期
（`terminal_start`、`terminal_output`、`terminal_wait` 和 `terminal_kill`），并使用不透明 task ID；
终端输入、resize 和 PTY framing 仍不可用。这仍明确不是完整 ACP v1
支持：ACP MCP 传输、MCP resource/prompt/sampling/elicitation、音频 prompt、内嵌二进制
资源 prompt、二进制多媒体历史回放、客户端交互式终端输入/resize/PTY 方法、WebSocket 传输和自定义扩展
仍不支持，也不会被声明。详见
[兼容矩阵](compatibility-matrix.md)和
[ADR 0035](adr/0035-partial-acp-v1-stdio.md)及
[ADR 0036](adr/0036-durable-acp-session-load.md)和
[ADR 0037](adr/0037-workspace-scoped-acp-session-list.md)，以及
[ADR 0038](adr/0038-session-owned-stdio-mcp-tools.md)、
[ADR 0050](adr/0050-acp-session-lifecycle.md)、
[ADR 0051](adr/0051-bounded-remote-mcp-transports.md)和
[ADR 0052](adr/0052-capability-gated-acp-client-filesystem.md)及
[ADR 0053](adr/0053-capability-gated-acp-client-terminal.md)，以及
[ADR 0054](adr/0054-bounded-acp-inline-image-prompts.md)及
[ADR 0055](adr/0055-bounded-acp-embedded-text-resources.md)，以及
[ADR 0056](adr/0056-bounded-acp-client-background-terminals.md)。

## 交互式 TUI

源码开发时，不带子命令即可启动交互界面：

```bash
uv sync --extra dev
uv run neuro
```

首次启动且没有就绪供应商时，TUI 会先打开供应商设置表单，再组合代理运行时。普通设置
入口先显示“界面语言”和“模型供应商”一级分类，再只打开所选详情页。供应商详情可新建或
编辑多个 OpenAI Responses、OpenAI 兼容 Chat、DeepSeek、Anthropic、Gemini 或 xAI
profile，并立即“保存并使用”。每个受管 profile 可选择继承环境代理、直连或从命名环境
变量读取显式代理；删除需要二次确认，并同时移除对应凭据。如果受管默认 profile 因代理
配置无法启动，TUI 会直接打开该 profile 显示错误并允许修复，而不是退出到终端。第一版
TUI 提供提示输入、滚动记录、assistant 流式文本、供应商/
工具状态，以及本地
`/help`、`/status`、`/settings`（别名 `/setting`）、`/provider`、`/model`、
`/effort [LEVEL]`（别名 `/reasoning`）、`/mode [MODE]`、`/sessions [QUERY]`、`/resume`、
`/rename TITLE`（别名 `/title`）、`/cancel`、`/clear`、`/quit` 和 `/exit` 命令。同一次
启动中的提示会共享一个持久会话；`--resume SESSION_ID` 会在工作区校验通过后打开已有
会话。

全屏界面采用中性深色配色，以冷色蓝、紫、青、绿承担语义强调；暖色只保留给警告和
错误。由于 `Ctrl+P` 已用于
供应商选择，Textual 自带的另一套命令面板会被禁用；会话搜索继续使用纯文字
`/sessions QUERY` 流程，不显示表情符号搜索图标。终端未正常送达尺寸变化通知时，应用还
会校准真实 TTY 单元格尺寸，因此最大化或缩放窗口会重绘整个视口，不会把旧画布留在
左上角。

选择性运行的生产 CLI 冒烟测试现在会发送真实终端输入，而不是依赖无头按键 hook。
Linux/macOS 使用标准库 PTY，Windows 使用标准库 `ctypes` ConPTY 适配器。Windows 路径
覆盖空闲 `Ctrl+C`、`Ctrl+Q`、resize、零/非零退出、有界输出、控制台句柄可用时的父 mode
保持，以及备用屏幕、光标和 focus tracking 按顺序清理。详见
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md)。

可复用的交互式终端底座现在位于这些原生适配器之上，提供带显式丢弃计数的有界游标输出、
原始输入、resize、信号、等待和关闭；权限、工作区和任何已配置沙箱检查通过前不会启动。
POSIX 持有完整 PTY 进程组，Windows 则把 ConPTY 入口原子创建到关闭即终止的 Job 中。
partial ACP 核心不会通过客户端终端 API 暴露该交互式终端底座；交互式 framing 与背压
仍待实现。详见
[ADR 0034](adr/0034-bounded-owned-interactive-terminal-sessions.md)。

用户提示显示为占满整行的低对比度块，助手输出使用独立回答块，不再依靠 `You:` 与
`Assistant:` 日志前缀区分。每条流式回答只在对话流中挂载一次，后续增量直到最终文本都
原地更新，因此完成时不会再从临时区域移动到滚动记录；用户主动向上滚动后也不会被强制
拉回底部。

助手文本使用应用自有的语义配色渲染 Markdown，对标题、强调、代码、列表、链接和表格
进行克制区分。模型文本不会作为 Rich/Textual markup 解释，同时禁用链接点击；用户提示
以及本地或外部值继续按字面文本显示。系统、状态、工具和错误通知使用固定宽度且对齐的
标签栏，供应商/模型、工具/会话、路径、结果、耗时、模式、强度和错误等值再按语义着色。
每个模型步骤会显示客户端观测到首个可行动结果前的耗时；每次工具调用只占用一张原地更新
的调用/权限/结果卡片并显示耗时；整轮完成后会在最终回答下方显示总耗时。完成后的卡片会
保留真实工具输出经过控制字符清理、凭据脱敏和长度限制后的预览；读取、列举、搜索、图片
和技能调用默认只占一条动作说明，仍可原地打开安全预览。带副作用的本地工具还会
按调用显示有界的工作区变更、文件路径和统一文本差异；敏感、二进制、过大、依赖、缓存及
版本库内部内容保持隐藏。成功编辑会自动显示变更切片；新增/删除行分别使用绿色/红色前景
和不同的淡色背景。安全详情可通过单击，或聚焦后按 `Enter`/`Space` 原地收起和重新展开。
当前切片尚不包含 Mermaid 和内嵌媒体。

提示框上方常驻一行运行信息，显示当前供应商与模型、压缩后的工作目录、上下文窗口占用、
请求的思考强度和交互模式。等待模型输出时，用户提供的七格折叠脉冲会在待完成助手文案前动画。
上下文百分比启动时会明确标为本地估算；模型步骤返回用量后，改用供应商报告的输入/输出
token。配置中的 `context_window_tokens` 提供分母；未配置容量时只显示已知 token 用量，
不会编造百分比。受管供应商详情页提供这个本地能力字段，因此每个已配置模型都能填写自己的
真实分母。
请求等级与当前实际策略不同时会同时显示两者，例如 `⚡ ultracode → ⬤ xhigh`。标签会随
界面语言切换，并且在窄窗口中仍保留关键信息。

输入 `/` 时会显示命令语法和参数提示。候选项包含五档强度、四种模式和当前可选择的供应商 profile
名称；自由文本命令则显示 `SESSION_ID`、`QUERY`、`TITLE` 等占位符。按 `Tab` 会应用第一
项有效补全，普通提示文字和模态框中的焦点切换仍保持原行为。

使用 `Ctrl+,`、`/settings` 或 `/setting` 会先打开一级分类页，再选择界面语言、模型供应商或网络与代理默认策略。
供应商详情明确区分 OpenAI Responses（`/responses`）与 OpenAI 兼容 Chat Completions
（`/chat/completions`）；DeepSeek 预设会选择后者并填入 `https://api.deepseek.com`。
网络设置负责全局代理默认策略；供应商详情默认继承它，只有特定供应商需要不同路径时才设置
显式覆盖。两个页面都会在写入前使用与运行时相同的代理策略解析器进行本地校验；该检查不会发送
网络请求。显式点击“测试连接并加载模型”后，应用才会使用草稿中的端点、凭据和代理策略
发送只读目录请求；它不会发送对话，也不会产生模型生成费用。成功结果会成为只存在内存、
数量有界的模型选择器，同时保留手动输入。认证、端点/协议、超时、限流、服务端、代理、
网络和异常目录响应会脱敏留在当前设置页，响应正文不会显示或持久化。受管配置删除仍要求
二次确认。
切换语言会立即更新应用
自有的控件、对话框和状态文案，但不会翻译用户提示、模型回答或工具内容。选择结果与
思考强度及交互模式偏好一起保存到 `$NEURO_CODE_HOME/ui-preferences.json`（通常为
`~/.neuro-code/ui-preferences.json`），该文件与供应商配置分离，后续启动 TUI 时会继续
使用。

TUI 管理的供应商元数据会原子写入 `~/.neuro-code/providers.json`；API 密钥不会进入该
文件或普通 `config.toml`，而是单独写入私有的
`~/.neuro-code/credentials.json`。平台支持 POSIX mode 时，两者均使用仅所有者可读写
权限；工具结果边界还会再次按已配置值脱敏。手工维护
`~/.neuro-code/config.toml` 仍受支持。同名 TUI profile 会完整替换 TOML 供应商表，防止
工作区把已保存密钥重定向到另一个端点。存储端口以后可替换为操作系统钥匙串适配器；
当前凭据文件本身不做静态加密。

使用 `Ctrl+E`、不带参数的 `/effort` 或 `/reasoning` 可以打开五级强度选择器；
`/effort LEVEL` 与 `/reasoning LEVEL` 可直接选择，`--effort LEVEL` 则可用于交互启动或
无头运行。TUI 内的修改会保存为用户偏好，并在以后启动、切换 profile 或进程内恢复会话
后继续应用。交互启动时，显式 `--effort` 优先于保存值；没有显式值或有效保存值时默认
为 `high`，无头运行未指定时同样默认 `high`。活动轮次中不能切换强度，新选择从下一次
模型步骤开始生效。

| 等级 | 标记 | 当前已实现的应用行为 |
|---|---:|---|
| `low` | ○ | 直接回答，只执行保证正确性所必需的最少检查与验证 |
| `medium` | ◐ | 常规检查、自我审查和有针对性的验证 |
| `high` | ● | 更深入地调查并主动检查可能的回归；默认等级 |
| `xhigh` | ⬤ | 面向困难边界情况，主动质疑假设并执行多轮验证 |
| `ultracode` | ⚡ | 当前实际采用 `xhigh` 策略；工作流编排尚未实现 |

这些等级目前表示 Neuro Code 的应用层审查策略，并不宣称控制了供应商私有的模型推理
参数。每次模型请求都会收到不持久化的策略指引，同时在 `ModelContext` 中携带有类型的
请求等级；供应商适配器不会把它盲目翻译成私有 API 参数。未来若增加供应商原生映射，
必须显式声明并按能力启用。选择 `ultracode` 不会启动子代理，界面会明确显示其回退到
`xhigh`。详见
[ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md)。

使用 `Shift+Tab` 可在 `normal`、`accept-edits`、`plan` 和 `auto` 之间循环，也可用
`/mode MODE` 直接选择。`normal` 自动允许读取并询问副作用操作；`accept-edits` 还会自动
允许工作区编辑工具；`plan` 不弹出授权而是直接拒绝副作用。安全分类器实现前，`auto`
会明确标为安全预览，并采用与 `accept-edits` 相同的默认权限，因此命令和网络操作仍需
授权。只有启动时显式使用 `--always-approve` 才保留现有绕过默认值；显式规则和进程沙箱
仍然优先。活动轮次中不能切换模式；模式会保存为 UI 偏好，并在 profile/会话切换后重新
应用。

计划模式还向模型提供供应商中立的 `update_plan` 工具。它会整体替换一份有界结构化计划
（目的，以及 pending、in-progress 或 completed 步骤），把它与 SQLite 会话一同保存，在恢复时
载入，并在分叉会话时复制。可用 `/plan DESCRIPTION` 进入计划模式并立即发起规划请求，或用
`/view-plan`（别名 `/show-plan`）查看当前已保存的计划。用户审阅后可用 `/execute-plan`
（别名 `/run-plan`）显式记录从计划到轮次的交接，并且只切换到 `accept-edits`。每次这样的
轮次都会获得一个仅含元数据的持久会话任务记录：它具有不透明 ID，状态为 `queued`、`running`、
`completed`、`failed` 或 `cancelled`；可在恢复后查看，但不会复制到分叉会话。命令、网络、
工作区和沙箱边界仍然有效。`/comment-plan STEP COMMENT`（别名 `/plan-comment`）会为当前计划的
一个步骤保存有界用户反馈，`/view-plan` 会在相应步骤下显示它。评论只会随下一条提示提供给模型；
它不会批准、执行或调度工作。评论会随当前计划一起分叉，但整体替换计划会丢弃旧评论。可使用
`/schedule-plan`（别名 `/queue-plan`）在不联系模型的情况下持久化当前计划的有界排队副本，再使用
`/run-task TASK_ID` 显式启动这一快照。每个会话最多排队四个计划任务；排队任务不会自动启动、重试、
唤醒或创建子代理。`/tasks` 始终保持紧凑的任务列表。如需查看某一持久计划执行任务的完整不可变计划
快照，用户必须显式运行 `/view-task TASK_ID`；它只会在当前打开的会话中解析该 ID，并把快照作为只读
参考展示。它既不会更改当前计划，也不会启动模型轮次或任何工作。详见
[ADR 0028](adr/0028-timed-tool-feedback-and-interaction-modes.md)
、[ADR 0057](adr/0057-durable-structured-session-plans.md) 与
[ADR 0058](adr/0058-durable-session-task-lifecycle.md)、
[ADR 0059](adr/0059-bounded-current-plan-comments.md) 与
[ADR 0060](adr/0060-plan-execution-revision-snapshots.md) 与
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md) 以及
[ADR 0063](adr/0063-bounded-explicit-plan-task-scheduling.md)。

使用 `Ctrl+P`、不带参数的 `/provider` 或 `/model` 可以打开已配置 profile 选择器；
`/provider PROFILE` 与 `/model PROFILE` 可以直接选择。选择器只展示 profile 名称、模型、
协议和就绪状态；不可用或缺少凭据的 profile 会被禁用。它选择的是已配置 profile，而非
任意远程模型 ID。选择 TUI 管理的 profile 时还会保存为以后启动的默认项；新建和编辑
仍在设置页完成。轮次运行期间禁止切换。切换到不同 profile 时，旧的
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
提示。TUI 会显式请求首 token 前回退策略：如果取消发生在任何非空模型文字/推理、完成事件或
工具活动之前，运行时只保存本轮之前的会话项，把刚提交的用户提示从模型上下文中移除，并将
它恢复到输入草稿。`USER_MESSAGE` 与 `TURN_FAILED` 仍保留在追加式审计事件流中。只要已经产生
输出或工具活动，就保留该提示并使用普通取消恢复路径。在首个非空模型 token 到达前，最多四条
显式后续提示会在界面内缓冲，并在当前轮次完成后按顺序执行；只有真正启动时才会进入会话历史。
如果无法安全回退，轮次取消或失败时第一条排队提示会恢复到输入框。

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

在 TUI 中使用 `/tasks` 可以只读查看当前绑定的存活后台任务元数据，以及持久的计划执行任务
记录。后台任务会显示任务 ID、状态、退出码、有界输出大小和开始时间；计划执行记录会显示不透明 ID、
类别、状态、开始/终态时间、不可变计划修订指纹的前 12 个字符以及已完成步骤数。它绝不会显示提示词、
命令、工具输出或凭据。每个后台任务进入终态时，TUI 会发出一次本地通知，但不会打印命令文本或原始输出。
`/tasks` 不能终止任务；应让模型使用 `kill_task`，使该操作继续经过权限/审批策略。应优先使用
`is_background=true`，而不是在 Shell 内部追加 `&`。对于单条持久计划执行记录，`/view-task TASK_ID`
是独立、需显式调用的只读计划快照查看入口；它只限当前打开的会话，不能执行、重试或修改任何内容。Windows 上每个 `ProcessTree`
都会掌控一个关闭即终止的 Job Object，通过 `CreateProcessW` 在创建时原子加入入口进程，
并在入口退出后继续等待后代；如果扩展创建无法维持该边界，命令启动会显式失败。子进程只
继承显式句柄列表中的空输入和输出管道句柄。详见
[ADR 0021](adr/0021-owned-background-shell-tasks.md)、
[ADR 0031](adr/0031-fail-closed-windows-job-objects.md)、
[ADR 0033](adr/0033-atomic-windows-job-process-creation.md) 和
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。多任务等待语义由
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) 定义。

自然完成还会在下一次明确模型边界报告一次：工具执行后的下一模型步骤，或空闲时由下一条
用户提示触发的轮次。仅供模型使用的每批通知最多包含 20 个任务，只携带经过转义的状态
元数据，不包含命令文本、cwd 或输出。终态 `task_output`、`wait_tasks` 或 `kill_task` 结果
会消费对应通知，防止重复。只有供应商返回有效完成后才确认通知；通知不会作为会话消息
持久化；当有效唤醒策略关闭时，也绝不会自主启动付费模型轮次。Settings 中可以编辑持久的
用户级默认策略，每个受管供应商配置可以继承默认值，也可以显式覆盖。空闲 TUI 会话还可以
使用 `/auto-wake on|off` 临时切换；会话选择优先，并且每个待处理完成批次最多启动一次仅供
模型使用的唤醒轮次，只有供应商返回有效完成后才消费待处理通知，唤醒回答与合成提醒不会
进入持久会话项。新旧配置默认均为关闭。详见
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
model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com"
auth = "env"
api_key_env = "DEEPSEEK_API_KEY"
context_window_tokens = 1000000
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
`context_window_tokens` 是用于本地预算和界面显示的能力元数据，不会作为供应商请求参数
发送。

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
model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "NEURO_DEEPSEEK_PROXY_URL"
```

配置检查只暴露模式、环境变量名和“是否已配置”布尔值；代理 URL 与认证信息会从异常中
脱敏。含义不明确的 `socks://` 会被拒绝，不会擅自猜测；应在安装 HTTPX 可选 SOCKS
依赖后使用 `socks5://` 或 `socks5h://`，或者改用 HTTP 代理。发行包可通过
`neuro-code[socks]` 安装这一可选依赖。TUI 保存受管 profile 前会执行同一校验；如果
当前默认受管 profile 在启动预检中失败，会打开设置页供修复。详见
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

`sessions` 和 `sessions search` 的 JSON 形式会在存在持久终态记录时增加有界的
`last_execution` 投影。它只包含状态、原因、是否收尾、是否可恢复和完成时间；纯文本会话
列表保持不变。

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
