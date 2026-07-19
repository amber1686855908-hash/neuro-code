# Neuro Code 架构

**简体中文** · [English](../en/architecture.md)

## 设计意图

Neuro Code 采用模块化单体架构。它保留有价值的外部行为，但不会照搬历史上游 Cargo
crate 图。所有交互界面消费同一条带类型的运行时事件流。

所有由项目拥有的公共标识都遵循
[ADR 0013](adr/0013-neuro-code-namespace.md) 定义的 Neuro Code 命名空间。

## 系统边界

Neuro Code 负责本地编排：CLI/TUI、代理轮次、模型适配器、工具、权限、工作区、
会话、扩展和协议端点。它不负责模型托管、训练、专有云中继、Computer Hub 服务或
Web 控制台后端。纯云端能力必须通过显式适配器接入；不可用时必须明确报告，不能
模拟成功。

## 依赖方向

```text
界面层（CLI、TUI、ACP、WebSocket）
                    |
应用层（代理循环、会话、命令、任务）
                    |
领域层（消息、事件、工具、权限、错误）
                    |
端口层（模型、存储、工具、工作区、沙箱、钩子）
                    |
适配器（供应商、SQLite、MCP、Git、PTY、操作系统、HTTP）
```

依赖只能向下。领域模块和应用模块不得导入 UI 框架、供应商 SDK、数据库驱动或平台
实现。适配器实现带类型端口，并且只能在组合根中选择。

`ApplicationComposition` 是与界面无关的进程组合服务。它解析配置与供应商 override，
执行会话沙箱预检，初始化 SQLite，创建供应商、工具、权限管理器和会话作用域后台任务
注册表，并持有监督器关闭责任。CLI、TUI 和 ACP 只负责把各自参数或协议值转换给该服务。
应用模块不依赖 argparse、Textual、ACP Schema 类型、ACP Client 或 stdio。

## 运行时事件模型

一次代理轮次是只追加的带类型事件流：

1. 接受用户消息；
2. 可选地产生供应商尝试失败/选择事件，随后接收模型文本/推理增量；
3. 接收零个或多个供应商托管工具生命周期事件和/或本地工具调用请求；
4. 产生权限判定、可选异步审批和本地工具生命周期事件；
5. 把本地工具结果追加到模型上下文；
6. 开始下一模型步骤或以完成/失败结束轮次；
7. 把事件与可恢复的有序上下文提交到会话存储。

运行时负责步骤上限、取消、重试和事件顺序。UI 可以渲染事件，但不得直接修改运行时
状态。后台任务必须由 `asyncio.TaskGroup` 或具有关闭契约的显式注册表管理；禁止没有
引用的即发即弃任务。

受管 Shell 工作使用应用级 `BackgroundTaskSupervisor`，并为每个会话绑定分配隔离的
`BackgroundTaskManager` 注册表。`bash` 可以不等待完成就返回任务 ID；`task_output` 读取
有界快照或短暂等待，`wait_tasks` 通过完成事件等待最多 20 个 ID 中的任意一个或全部，
`kill_task` 则通过普通权限边界终止受控进程树。绑定只能访问自己的
任务 ID；替换绑定会关闭其作用域，组合根退出时一定关闭监督器。任务记录只存在于内存，
不会成为持久会话上下文。详见 [ADR 0021](adr/0021-owned-background-shell-tasks.md) 和
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) 定义多任务等待条件、超时、
取消和输出边界。

每次明确模型步骤时，`AgentRuntime` 会查询该作用域中尚未报告的终态任务，追加最多 20 条
且仅供模型使用、只含元数据的提醒，并在供应商产生有效完成后确认该批次。终态
`task_output`、`wait_tasks` 和 `kill_task` 结果会优先确认同一 ID，防止重复投递。提醒不进入
`SessionItem` 持久化，只有有界审计事件会保存；空闲完成必须等待用户输入，绝不会自行启动
模型轮次。详见
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md)。

## 会话与交互界面

`AgentConversation` 是位于单轮 `AgentRuntime` 之上的可复用应用边界。它串行执行轮次，
并在每次持久提交后继续携带有序会话项、会话 ID 和供应商来源元数据。打开已有会话时，
它会校验记录的工作区与请求工作区是否指向同一文件系统位置。无头 CLI 和 Textual 界面
组合相同的控制器，因此恢复和供应商回放规则不会因界面不同而分叉。

