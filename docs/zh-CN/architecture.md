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

最小 TUI 是 `AgentEvent` 之上的表现适配器，负责提示输入、滚动记录、实时文本表面和
本地斜杠命令。它把供应商与工具生命周期事件收敛为状态消息，并且刻意不渲染原始推理、
通用工具参数映射或工具结果。详见
[ADR 0014](adr/0014-minimal-event-stream-tui.md)。

表现适配器持有一套固定的中性深色主题，不暴露 Textual 无关的主题与命令面板表面。
内建命令面板会被禁用，供应商与会话发现通过明确的应用命令完成，会话查询按字面纯文本
渲染。在全屏终端模式中，低频视口校准会读取真实 TTY 尺寸，并且只在当前 Screen 尺寸
过期时发送正常的 Textual resize 事件；无头测试、行内模式和 Web 模式不会安装该兜底。

对于当前会话作用域，本地 `/tasks` 只渲染有界任务元数据，不显示命令文本或输出；周期
只读轮询会为每个终态转换发出一次通知。它不能修改任务状态；`kill_task` 仍走普通模型
工具与权限路径。详见
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。

TUI 在 Worker 管理的轮次运行时保持提示框可用。`Ctrl+C` 与本地 `/cancel` 会取消该
Worker；审批模态框则把 `Ctrl+C` 限定为拒绝待处理请求。运行时拥有的恢复与工具结果
配对规则见 [ADR 0016](adr/0016-recoverable-turn-cancellation.md)。

交互组合使用 `ProfileConversationController` 包装当前 `AgentConversation`。它让 profile
选择与轮次串行执行，并且只向 TUI 暴露脱敏的 `ProviderOption` 数据。选择另一个已配置
profile 时，组合根创建不恢复任何会话的新供应商/运行时/会话绑定，旧 SQLite 会话保持
不变。这条严格边界避免跨供应商回放加密推理、托管工具状态、方言元数据和 profile 亲和
上下文。详见 [ADR 0017](adr/0017-safe-interactive-profile-selection.md)。

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
  供适配器自行作出回放决策。
- `Tool`：发布 JSON schema，并在受限 `ToolContext` 中执行。
- `ToolRegistry`：解析规范工具名称并拒绝重复注册。
- `ShellSandbox`：把 Shell 字符串转换为参数边界安全、由平台强制执行的启动计划，
  无需向工具暴露命名空间实现细节。
- `BackgroundTaskSupervisor`：创建隔离的会话任务作用域，并在应用关闭时终止每一棵仍存活
  的进程树。
- `BackgroundTaskManager`：在单个会话作用域内启动受控 Shell/exec 进程树，并提供有界
  快照/单任务或多任务等待/终止及待报告完成确认操作。
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
受控进程组；终止使用有界 TERM→KILL 序列。Windows 目前仍使用进程组加
`taskkill /T /F`，完整对齐还需要 Job Object 所有权。详见
[ADR 0021](adr/0021-owned-background-shell-tasks.md) 和
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md)。面向模型的完成元数据由
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md) 定义，事件驱动的
多任务等待由 [ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) 定义。