发生失败或取消时，`AgentConversation` 会在释放轮次锁之前，从 `SessionStore` 重新加载
规范有序项和供应商来源。所以下一条提示会复用持久状态，而不是过期的内存前缀。取消的
用户消息仍保留在历史中；首 token 前回退是另一项尚未实现的交互策略。

## Partial ACP v1 适配器

`neuro-code acp` 是位于 `ApplicationComposition` 与官方
`agent-client-protocol` Python SDK 之上的协议适配器。生产 framing、JSON-RPC
路由、换行分隔 stdio、`session/update` notification 和
`session/request_permission` request 均继续由 SDK 持有。适配器只声明
`sessionCapabilities.close = {}`，实现 `initialize`、`session/new`、
`session/prompt`、`session/cancel` notification 和 `session/close`。SDK 0.11
将 `session/close` 路由置于 `use_unstable_protocol` 门后；进程只为使已声明的 close
方法可达而打开该路由门，不实现其他 unstable 方法。

每条 ACP 连接固定绑定到规范化后的启动工作区。每个成功 session 拥有稳定随机 ACP ID、
一个 `AgentConversation`、一个后台任务 scope、一个活动 prompt 槽位，以及独立审批/
取消/关闭状态。内部 SQLite ID 与 ACP ID 保持分离，并在首次 prompt 时按需记录。
所有资源就绪前不会发布 session。close 会先应用 cancel 语义，等待必须的工具终态更新和
prompt 响应，关闭 scope，释放运行时绑定，同时保留持久历史。EOF 或连接故障会对全部
活动 session 执行相同的幂等清理。

提示转换只接受 ACP 基线 Text 与 ResourceLink。block 数量、单字段大小、annotations
序列化、ResourceLink 汇总字节和整轮提示字节都有上限。只有 `uri`、`name`、`title`、
`description`、`mimeType`、`size` 和标准 annotations 字段会进入模型可见的引用描述；
`_meta` 会被忽略。本地 `file:` 与远程链接都不会被读取、下载或解引用；模型随后主动
读取文件时仍必须经过普通工作区/工具边界。

事件投影采用显式白名单：

| 运行时事件 | ACP 投影 |
|---|---|
| `TEXT_DELTA` | 带同一回答稳定 `messageId` 的 `agent_message_chunk` |
| `TOOL_REQUESTED` | `tool_call` / `pending` |
| `TOOL_STARTED` | `tool_call_update` / `in_progress` |
| `TOOL_COMPLETED` | 有界且脱敏的 `tool_call_update` / `completed` |
| `TOOL_FAILED` | 有界且脱敏的 `tool_call_update` / `failed` |
| 有效 `CONTEXT_USAGE_UPDATED` | 上下文窗口已知时发送标准 `usage_update` |
| `REASONING_DELTA`、`TURN_COMPLETED`、`TURN_FAILED` | 不发送自定义 update |

原始 prompt 响应承载 `end_turn`、`max_tokens`、`max_turn_requests`、`refusal` 或
`cancelled`。审批沿用现有失败关闭权限管理器：本地 deny、工作区和沙箱结论始终拥有最终
优先级，pending 工具更新先于客户端请求，批准返回前不能开始执行。本切片会保存协商到的
客户端文件系统与终端能力，但绝不调用这些方法。详见
[ADR 0035](adr/0035-partial-acp-v1-stdio.md)。

最小 TUI 是 `AgentEvent` 之上的表现适配器，负责提示输入、滚动记录、实时文本表面和
本地斜杠命令。它绝不渲染原始推理或不受限制的参数/结果映射；只有路径、命令、模式、
查询与任务 ID 等有界白名单参数可进入调用摘要。每个本地工具调用再按调用 ID 持有一张
稳定卡片，后续在原地更新权限路径、脱敏结果预览、耗时和有界工作区变更报告。详见
[ADR 0014](adr/0014-minimal-event-stream-tui.md) 与
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md)。

滚动记录由稳定消息组件组成的纵向对话实现，而不是“预渲染日志 + 临时流式区域”。用户
提示和助手回答使用不同布局；待完成的助手组件始终位于对话末尾，生命周期通知插入其前，
文本增量和最终回答都更新同一个组件。只有视口本来就在末尾时才自动跟随。详见
[ADR 0026](adr/0026-stable-localized-tui-conversation.md)。

助手组件使用 Rich 的 Markdown 文档模型和应用自有语义主题，同时禁用链接点击；模型
输出绝不会进入 Rich/Textual markup 解析。用户内容以及应用或外部值使用字面 `Text`。
本地系统、状态、工具和错误记录统一使用两列表格：固定宽度标签栏配合可折行正文。
颜色由供应商、模型、工具、会话、路径、结果、耗时、模式、强度和错误等语义值类型决定，
而不是由任意载荷 markup 决定。工具输出与差异使用应用赋予样式的字面 `Text`，绝不会把
载荷当作 markup。有界详情可以聚焦并收起或展开，且不会获取新数据；详见
[ADR 0030](adr/0030-bounded-interactive-tool-card-details.md)。Mermaid 与媒体仍在该渲染器
边界之外。另见 [ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md)。

应用自有 TUI 文案通过 `UiLanguage` 选择。注入的 `UiPreferencesStore` 端口持久化界面
语言、请求的思考强度和交互模式；JSON 适配器使用与供应商配置分离、原子写入且仅用户可访问的状态
文件。值缺失或无效时分别回退到英语、`high` 和 `normal`。英语和简体中文目录必须具有相同键集合。
切换语言会重新渲染界面外壳和可翻译的本地历史，但可见的用户/模型文本以及已经清理的
工具预览不会翻译，也绝不会送入翻译器。

表现适配器持有一套固定的冷色中性深色主题，不暴露 Textual 无关的主题与命令面板表面。
内建命令面板会被禁用，供应商与会话发现通过明确的应用命令完成，会话查询按字面纯文本
渲染。提示框上方的常驻运行栏直接从控制器状态显示当前供应商/模型、压缩后的工作路径、
上下文窗口占用、请求/实际强度及交互模式；本地化、供应商故障转移或用户选择发生变化时主动刷新，不会从对话文本
反向解析状态。上下文用量先对规范会话项进行供应商中立的本地估算；模型完成事件带有
token 元数据时，运行时发出 `CONTEXT_USAGE_UPDATED`，并用供应商报告的输入加输出用量
替换估算值。分母来自显式 profile 元数据 `context_window_tokens`；字段缺失时保持未知。

斜杠补全是与命令执行分离的确定性表现目录。它投影强度/模式选项和可选择的脱敏 profile 名，
为自由文本参数显示占位符，并同时驱动行内建议与可见提示栏。只有主提示框包含斜杠命令
时，TUI 的高优先级 Tab 动作才会应用第一项候选；模态框焦点切换保持原行为。在全屏终端
模式中，低频视口校准会读取真实 TTY 尺寸，并且只在当前 Screen 尺寸过期时发送正常的
Textual resize 事件；无头测试、行内模式和 Web 模式不会安装该兜底。

Textual 的平台驱动持有 raw/应用模式并负责恢复终端状态；应用不会重复持有转义序列或
`termios`。`run_async` 返回后，CLI 传播 Textual 的公开 `return_code`；组合根则通过
`finally` 在正常退出、Textual 非零结果或启动异常时关闭后台任务监督器。选择性运行的生产
CLI 冒烟测试会在 Linux/macOS 标准库 PTY 与 Windows ConPTY 中发送真实 `Ctrl+Q`，且不
提交模型提示。测试验证备用屏幕、光标与 focus tracking 按顺序退出；POSIX 还会比较完整
`termios`，Windows 则覆盖 resize、空闲 `Ctrl+C`、非零退出码保持，以及任何可用父控制台
mode 的比较。私有标准库 `windows_conpty` 适配器掌控同步管道、扩展进程创建、有界捕获和
一个在 `ClosePseudoConsole` 期间继续工作的专用输出排空线程。详见
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md)。该进程边界沿用固定历史
基线中只读 `crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs` 的行为证据，但不复制其
Rust 实现。

原生适配器之上，`LocalInteractiveTerminalManager` 实现共享
`InteractiveTerminalManager` 端口。创建必须在启动前依次经过权限、工作区和匹配沙箱
检查；线程安全的有界尾部环形缓冲通过单调输出游标暴露准确丢弃字节数，输入、resize、
信号、等待和关闭共享同一条受控生命周期。POSIX 作用于完整 PTY 进程组；生产 Windows
ConPTY 创建会原子组合伪控制台与 Job 列表属性，终止/关闭作用于整个 Job。取消会等待进行中
的原生创建并关闭任何返回的所有者；shutdown 会等待创建中会话并关闭全部注册会话。在
协议 framing、授权和背压定义完成前，该基础有意不通过 ACP 暴露。详见
[ADR 0034](adr/0034-bounded-owned-interactive-terminal-sessions.md)。

运行时计时使用单调时钟。`MODEL_THINKING_COMPLETED` 测量每个模型步骤从发出请求到首个
可见或可行动结果的时间，并不宣称能够读取供应商私有推理遥测。工具终态事件携带耗时，
`TURN_COMPLETED` 则在稳定助手节点之后显示整轮摘要。工具调用、权限路径、输出预览、
工作区变更和终态会在同一张原地更新的有界树状卡片中渲染。对于带副作用的本地工具，
运行时只在权限通过后、紧邻执行前后比较有界的只读工作区快照；这份报告只是审计元数据，
既不授予权限，也不代表执行成功。详见
[ADR 0028](adr/0028-timed-tool-feedback-and-interaction-modes.md) 与
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md)。

对于当前会话作用域，本地 `/tasks` 只渲染有界任务元数据，不显示命令文本或输出；周期
只读轮询会为每个终态转换发出一次通知。它不能修改任务状态；`kill_task` 仍走普通模型
工具与权限路径。详见
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。

TUI 在 Worker 管理的轮次运行时保持提示框可用。`Ctrl+C` 与本地 `/cancel` 会取消该
Worker；审批模态框则把 `Ctrl+C` 限定为拒绝待处理请求。运行时拥有的恢复与工具结果
配对规则见 [ADR 0016](adr/0016-recoverable-turn-cancellation.md)。

`ProfileConversationController` 还持有 `InteractionMode`，让模式切换与活动轮次串行，并
把选择重新应用到替换后的绑定。`normal`、`accept-edits` 与 `plan` 映射为确定性的权限管理
模式；安全分类器实现前，`auto` 默认采用安全的 `accept-edits` 预览，只有显式授权的
`--always-approve` 启动会保留绕过默认值。提示词指引只描述模式，真正权限只来自权限、
工作区和沙箱适配器。详见 ADR 0028。

交互组合使用 `ProfileConversationController` 包装当前 `AgentConversation`。它让 profile
选择与轮次串行执行，并且只向 TUI 暴露脱敏的 `ProviderOption` 数据。选择另一个已配置
profile 时，组合根创建不恢复任何会话的新供应商/运行时/会话绑定，旧 SQLite 会话保持
不变。这条严格边界避免跨供应商回放加密推理、托管工具状态、方言元数据和 profile 亲和
上下文。详见 [ADR 0017](adr/0017-safe-interactive-profile-selection.md)。

该控制器还持有一项进程内 `ReasoningEffort` 选择，并让强度切换与轮次串行。profile 或
会话切换安装新对话绑定时，会把请求等级重新应用到新绑定。`low`、`medium`、`high` 与
`xhigh` 对应应用层审查指引；在工作流编排实现前，`ultracode` 的明确实际值是 `xhigh`。
TUI 通过 `Ctrl+E`、`/effort` 和 `/reasoning` 暴露选择，CLI 则使用 `--effort`。选择不会
改写供应商配置，也不成为会话身份。

每次模型步骤开始时，`AgentRuntime` 会把所选指引加入仅用于本次请求的系统消息，并将
有类型的请求值写入 `ModelContext`；该指引不会加入规范 `SessionItem` 历史。供应商
适配器可以读取这个类型值，但当前适配器不会把它翻译成供应商私有推理参数。未来若增加
原生映射，必须显式声明能力并提供相应测试。详见
[ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md)。

同一控制器还暴露限定工作区的 `SessionOption` 目录，并让会话选择与轮次串行。组合根
按文件系统身份过滤近期 SQLite 摘要，随后由 `AgentConversation.open` 再次校验所选 ID。
恢复时优先使用已就绪且名称匹配来源的 profile，否则使用当前就绪 profile，同时保留
保存的供应商、模型和亲和来源，以失败关闭地投影原生上下文。TUI 会把滚动记录替换为
有界的可见消息投影，其中不包含推理、原生记录、参数、图片 URL 或原始工具结果内容。
详见 [ADR 0018](adr/0018-workspace-scoped-interactive-session-resume.md)。

同一目录还具有独立的相关性搜索路径。`SessionStore` 从同步的 SQLite FTS5 投影返回带类型
标题/内容结果；组合根先按文件系统身份过滤工作区，控制器才创建 `SessionOption`。
`/sessions QUERY` 显示保存标题或确定性的首提示标题，并可附加按字面文本渲染的摘要。
系统消息、供应商保留项、assistant 私有推理、工具参数/元数据、原始工具结果内容和图片 URL
永远不会进入该投影。
详见 [ADR 0025](adr/0025-session-title-and-full-text-search.md)。

手动重命名遵循同一边界。`SessionStore.update_session_title` 返回更新后的规范摘要，并以
原子方式修改 SQLite 标题、更新时间和同步 FTS 文档。TUI 组合根只允许重命名当前文件
系统身份工作区中的会话，控制器则让重命名与模型轮次串行；CLI 调用方可以在所选状态
数据库中按明确 ID 重命名。

操作系统沙箱也是会话身份的一部分。原生会话会保存创建时的规范 profile。按明确 ID 启动
恢复时，会在强制进程沙箱之前通过 immutable 只读 SQLite 查询元数据；除非显式 CLI 或
环境变量请求经规范化后不同并形成冲突，否则还原保存值。应用内 TUI 无法替换不可逆的
进程沙箱，因此会禁用不同 profile 的选项并要求重启。普通摘要加载后，
`AgentConversation.open` 还会再次校验。详见
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md)。

权限策略与用户交互是两个独立边界。`PermissionManager` 先返回确定性判定；`ask` 随后
可以进入可选的异步 `PermissionApprover` 端口。运行时产生请求/结果审计事件，并且在
收到允许结果之前不能产生 `tool_started`。TUI 会话代理只在内存中记住“精确工具/参数
组合”的哈希；后续每次调用仍重新经过策略判定，从而保持 deny 优先级。无头组合不提供
审批器并继续失败关闭。详见
[ADR 0015](adr/0015-async-interactive-tool-approval.md)。

## 稳定端口

- `ModelProvider`：把有序 `ModelContext` 和工具 schema 转换为模型事件；它暴露所选
  profile 身份和不含密钥的亲和指纹。上下文携带会话来源 profile/模型/亲和元数据，
  供适配器自行作出回放决策，同时携带供应商中立的请求思考强度，供显式能力处理。
- `Tool`：发布 JSON schema，并在受限 `ToolContext` 中执行。
- `ToolRegistry`：解析规范工具名称并拒绝重复注册。
- `ShellSandbox`：把 Shell 字符串转换为参数边界安全、由平台强制执行的启动计划，
  无需向工具暴露命名空间实现细节。
- `BackgroundTaskSupervisor`：创建隔离的会话任务作用域，并在应用关闭时终止每一棵仍存活
  的进程树。
- `BackgroundTaskManager`：在单个会话作用域内启动受控 Shell/exec 进程树，并提供有界
  快照/单任务或多任务等待/终止及待报告完成确认操作。
- `InteractiveTerminalManager`：创建经过权限、工作区与沙箱门禁的有界交互式 exec
  会话，并持有其关闭生命周期。
- `TerminalPlatform`：在一个同步适配器端口后统一 POSIX PTY 或 Windows ConPTY/Job 的
  输入、输出、resize、信号、等待和关闭行为。
- `PermissionManager`：在任何副作用之前返回 allow、deny 或 ask。
- `PermissionApprover`：可选地异步解决 `ask`，但不能覆盖策略拒绝。
- `SessionStore`：追加带版本事件、保留有序 `SessionItem`，提供规范序列与普通消息
  投影，并返回带类型、可分页的会话标题/内容搜索页。
- `PlatformAdapter`：封装 PTY、进程、信号、路径、剪贴板和沙箱差异。

外部边界的协议模型必须版本化。内部状态优先使用冻结 dataclass 和枚举。未经校验的
字典不得跨越模块边界，已校验的 JSON 载荷除外。

## 供应商 profile 与兼容网关

组合根选择命名 `ProviderProfile`；代理运行时不会按商业供应商名称分支。profile 将线路
协议（`openai-chat`、`openai-responses`、`anthropic-messages` 或
`gemini-generate-content`）与 xAI Responses 等可选方言行为分离。凭据只能是环境变量
引用或通过校验的回环代理占位符，不能作为密钥持久化。

可选的正整数 `context_window_tokens` 记录供应商/模型能力元数据。它通过脱敏 profile 选择
和故障转移事件传播，用于本地预算，但绝不会序列化为 API 请求参数；真实上限仍由模型
端点执行。

CC Switch 是可选配置源和 HTTP 网关，不是应用依赖。其导出的活动 profile 只在配置
边界转换为内存对象；项目配置优先级更高，CC Switch 数据库和进程控制 API 不会
进入领域层或应用层。详见 [ADR 0010](adr/0010-provider-profiles-and-cc-switch.md)。

可选路由包装器负责一条有序、按需构造的供应商候选链。供应商产生的第一个事件就是
提交点：在此之前发生配置或供应商错误时可以推进到下一个候选项；在此之后发生的错误
会直接终止当前模型步骤。某个候选项一旦成功，同一进程运行期间的选择只会向前推进，
不会回切。尝试失败和选择结果会作为显式运行时事件，而不是隐藏在日志里。无论候选项
直连端点还是经过 CC Switch 网关，规则都相同。详见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)。

每个 profile 还会在构造适配器时解析一个 `HttpClientPolicy`。环境模式把标准代理/证书
环境变量交给 HTTPX；直连模式关闭 HTTPX 环境信任；显式模式从指定环境变量读取一个
代理 URL。解析后的策略为所有供应商适配器提供相同的客户端选项和错误脱敏。代理 URL
不会进入领域事件、配置检查输出或持久化配置。详见
[ADR 0012](adr/0012-provider-http-proxy-policy.md)。

供应商适配器统一文本、推理、工具调用、结束原因和 Token 用量。需要跨工具轮次保留的
供应商专属状态存入可选的 `ToolCall.metadata`，键必须带供应商命名空间；该映射随消息
持久化，应用层把它视为不透明数据。属于供应商工具调用连续性契约的流式 assistant
推理会单独存入仅允许 assistant 使用的可选 `Message.reasoning_content`。OpenAI 兼容
对于新生成的轮次，OpenAI 兼容适配器只会在同一条 assistant 消息包含工具调用时回传
该字段；已完成且没有工具调用的推理不会回传。供应商亲和的导入可见推理遵循 ADR 0007
定义的独立有序投影。

终态 `ModelCompleted` 事件还可以携带供应商原生保留项和规范响应文本。运行时会把这些
项目插入 assistant 消息之前，把终态文本作为持久化和后续模型输入的真值，同时继续把
流式增量作为 UI 事件。随后提交的是完整 `SessionItem` 序列，而不只是消息投影。这样
可以把及时渲染与字节稳定的上下文回放分离开来。

供应商托管工具与本地工具刻意使用不同事件类型。`backend_tool_started` 和
`backend_tool_completed` 表示已由供应商负责并执行的工作；应用层绝不会把它们送入
`PermissionManager`、`ToolRegistry` 或本地工具结果消息合成。本地从
`tool_requested` 到 `tool_completed`/`tool_failed` 的事件仍遵守现有权限和工作区
边界。xAI Responses 适配器会去重流式生命周期通知；如果中间事件缺失，则根据终态
后端输出补出一对开始/完成事件。

## 安全不变量

- deny 规则优先于 allow 规则和绕过模式。
- 无头执行把未解决的 `ask` 转换为拒绝。
- 具有副作用的工具在等待审批、被拒绝或审批等待取消后都不能启动。会话批准只覆盖
  完全相同的工具/参数摘要，仅保留在内存中，并从属于新的策略判定。
- 助手消息中持久化的每个本地工具调用，在上下文再次使用之前必须恰好具有一个工具结果。
  取消会给当前调用以及同一模型批次中的所有剩余调用记录错误结果。
- 写入前必须解析并校验目标；工作区工具不能通过 `..` 或符号链接逃逸。
- 平台无法实施显式沙箱要求时必须失败关闭。
- 沙箱激活标记本身不足以作为证据。Linux 组合层会在暴露工具前校验根目录、工作区与
  状态目录的挂载标志；`strict` 还会校验白名单根目录的文件系统类型。
- `read-only` 会移除编辑工具并在直接调用时再次拒绝。`read-only` 与 `strict` 的 Shell
  后代不继承父代理的网络命名空间，而父进程仍可执行供应商 HTTP 请求。
- inspect 输出、日志、会话事件和异常都不得包含密钥。
- Bash 后代不会继承已配置的供应商 API Key 环境变量，也不会继承标准/显式代理变量；
  密钥访问以后必须通过显式能力提供，不能依赖进程环境。
- API 与代理凭据只能通过环境变量引用；解析后的代理 URL 保留在适配器内部，并从网络
  异常中移除。
- 只有在候选供应商产生第一个模型事件之前才能故障转移；越过该边界后，错误必须直接
  上抛，不得在其他供应商上重放当前步骤。
- 交互式 profile 切换必须与轮次串行，并从全新会话开始；不得把旧会话重新标记或回放
  到新 profile 下。
- 交互式会话选择只能列出文件系统身份相同的工作区，打开时重新校验 ID，并且只有成功
  恢复后才替换活动绑定。即使用当前 profile 恢复普通消息，也必须保留保存的供应商/
  模型/亲和来源。
- 带沙箱元数据的会话始终使用创建 profile 恢复。显式 CLI/环境请求经规范化后不同、
  应用内选择不同 profile、保存值损坏或不受支持时，都必须在模型轮次或工具动作前失败
  关闭。
- 恢复到 TUI 的历史绝不能渲染持久化推理、供应商原生记录、工具参数、图片 URL 或
  原始工具结果内容。
- 会话搜索只能索引本地可见投影。交互结果继续限定工作区，保存的查询、标题和摘要按
  字面文本渲染，不能解释为 UI markup。
- 取消必须终止受所有权控制的子进程、提交终态失败、保存配对完整的上下文，并在下一轮
  会话开始前重新加载该上下文。
- Shell 命令在受所有权控制的进程组中运行。超时和取消先尝试优雅终止整个进程树，
  有界宽限期后再强制终止；输出按固定内存上限持续排空。
- 后台 Shell 命令始终归应用监督器所有，并且只能通过所属会话作用域访问。合并输出预览、
  运行任务数、保留记录数、等待区间和生命周期均有上限；替换绑定或应用退出会终止对应的
  存活进程树。
- 面向模型的完成提醒只包含经过 JSON 转义的 ID/状态元数据，每个模型边界有数量上限，
  排除命令/输出/cwd，并且只在供应商完成或规范终态任务工具结果后确认。
- 限制性 Bash 规则检查每一个可安全分解的命令段，包括常见包装器和嵌套
  `bash -c`。deny/ask 策略可能适用时，无法分类的脚本必须失败关闭。
- 旧上游状态只能只读导入，不能原地修改。

## 持久化

SQLite 是会话及其有序事件的规范事务存储。JSON 和 Markdown 用作交换/导出格式。
数据库暴露整数 schema 版本；每次变更必须包含前向迁移、fixture 覆盖和已记录的兼容
决策。schema v3 新增可空的规范沙箱 profile：新会话保存值，迁移后的旧会话保留
`NULL`。schema v4 新增稳定的可选标题和由触发器同步的外部内容 FTS5 投影；迁移会在
没有导入标题时从第一条可见用户消息生成十词标题，并回填包含转义字符的对话内容，但
不索引供应商私有项。启动时可通过 immutable 只读连接检查沙箱字段，且发生在创建/迁移数据库或
激活进程沙箱之前。Rust 会话由独立的只读适配器解析。该适配器校验格式版本 0 和 1，以明确上限
读取 JSONL 记录，把受支持的新旧记录转换为有序 `SessionSnapshot`，并报告损坏或
不支持的记录，而不是静默编造内容。SQLite 适配器在单个事务中插入快照，并保留其
ID、工作区、模型和时间戳；ID 已存在时不做任何修改并返回失败。源会话文件永远不会
以写入模式打开。恢复授权按文件系统身份比较已记录工作区与请求工作区，并以规范化路径
作为回退，因此可以接受平台路径别名，同时仍拒绝不同工作区。

规范序列由普通 `Message` 和不透明但经过校验的 `PreservedContextItem` 联合组成。
消息内容项保留文本/图片顺序及图片 URL；推理和后端工具载荷保留供应商 JSON 与相对
顺序。运行时会把完整有序序列带入每个模型步骤，应用层视图仍使用普通消息投影。恢复
导入会话时，存储只允许在原前缀后追加，并拒绝改写已保存的上下文。JSON 导出格式
版本 4 同时包含两个投影、会话沙箱 profile 和可选标题。供应商适配器会校验图片引用，并且只在协议角色和 URI 形式
受支持时使用原生多模态内容块；其他图片无需执行适配器侧媒体 I/O，直接降级为可见
占位文本。保留上下文遵循失败关闭的亲和策略。只有来源标记可信的 Rust 导入会话才能
向 xAI 官方 HTTPS Chat Completions 端点发送可见推理与后端工具摘要；不透明加密
内容和所有非亲和目标都会被过滤。通用 Responses 适配器使用 `store: false`；可选 xAI
方言会请求加密推理并支持托管工具。不透明输出只有在保存的 profile 亲和指纹完全匹配
时才能回放；没有指纹的旧 Rust 导入继续采用更严格的 xAI 官方 HTTPS/来源标记回退规则。
回放前仍会剥离仅供输出使用的推理状态。详见
[ADR 0004](adr/0004-ordered-session-items.md) 和
[ADR 0005](adr/0005-provider-native-image-replay.md)。新生成的思考模式工具轮次改走带类型
消息路径；详见 [ADR 0006](adr/0006-thinking-tool-continuity.md)。导入上下文亲和规则见
[ADR 0007](adr/0007-provider-affine-context-replay.md)；Responses 原生回放规则见
[ADR 0008](adr/0008-xai-responses-native-replay.md)；xAI 托管工具的配置与生命周期归属见
[ADR 0009](adr/0009-xai-hosted-tools.md)；通用 profile 决策见
[ADR 0010](adr/0010-provider-profiles-and-cc-switch.md)；安全的输出前故障转移规则见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)；供应商 HTTP 传输选择规则见
[ADR 0012](adr/0012-provider-http-proxy-policy.md)。

Rust 边界还会对旧 assistant 记录执行有界的内存升级。`raw_output` 中携带上下文的
条目、单体 `reasoning` 和 v0 `reasoning_content` 会被提升到对应 assistant 之前。
读取流范围内会维护独立后端工具 ID 集合，仅抑制重复的内嵌副本；推理项保持原顺序，
绝不合并。损坏和未知的内嵌条目会分别计数，但不会导致原本有效的 assistant 整行
被拒绝。

## 平台策略

Linux、macOS 和 Windows 都是一等 CI 目标。平台专属代码隔离在适配器后。内核沙箱
和进程隔离可以使用小型原生辅助程序或系统设施，但业务与编排逻辑必须保留在 Python
中。不受支持的安全保证必须在启动时报告，绝不能静默降级。

第一版具体实现会在 Linux 上使用 bubblewrap 重新执行 `workspace`、`read-only` 和
`strict` 运行；`off` 仍是可移植默认值。文件系统挂载会同时约束进程内 Python 工具与
后代进程。独立的 `ShellSandbox` 启动计划会把 `read-only` 和 `strict` 下的 Bash 后代
放入嵌套网络命名空间。macOS 与 Windows 当前会拒绝显式非 `off` profile，而不会宣称
未执行的安全能力。详见
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) 和
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md)。

前台和受管后台 Shell 命令共享 `ProcessTree`。POSIX 等待会在 Shell 入口退出后继续观察
受控进程组；终止使用有界 TERM→KILL 序列。Windows 上的惰性 ctypes 平台适配器会在
启动进程前创建关闭即终止的 Job Object，通过 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 传入借用
句柄，并创建一个已经属于该 Job 的入口进程。同一次 `STARTUPINFOEXW` 调用还通过
`PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 把继承范围限制为空输入与选定输出管道句柄。独立的
读取和等待线程会把同步 Win32 句柄投影到现有 `asyncio.StreamReader` 与进程等待契约，
不依赖 asyncio 私有 transport。创建、属性、管道、等待、记账和关闭失败都会失败关闭；
系统不会用 `taskkill`、挂起进程竞态或 breakaway 回退弱化宿主约束。详见
[ADR 0021](adr/0021-owned-background-shell-tasks.md)、
[ADR 0031](adr/0031-fail-closed-windows-job-objects.md)、
[ADR 0033](adr/0033-atomic-windows-job-process-creation.md) 和
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。面向模型的完成元数据由
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md) 定义，事件驱动的
多任务等待由 [ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) 定义。
