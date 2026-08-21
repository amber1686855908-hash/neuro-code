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
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

目标包边界包括：承载纯值与规则的 `domain`、负责编排的 `application`、定义应用所需
抽象的 `application/ports`、包含具体出站适配器的 `infrastructure`、包含入站适配器的
`interfaces`、负责配置/工厂/装配的 `bootstrap`，以及容纳小型跨层原语的 `shared`。
bootstrap 是唯一允许同时依赖 interfaces、application 和 infrastructure 的层。domain
和 application 不得导入具体 infrastructure 实现。
canonical 进程入口位于 bootstrap。少数从入站层到 bootstrap 的兼容和启动边由 AST 护栏逐条
记录；这不授予任何界面自行装配具体依赖的权限。

阶段 1 已将 `neuro_code.shared.{errors,async_utils,redaction}` 和
`neuro_code.application.ports.*` 建立为 canonical 路径。开发阶段的 breaking cleanup 已移除
根级 shared compatibility 模块 `neuro_code.{errors,async_utils,redaction}` 和
`neuro_code.ports`；shared 原语和端口契约仅可通过各自的 canonical 路径获得。
`neuro_code.shared.ui_language` 现在拥有跨层 `UiLanguage` 原语；原
`neuro_code.domain.ui_preferences` facade 已移除。UI 偏好端口、持久化、TUI 和本地化文案均使用
shared owner，同时不改变语言值或持久化行为。
阶段 2A 将 `neuro_code.application.settings.ApplicationSettings` 和
`neuro_code.bootstrap.composition.ApplicationComposition` 建立为 canonical 路径。
`neuro_code.application` 仅保留惰性的 `ApplicationSettings` 包级导出；组合必须从
`bootstrap.composition` 显式导入，因此普通的 `application.ports` 导入不会加载 bootstrap 或
具体 infrastructure。审批交互契约现在只位于 `neuro_code.application.permissions.contracts`。
开发阶段的 breaking cleanup 已移除根级的 `PermissionApproval`、`PermissionApprovalKind`、
`PermissionRequest` 和 `build_permission_request` re-export；
原 `neuro_code.permissions` 模块也已移除；权限策略只从
`neuro_code.application.permissions.policy` 提供。

阶段 2B 将 `neuro_code.bootstrap.entrypoints` 建立为 canonical 的 CLI/TUI 启动入口，console
scripts 和 `python -m neuro_code` 都直接使用它。它只在相应命令实际需要时选择应用组合、SQLite
会话存储、历史会话导入器、TUI 设置/目录/偏好端口和工作区身份行为。`neuro_code.cli` 保留参数
解析、分发、渲染和退出码处理；其注入式 `run` 函数由 canonical bootstrap entrypoint 调用。导入
CLI 不会加载 bootstrap、adapters 或 providers，也不会创建资源。

阶段 2C 保持 `neuro_code.acp` 原位置，作为 ACP/JSON-RPC 入站适配器，但只向它提供
`application.acp` 契约和 ACP 专用应用服务。该服务暴露绑定创建和安全恢复准备、会话别名与列表、
工作区校验、协议元数据以及按会话惰性创建的 MCP 工具上下文。`bootstrap.entrypoints` 将
`ApplicationComposition`、会话存储、工作区身份校验和具体 stdio MCP 工具集合适配到这些契约，
随后启动 server。`serve_acp` 只接受所得的 `AcpApplicationService`，不再适配
`ApplicationComposition` 调用方。ACP 不再导入 MCP 或工作区实现，也不再直接读取组合根配置或存储；导入 ACP 不会
加载 bootstrap、MCP adapter、SQLite 存储或 providers。

Agent harness 行为现阶段位于 `neuro_code.application.runtime` 的明确 canonical 子模块：
`background_task_reminders`、`agent`、`conversation` 以及循环、上下文、工具和终结模块。
交互式审批协调由 `neuro_code.application.permissions.broker` 拥有；原
`neuro_code.application.runtime.approval` 路径只保留单向兼容 facade。Profile 与交互式终端会话协调位于
`neuro_code.application.sessions` 的 canonical owner。绑定级 `InstructionTracker` 与
`SkillTracker` 位于 `neuro_code.application.memory` 的 canonical owner。
只读会话目录与检查查询位于 `neuro_code.application.sessions.catalog`；生命周期服务委托这些投影，
但不迁移会话写入或会话对话控制权。
单个会话回合的类型化边界位于 `neuro_code.application.sessions.turns`；
`SessionApplicationService` 仅保留兼容性的绑定辅助方法，回合运行器继续拥有锁、持久化上下文、事件发送和取消。
共享的 Provider 选择投影位于 `neuro_code.application.providers.contracts`。profile 会话控制器仍拥有绑定替换
与会话选择，Provider 应用服务以及接口/bootstrap 消费者则使用 Provider 契约接缝。历史 profile 与 runtime 导入
继续作为保持 identity 的兼容 re-export。
类型化会话绑定契约位于 `neuro_code.application.sessions.binding`。ACP、bootstrap、会话应用服务以及面向
Runtime 的消费者使用其中的 `ConversationBinding` 与 `ConversationRunner` 类型；`ProfileConversationController`
继续拥有 profile 专属的会话选择与绑定替换。历史 profile 与 runtime 导入继续作为保持 identity 的兼容
re-export。
不可变的会话选择与交互策略投影位于 `neuro_code.application.sessions.contracts`。TUI 从该接缝直接消费
`SessionOption`、`SessionSelectionResult`、`ReasoningEffortSelectionResult` 和 `InteractionModeSelectionResult`；
`ProfileConversationController` 继续拥有选择、策略应用、锁和绑定替换。历史 profile 与 runtime 导入继续作为
保持 identity 的兼容 re-export。
交互式会话列表、选择和重命名现在使用非拥有型的
`neuro_code.application.sessions.selection.SessionSelectionService` 接缝。profile 控制器仍是生命周期 owner；
TUI 通过该门面执行这些操作，同时仅保留兼容性控制器引用以取得现有执行记录投影。
类型化的持久会话生命周期命令使用规范的
`neuro_code.application.sessions.lifecycle.SessionLifecycleService` 接缝。
Runtime 会话创建、CLI 导入/重命名以及 ACP 分叉/删除都消费其已校验的请求类型；旧的
session application service 保持 identity 兼容委托。工作区可见性、binding 替换、回合锁、协议清理
以及执行记录投影仍由现有 owner 负责。
只读会话任务查询使用规范的
`neuro_code.application.sessions.task_queries.SessionTaskQueryService` 接缝。
Runtime 与 `AgentConversation` 消费其经过校验的列表/单任务请求，宽泛的 session service
仍为旧调用方保留保持 identity 的委托。任务创建、排队、状态转换、权限、执行、锁、取消以及所有
SessionStore/SQLite 写入仍由现有会话/Runtime owner 负责。
只读会话摘要查询使用规范的
`neuro_code.application.sessions.summary.SessionSummaryQueryService` 接缝。
会话恢复、bootstrap 配置、ACP 工作区校验和会话作用域工具输出 artifact 读取使用其经过验证的请求；
宽泛的 session service 为旧调用方保留保持 identity 的兼容委托。生命周期写入、事件/会话项读取、schema、
事务、Runtime、Provider、Finalizer 与 wire 行为仍由原有 owner 负责。
只读执行记录投影使用规范的
`neuro_code.application.sessions.execution_queries.SessionExecutionQueryService` 接缝。
会话目录和会话恢复/重载路径共享其单条及有界批量请求，宽泛的 session service 为旧调用方保留保持
identity 的兼容导出。执行记录写入、schema、事务、Runtime、Provider、Finalizer、TUI、ACP 与 wire 行为仍由
原有 owner 负责。
复制后的会话事件投影使用规范的
`neuro_code.application.sessions.event_queries.SessionEventQueryService` 接缝。
会话导出和会话作用域工具输出 artifact 读取共享其类型化请求与外层不可变 mapping 投影。事件行仍是不可信
存储数据，而不是第二套领域事件模型；事件写入、解码、事务、Runtime、Provider、Finalizer、TUI、ACP 与 wire
行为仍由原有 owner 负责。
开发阶段的 breaking cleanup 已移除 `neuro_code.runtime`；运行时应用行为仅可通过这些
明确的 canonical 子模块获得。`neuro_code.application.runtime.__init__` 现阶段保持最小，
不提供 aggregate API；内部生产代码直接导入 canonical 子模块。

`neuro_code.configuration.app` 负责 `AppConfig` 和 `ProviderProfile`、TOML 与 CC Switch
配置、环境覆盖、路由、managed overlay、sandbox 策略、stored credential 注入以及 HTTP
proxy policy。`neuro_code.configuration.managed_provider_settings` 中的同步 managed JSON
reader 负责 schema、protocol 和 dialect 检查、文件大小限制、metadata/credentials 合并、
结构校验以及 `ManagedProviderSettings` 构造。managed provider 值对象和持久化契约的
canonical owner 是
`neuro_code.application.ports.provider_settings`；原
`neuro_code.domain.provider_settings` facade 已移除。这样配置和基础设施消费者
都通过 application port 边界工作，同时不改变校验或持久化行为。
`JsonProviderSettingsStore` 由 `neuro_code.infrastructure.providers.provider_settings` 负责，
包括异步持久化、原子写入和 POSIX 私有权限。它通过私有绑定使用 canonical reader。
导入 provider-settings adapter。原 `neuro_code.config` facade 已移除，调用方应直接使用
`neuro_code.configuration.app`；该边界中的 `ProviderProfile` 和 `AppConfig` 取代已移除的
`ProviderConfig` alias。当前 active temporary allowlist 为空。唯一剩余的 raw forbidden edge
是 canonical package-executable entrypoint：
`neuro_code.__main__ -> neuro_code.bootstrap.entrypoints`；它不属于待清除的兼容债务。

`bootstrap.composition` 中的 `ApplicationComposition` 会解析配置与供应商 override、执行
会话沙箱预检、初始化 SQLite、创建供应商/工具/权限管理器和会话作用域后台任务注册表，并持有
监督器关闭责任。阶段 2A 只改变其结构归属，初始化和失败清理顺序保持不变。CLI、TUI 和 ACP
继续共享同一服务和带类型运行时事件流。

完整依赖规则、兼容迁移策略和 allowlist 纪律见
[ADR 0049](adr/0049-progressive-architecture-boundaries.md)。

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

`AgentConversation` 是位于单轮 `AgentRuntime` 之上的可复用应用边界。其 canonical 实现位于
`neuro_code.application.sessions.conversation`；原
`neuro_code.application.runtime.conversation` 路径只保留单向兼容 facade。它串行执行轮次，
并在每次持久提交后继续携带有序会话项、会话 ID 和供应商来源元数据。打开已有会话时，
它会校验记录的工作区与请求工作区是否指向同一文件系统位置。无头 CLI 和 Textual 界面
组合相同的控制器，因此恢复和供应商回放规则不会因界面不同而分叉。

发生失败或取消时，`AgentConversation` 会在释放轮次锁之前，从 `SessionStore` 重新加载
规范有序项和供应商来源。所以下一条提示会复用持久状态，而不是过期的内存前缀。TUI 会为
提示请求显式的首 token 前回退策略：在任何非空模型输出、完成事件或工具活动之前取消时，
运行时保存本轮前的项前缀，并在 `TURN_FAILED` 中报告回退；追加式审计事件仍记录用户曾提交
该提示。产生输出或工具活动后，用户消息仍保留。TUI 会把安全回退的提示恢复到草稿，并可在
首个非空模型 token 前缓冲最多四条显式后续提示；该队列只是界面状态，不属于持久 Runtime
上下文。

## 仓库级 AGENTS.md 指令发现

纯指令值对象的 canonical owner 是
`neuro_code.domain.workspace.instructions`。旧的
`neuro_code.domain.instructions` facade 已移除；文件系统发现适配器仍位于
`neuro_code.infrastructure.workspace.instructions`。这样可以把领域投影值与文件系统副作用分开，
同时不改变发现端口和既有安全限制。

仓库级 AGENTS.md 文件是项目自有的非系统指令，在工作区边界内指导代理行为。它们
不会从网络加载，不会被执行，也不会被允许冒充 system 或 user 消息。所有发现过程都是
确定性的、有界的、失败关闭的，并且只在工作区根目录向目标目录的方向上单向展开。

发现服务由 `InstructionDiscovery` 端口定义，默认适配器是
`FilesystemInstructionDiscovery`。应用组合根通过 `InstructionDiscoveryFactory`
构造适配器，并为每个会话绑定安装独立的 `instruction_provider` 闭包。文件工具会移动
绑定内的目标；该闭包在每次模型步骤前从工作区根重新发现到该目标。有界文件系统工作
在线程中执行，不阻塞事件循环；同会话内的 AGENTS.md 变更会在下一步生效。发现结果
不会缓存到跨会话状态，也不会进入持久化
`SessionItem` 历史；注入的指令消息是临时的、每步重算的合成项。

`InstructionTracker` 会另行记录最近一次真正注入模型步骤的结果。在
`search_replace` 写入前，它按路径和内容对比目标目录的当前指令与该快照；新增或
变更的指令会让写入以错误中止，使下一模型步骤先看到新规则再重试。任意 Bash
命令涉及的路径无法可靠推断，因此 Bash 写入仍保留这一已记录限制。

发现的指令不会追加到系统消息，而是作为独立的合成 `User` 消息注入到系统消息之后、
真实用户消息之前，并标记为 `SyntheticReason.PROJECT_INSTRUCTIONS`。这个结构化来源
标记保证仓库内容不会共享应用系统提示的信任级别。`Message.synthetic_reason` 仅存在
于内存中；合成项每步重建，不会进入存储、UI 或协议会话历史。

发现过程从根到目标逐层收集 AGENTS.md，并按浅到深返回。文件系统工作上限为 20 层
目录、10 个已加载文件、单文件 64 KiB、总计 256 KiB；同时校验 UTF-8、C0/C1/DEL
控制字符、常规文件身份和工作区边界。所有符号链接与 Windows reparse point 都会被
拒绝；审计输出区分逃逸、循环/损坏和仍位于边界内的链接。拒绝路径中的控制字符会先
转义，再进入终端或 JSON 输出。

文件读取使用有界、尽力抗符号链接的方式：`lstat()` 拒绝符号链接和 Windows
reparse point；`os.open()` 配合 `O_NOFOLLOW`（POSIX）打开句柄；句柄级 `fstat()`
校验为常规文件并与 `lstat` 的 `st_dev`/`st_ino` 比较，检测 lstat→open 之间的路径
替换；`os.read()` 最多读取 `MAX_SINGLE_FILE_BYTES + 1` 字节（+1 检测超限）；读取后
再次 `fstat()` 验证句柄身份未变。这不是完全 TOCTOU 安全的实现——POSIX 的
`O_NOFOLLOW` 只保护最后一个路径组件，Windows 无 `O_NOFOLLOW`——但 lstat 拒绝、
有界读取和 lstat↔fstat 身份比较的组合为常见攻击向量提供了强防御。目标目录逃逸
工作区会被整条拒绝为 `ESCAPES_WORKSPACE`，而不是被静默 clamp 到根目录。所有拒绝
原因都是枚举值，便于 inspect 与审计。

CLI 的 `inspect` 命令在同一发现服务上输出已加载文件路径、深度、内容字节数、指纹
和所有拒绝项。JSON 模式包含 `instructions` 字段；纯文本模式按行渲染。ACP 与 TUI
通过 `ApplicationComposition` 注入 `InstructionDiscovery` 实例；CLI inspect 通过
`ApplicationComposition.default_instruction_discovery()` 使用相同的默认工厂构造
实例。它们共享相同的端口契约和默认工厂，因此发现规则不会因界面不同而分叉，但
inspect 不使用与活动会话相同的运行时实例。详见
[ADR 0039](adr/0039-repository-instruction-discovery.md)。

## 只读技能文件发现

纯技能元数据的 canonical owner 是
`neuro_code.domain.workspace.skills`。旧的
`neuro_code.domain.skills` facade 已移除；文件系统发现仍位于
`neuro_code.infrastructure.workspace.skills`，`SkillTool` 仍是基础设施层的只读正文读取工具。
这样可以让解析、有界元数据投影、替换、fingerprint 和合成消息构造与文件系统副作用分离。

只读技能文件发现遵循与指令发现相同的端口与适配器架构模式。技能文件
（`SKILL.md`）是仓库提供的最佳实践参考文档，描述如何处理特定任务。与
AGENTS.md 指令文件一样，它们不会从网络加载，不会被执行，也不会被允许冒充
system 或 user 消息。所有发现过程都是确定性的、有界的、失败关闭的。

发现服务由 `SkillDiscovery` 端口定义，默认适配器是
`FilesystemSkillDiscovery`。适配器扫描配置目录
（`.neuro/skills/`、`.agents/skills/`、`.claude/skills/`），
递归遍历每个 `skills/` 目录树（最大深度 5）查找 `SKILL.md` 文件。配置目录
按产品特定优先级扫描（`.neuro` 先于 `.agents` 先于 `.claude`），
子目录按字典序遍历。技能按名称去重（先见为准，按各范围内的配置目录优先级），
再按范围优先级（Local > Repo > User）和名称排序。

技能发现适配器从指令发现适配器导入文件系统安全辅助函数
（`_toctou_safe_read`、`_is_symlink_or_reparse_point`、
`_resolve_within_workspace`、`_relative_posix`），复用相同的有界、尽力抗符号
链接读取模式。符号链接全部拒绝，但按逃逸、循环和安全分类给出不同拒绝原因。
所有拒绝原因都是 `SkillRejectionReason` 枚举值，便于 inspect 与审计。

每个 `SKILL.md` 的 frontmatter 由有界、无依赖的行解析器处理，支持常见的
`key: value` 标量、引号值和行内注释；分隔符必须独占整行。缺失或格式错误的
元数据会回退到技能目录名和正文首个散文行。

模型收到的是受字节上限约束的精简技能目录（name + description + when-to-use），不是完整
的技能正文。技能列表作为独立的合成 `User` 消息注入，标记为
`SyntheticReason.AVAILABLE_SKILLS`，插入在指令消息之后（若无指令则插入在
系统消息之后）。这使模型在看到仓库项目约定后、真实用户消息前看到可用技能。
与指令注入相同，该合成消息不进入 `SessionItem` 持久化，是临时的、每步重算
的合成项。`Message.synthetic_reason` 是仅模型上下文中的内部标记，从不写入
持久化会话。

当前已实现 `LOCAL` 范围（从移动目标向上到工作区根）、`REPO` 范围（工作区之上
到 git 根的每个祖先）和 `USER` 范围（用户主目录）。服务器同步和插件技能保留给
未来的切片。动态会话中发现已实现——追踪器维护移动目标，每次调用时从目标向上
遍历到工作区根（含）重新扫描。

应用组合根通过 `SkillDiscoveryFactory` 构造适配器，并为每个会话绑定安装
独立的 `skill_provider` 闭包。该闭包在每次模型步骤前由
`AgentRuntime._refresh_skills()` 调用，使同会话内的技能文件变更能在下一步
生效。CLI inspect 通过 `ApplicationComposition.default_skill_discovery()`
使用相同的默认工厂构造实例，与指令发现共享相同的模式：inspect 与会话
使用不同的运行时实例，但共享端口契约和默认工厂。详见
[ADR 0040](adr/0040-read-only-skill-discovery.md)。

## 技能正文加载工具

`SkillTool`（`tools/skills.py`）允许模型按名称加载已发现技能的完整正文。
模型首先通过 `AVAILABLE_SKILLS` 合成消息看到精简的技能目录；当它决定某个
技能与当前任务相关时，调用 `skill` 工具并传入技能名称来加载完整的 SKILL.md
正文。

该工具遵循与发现相同的有界、抗符号链接读取模式：解析
`skill.root / skill.relative_path`，按 LOCAL、REPO 或 USER 的相应根校验边界，
并确认加载内容仍与发现指纹一致。工具剥离 BOM 和 YAML frontmatter，返回有界的
`<skill_content>` 块。捆绑文件样本最多包含 10 个直属常规文件名；链接、目录、含
控制字符的名称和条目过多的目录会被省略。

`ToolContext` dataclass 依赖 `SkillContextTracker` 端口，不把具体运行时追踪器
导入端口层；该端口由
`ApplicationComposition.create_binding()` 接线。`SkillTracker` 在每次
`current_result()` 调用时重新发现，因此技能文件变更在下次工具调用时生效，
无需重启会话。变量替换在加载时执行：`SkillTool` 接受可选的 `args` 参数，
通过 `domain/workspace/skills.py` 中的 `apply_skill_substitutions()` 展开正文中的
`$ARGUMENTS`、`$ARGUMENTS[N]`、`$N` 和 `${SKILL_DIR}` 令牌。当正文不包含
参数令牌但 args 非空时，args 作为 `**ARGUMENTS:**` 后缀追加以保持向后兼容。
参数字节数、替换次数和渲染输出都有上限；不支持的位置令牌（如 `$100`）保持原样。
详见 [ADR 0041](adr/0041-skill-body-loading-tool.md) 和
[ADR 0045](adr/0045-skill-variable-substitution.md)。

## 用户级技能发现

`FilesystemSkillDiscovery` 接受可选的 `user_home: Path | None` 构造参数。
当为 `None` 时，适配器在发现时通过 `Path.home()` 解析用户主目录。LOCAL 发现以
工作区为公共边界，REPO 发现以检测到的 git 根为公共边界，USER 发现以解析后的
用户主目录为边界。当工作区根与用户主目录是同一路径时（例如会话从主目录
启动），跳过 USER 遍历以避免重复扫描。候选元组同时携带发现根和范围，使
处理循环能为每个候选项针对正确的根计算 POSIX 相对路径并执行边界检查。

`SkillInfo` 新增 `root: Path | None` 字段（默认为 `None` 以保持向后兼容），
存储发现该技能时所用的发现根。`SkillTool` 通过
`skill.root / skill.relative_path` 解析绝对路径（当 `root` 为 `None` 时回退
到 `tracker.workspace_root`），并针对发现根而非工作区根执行边界检查。这使
LOCAL 技能（根 = 工作区）和 USER 技能（根 = 用户主目录）的路径解析都正确，
而无需更改工具的公共契约。

跨范围优先级以范围为先：LOCAL 候选先于 REPO 候选收集和处理，REPO 候选先于
USER 候选。按名称先见为准的去重确保 LOCAL 技能遮蔽同名 REPO 技能遮蔽同名 USER
技能。在每个范围内，配置目录优先级（`.neuro` → `.agents` → `.claude`）仍然适用。详见
[ADR 0042](adr/0042-user-level-skill-discovery.md) 和
[ADR 0044](adr/0044-repository-level-skill-discovery.md)。

## 动态会话中技能发现

`SkillTracker` 维护一个移动目标，镜像 `InstructionTracker` 设计。当文件
访问工具（`read_file`、`read_files`、`list_dir`、`list_tree`、`grep`、
`grep_many`）触碰某路径时，`check_path()`
更新目标，使从被访问目录**向上**到工作区根（含）的 `SKILL.md` 文件在下一次
`current_result()` 调用时被发现。这能发现工作区内任何嵌套深度的技能，不仅
限于工作区根——例如，当模型读取 `src/foo/` 中的文件时，
`src/foo/.neuro/skills/commit/SKILL.md` 会被发现。

适配器从 `target` **向上**遍历到 `workspace_root`（含），检查每个祖先目录
的配置目录。更深的祖先先被扫描，因此先见为准的名称去重使更具体（更深）的
技能优先于一般（根）的技能。当 `target`
为 `None` 或等于工作区根时（如 CLI inspect、`rediscover_skills`），遍历退化
为仅扫描根层级。

子树隔离适用：从 `src/foo/` 切换到 `src/bar/` 会移动目标，`src/foo/` 配置
目录中的技能不再被包含。单个及有界批量读取、列举和搜索工具都会在已有的
`InstructionTracker.check_path()` 调用旁调用 `SkillTracker.check_path()`。
`SearchReplaceTool` 不移动技能目标（其指令追踪器另有写入预检），
`BashTool` 则不尝试从任意 shell 语法推导路径。详见
[ADR 0043](adr/0043-dynamic-session-skill-discovery.md)。

## 仓库级技能发现

当工作区是 git 仓库的子目录（如 `myrepo/packages/frontend/`）时，适配器从
工作区根向上查找常规且非链接的 ``.git`` 目录或文件来检测 git 根。随后按由近到远
的顺序扫描工作区之上的每个祖先，直至并包含 git 根，并把技能标记为
`SkillScope.REPO`。因此包级仓库技能可以遮蔽 git 根默认技能，两者对嵌套工作区
仍然可见。

当 git 根等于工作区根（已被 LOCAL 发现覆盖）或在有界向上遍历中未找到可接受的
``.git`` 标记时，跳过 REPO 扫描。
`FilesystemSkillDiscovery.__init__` 接受可选的 `git_root` 参数（默认为
`None` 以自动检测），遵循与 `user_home` 相同的模式。所有 REPO 技能的
`SkillInfo.root` 都设置为公共 git 根，使中间祖先路径保持唯一，并让
`SkillTool` 始终针对同一稳定边界解析。详见
[ADR 0044](adr/0044-repository-level-skill-discovery.md)。

## 规范的结构化文件系统目标

结构化本地文件系统工具对每次调用使用一个不可变的
`FilesystemAccessPlan`。工具适配器先从经过校验的工具语法中提取全部目标，随后
`resolve_filesystem_access_targets()` 在权限评估前一次性规范化每个本地路径。每个目标
记录规范路径、所属主工作区或附加工作区根、策略路径、操作、存在状态和链接样组件证明；
原始写法只用于诊断。

权限链严格按以下顺序执行：

1. 一次解析全部目标，包括 `apply_patch` 的所有源路径和目标路径。对缺失的创建叶子会证明
   已存在的父级/祖先，并拒绝符号链接、junction、Windows reparse traversal、父级逃逸以及
   含义不明确的 Windows device/extended/ADS 命名空间。
2. `PermissionManager.decide_targets()` 对每个规范目标独立评估。显式 deny 优先；无头模式中
   未解决的 ask 会拒绝；路径范围 allow 是 allowlist。只有所有目标都获授权时，结构化调用才会通过。
3. 工具执行接收同一个不可变计划，并按提取索引消费规范目标，不会再次解析原始路径。因此混合
   允许/拒绝目标的 `apply_patch` 会在 journal 或任何写入前停止。

该契约覆盖本地结构化工具：`read_file`、`read_files`、目录列举、glob/search、
`search_replace` 和 `apply_patch`。工作区身份、permission、sandbox 和 execution 仍是分离
决策；该计划不会把任意 Bash 路径解释、MCP 调用、ACP 委托执行或不透明 artifact 句柄变成
结构化文件系统目标。ACP 客户端路径属于独立的 client authority：Neuro Code 只做 session 根
的词法校验，不会对远程路径调用宿主 `Path.resolve()`、存在性或链接检查。该契约收口的是
本地结构化工具边界上的 raw-path authority gap，不声称为所有进程或 Provider capability 提供
无竞态的 TOCTOU 保护。

## Partial ACP v1 适配器

`neuro-code acp` 是位于 `ApplicationComposition` 与官方
`agent-client-protocol` Python SDK 之上的协议适配器。生产 framing、JSON-RPC
路由、换行分隔 stdio、`session/update` notification 和
`session/request_permission` request 均继续由 SDK 持有。适配器声明
`loadSession: true` 与 list/delete/fork/resume/close session capability，实现
`initialize`、`session/new`、`session/list`、`session/load`、`session/delete`、
`session/fork`、`session/resume`、`session/prompt`、`session/cancel` notification
和 `session/close`。SDK 0.11 把 fork、resume 和 close 置于
`use_unstable_protocol` 门后；其生成 Schema 已包含稳定 delete 模型，但 Agent router
漏掉了该路由，因此 Neuro Code 只把生成的 delete request 加到官方 `MessageRouter`，
SDK stream、`Connection`、dispatcher、Schema、framing 与错误规范化保持不变。

每条 ACP 连接固定绑定到规范化后的启动工作区。每个成功 session 拥有稳定随机 ACP ID、
一个 `AgentConversation`、一个后台任务 scope、一个活动 prompt 槽位，以及独立审批/
取消/关闭状态。内部 SQLite ID 与 ACP ID 保持分离，并在首次 prompt 时按需记录；
SQLite schema v5 会持久化唯一且带 namespace 的 alias，使后续进程可加载同一个 ACP
ID。所有资源就绪前不会发布 session。load 会预留请求的 ACP ID，重新校验工作区、固定
sandbox 与 provider 亲和，重建 conversation 和后台 scope，回放历史，然后才发布
session。resume 执行相同检查与重建，但不回放历史。fork 把持久有序上下文与
provider/sandbox 亲和复制到新的内部和外部 ID，再创建不回放历史的独立 session；来源
prompt 必须空闲，发布失败会删除复制行。delete 先关闭活动资源，再删除工作区内持久
session，其 event、alias 和搜索行通过级联清理。close 会先应用 cancel 语义，等待必须的
工具终态更新和 prompt 响应，关闭
scope，释放运行时绑定，同时保留持久历史与 alias。EOF 或连接故障会对全部活动中或
创建中的 session 执行相同的幂等清理。
详见 [ADR 0050](adr/0050-acp-session-lifecycle.md)。

`additionalDirectories` 可为某次新建、加载、恢复或分叉 ACP binding 声明至多四个已存在、
绝对且互不重叠的目录根。它们会在连接工作区通过验证后再验证，并不会随持久 session
保存；以后重新建立 binding 时，客户端必须重新声明。文件工具只能接受主工作区或这些显式
根内的路径；指令和技能发现仍严格限定在主工作区。变更报告会分别快照每个已声明根，并对
额外根使用绝对路径标注。`off` session 继续走原有权限流程。所有已启用的沙箱都会拒绝非空
附加目录，因为它们的挂载命名空间在 ACP 请求前已经固定；这也避免将平台中可写的临时或
状态挂载误当作事后声明的目录根。这样不会通过事后挂载主机目录来削弱显式沙箱边界。

当 ACP 客户端明确声明 `fs.readTextFile` 时，session 会获得一个绑定到该 ACP session 的窄
`ClientFileSystem` 应用端口。既有 `read_file` 和 `read_files` 工具仍会先通过选定的主工作区或
额外工作区根做词法 session 根校验，再将客户端拥有的路径及有界行范围委托给
`fs/read_text_file`；宿主机不会解析或检查该路径，也绝不会回退调用本地操作。ACP 文件系统能力不提供目录遍历或搜索操作，因此 `list_tree` 和
`grep_many` 与 `list_dir`、`grep` 一样继续使用本地工作区语义。当客户端同时声明文本读写能力时，
`search_replace` 会使用同一端口读取、保留现有
精确匹配/歧义检查和指令预检规则，再通过 `fs/write_text_file` 写回结果。只读客户端不会暴露该
工具。客户端响应与写入均限制为 1 MiB；客户端失败会成为不含原始详情的稳定失败关闭工具错误，
最终写入的文件系统语义仍由客户端负责。

当 ACP 客户端明确声明 `terminal: true` 时，`off` profile 的 binding 还会获得绑定到 session 的
`ClientTerminal` 端口。独立的 `terminal_exec` 工具接收一个可执行文件与有界参数向量；它不会把
既有本地 `bash` 工具重新解释为远程 Shell。每次调用都会创建、等待、读取并释放一个客户端终端，
输出限制为 1 MiB，超时或取消时请求 kill，也不会转发任何已配置的 Neuro Code 环境值。同一按 session
绑定的端口也暴露标准 ACP 后台直接可执行文件工具：`terminal_start`、`terminal_output`、
`terminal_wait` 与 `terminal_kill`。它们使用不透明 task ID，最多允许八个运行中任务和 32 个保留任务，
并会在超时或 session 清理时 kill/release 工作。普通有副作用权限仍会门禁启动和终止操作。所有启用的
sandbox 都不会暴露这些工具，直接调用也会失败关闭，因此客户端终端不能弱化显式本地沙箱。交互式
输入/resize、游标流式读取和 PTY framing/背压仍未支持。

非空 `mcpServers` 接受 ACP stdio、Streamable HTTP（`http`）和 legacy SSE（`sse`）
结构；ACP 传输 server 会被确定性拒绝。每个 server 都必须在 session 发布前完成初始化，
并通过有界分页枚举和校验工具目录；server 重名、非法工具名、远端工具之间或与内建工具
冲突、覆盖受保护环境变量、不安全的 URL/header 输入或配置超限都会使整个 session 创建
失败。远程 URL 必须是绝对 HTTP/HTTPS endpoint，不能包含内嵌凭据或 fragment。header
名称、数量、值与总字节均有上限，且不能覆盖 framing 或 routing header。加载持久 ACP
session 时可以重新提供相同的临时 MCP 配置，但它不会作为历史或授权被持久化。

官方 `mcp>=1.28.1,<2` SDK 持有 MCP Schema、`ClientSession`、版本协商、JSON-RPC 调度
和工具结果类型。stdio 使用项目自有的换行分隔 `ProcessTree` 桥接：官方 SDK 在 Windows
上采用 spawn 后再附加 Job 的方式，无法满足 Neuro Code 创建时原子加入 Job 列表的要求。
Streamable HTTP 与 SSE 使用 SDK client，并由应用 HTTP client 禁用环境代理和重定向、
保持 TLS 校验且将每个响应体限制为 1 MiB。frame、Schema、工具数、JSON 深度/节点、参数、
输出和超时均有上限；MCP stderr 会被排空但不会进入 ACP stdout；`_meta`、图片/音频/
embedded body 与无界 raw 值不会被投影。ResourceLink 结果只保留引用元数据，不会被
解引用。显式 server 环境变量/header 值与应用凭据会从模型可见文本中脱敏。

MCP annotations 只是不可被信任的提示，因此所有投影后的 MCP 工具均标记为有副作用。
`ApplicationComposition` 会在 bypass/always-approve 行为之上安装精确 ASK 规则，同时
保留本地显式 DENY 的优先级。普通运行时因此先发送 pending、请求 ACP 审批，随后才发送
in-progress 并调用 server；拒绝审批绝不会执行。stdio prompt 取消会在工具失败 update 与
`cancelled` prompt 响应完成前终止整个受控 server 进程树。远程 server 的取消会关闭 SDK
连接并让其无法再被调用；项目不声称持有远程进程，因此不会把终态不确定的远程副作用表示成
已成功取消。close、load 失败、创建失败、EOF 与断连都幂等关闭同一 session-owned
collection。MCP resources、prompts、sampling、elicitation、动态工具目录刷新与 ACP
传输仍未支持。

list 只用于发现；即使省略 `cwd`，也始终限制在连接工作区。它只返回持久 ACP ID、记录的
绝对 cwd、有界标题和 ISO 更新时间。尚无 alias 的 session 通过 schema v5 原子
get-or-create 获得一个。SQLite keyset page 经过文件系统身份工作区比较过滤；每个 request
最多返回 50 个匹配并扫描 5,000 行。随机连接局部 cursor token 只在内存中保存 keyset
位置，最多 256 个，不暴露内部 ID。list 不会打开 conversation/background scope，也不
返回内容、provider 元数据、`_meta` 或额外目录。

提示转换按照输入顺序接受 ACP 基线 Text、内嵌 Image、ResourceLink 与内嵌
`TextResourceContents`。Text/resource 数量、单字段大小、annotations 序列化、ResourceLink
汇总字节和文本总字节都有上限。Image 只接受固定光栅 MIME 白名单中通过校验的 base64：最多
八张、解码后单张 5 MiB、总计 10 MiB。其可选 URI、本地文件与远程链接绝不会被读取、下载或
解引用。内嵌文本资源只接受已提供的文本：最多八个、单个 64 KiB、合计 128 KiB。它会成为带
有界 URI 和可选 MIME 类型来源标签的文本 `ContentPart`；URI 绝不会被解析，block、resource
及 annotation 的 `_meta` 都会被省略。规范的有序 `ContentPart` 会随用户消息持久化，因此
供应商适配器可以在当前轮和恢复会话中应用自己的角色、MIME 和请求大小校验。只有 `uri`、
`name`、`title`、`description`、`mimeType`、`size` 和标准 annotations 字段会进入模型可见的
ResourceLink 描述；`_meta` 会被忽略。音频和内嵌 `BlobResourceContents` 提示块仍会被拒绝。

load 历史使用另一组显式投影。可见用户与助手文本使用新的 UUID message ID 映射为标准
message chunk。有序图片 part 会变成现有的安全图片占位符，绝不会变成 raw data URI、图片
字节载荷或远程 URL。内嵌文本资源会保留为有界、带标签的用户文本。工具调用只暴露有界/脱敏
的名称、类型、白名单路径和结果内容，并确保 pending 到终态 update 配对。system message、
reasoning、供应商保留上下文、任意参数、
`_meta` 和 raw input/output 全部省略。发送第一条 update 前先校验完整回放，并限制持久项数、
update 数、单字段和序列化总字节。

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
优先级，pending 工具更新先于客户端请求，批准返回前不能开始执行。协商到的客户端文件系统和
终端能力只会经绑定到 session 的应用端口调用；应用代码不会接触 ACP SDK 类型。详见
[ADR 0035](adr/0035-partial-acp-v1-stdio.md)与
[ADR 0036](adr/0036-durable-acp-session-load.md)及
[ADR 0037](adr/0037-workspace-scoped-acp-session-list.md)，以及
[ADR 0038](adr/0038-session-owned-stdio-mcp-tools.md)、
[ADR 0052](adr/0052-capability-gated-acp-client-filesystem.md)与
[ADR 0053](adr/0053-capability-gated-acp-client-terminal.md)，以及
[ADR 0054](adr/0054-bounded-acp-inline-image-prompts.md)及
[ADR 0055](adr/0055-bounded-acp-embedded-text-resources.md)，以及
[ADR 0056](adr/0056-bounded-acp-client-background-terminals.md)。

最小 TUI 是 `AgentEvent` 之上的表现适配器，负责提示输入、滚动记录、实时文本表面和
本地斜杠命令。它绝不渲染原始推理或不受限制的参数/结果映射；只有路径、命令、模式、
查询与任务 ID 等有界白名单参数可进入调用摘要。每个本地工具调用仍保留按 call ID 标识的
稳定状态，但 TUI 会把连续调用投影为一个活动组。活动组默认折叠，编辑也不例外；摘要只
保留状态、有界意图或聚合数量、关键失败信息与耗时。按 Enter 或单击会打开只显示一个所选
调用的固定高度 Inline Peek；上/下方向键选择其他调用，Enter 打开独立 Tool Inspector，Esc
返回稳定摘要。再次单击已打开的 Peek 会收起；应用级 fallback 会在焦点移动后继续保证 Esc
能够收起。Inspector 打开期间，实时生命周期事件会更新所选 presentation，并通过持久基础
screen 定位 Conversation widget，而不是在当前 Modal 内查找；运行计时器每次 tick 对每个
活动组最多刷新一次，并跳过已打开的 Peek/Inspector 布局。Peek 的十个逻辑行 presenter
预算由十二行 widget 最大高度兜底，因此终端换行不能让 Conversation 无限增高。长 Bash 意图
会截断，正常 allow 判定不会进入 Summary/Peek，完成状态只由勾号表达一次。详见
[ADR 0014](adr/0014-minimal-event-stream-tui.md) 与
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md)，以及表现层细化
[ADR 0108](adr/0108-editorial-tui-presentation.md)。

滚动记录由稳定消息组件组成的纵向对话实现，而不是“预渲染日志 + 临时流式区域”。用户
提示和助手回答使用不同布局；待完成的助手组件始终位于对话末尾，生命周期通知插入其前，
文本增量和最终回答都更新同一个组件。只有视口本来就在末尾时才自动跟随。详见
[ADR 0026](adr/0026-stable-localized-tui-conversation.md)。

助手组件使用 Rich 的 Markdown 文档模型和应用自有语义主题，同时禁用链接点击；模型
输出绝不会进入 Rich/Textual markup 解析。用户内容以及应用或外部值使用字面 `Text`。
对话消息与本地系统、状态、活动、计划和错误记录共享同一左侧阅读轴，并限制为最多 116 列；
标签使用行内形式，不再永久占据固定宽度栏。颜色由信息层级而非对象类型本身决定，只保留克制
交互 accent 与 success/warning/error 语义色。Tool Activity 的 tree、grep、file-read、Bash 与
generic renderer 优先投影 metadata；格式化 stdout 只作为有界 fallback。Conversation 永不
渲染 artifact 或完整工具输出。独立 Inspector 提供可滚动、可复制的 Output/Input/Meta，递归脱敏
Input、白名单化 Meta，并且只在此时通过既有 256 KiB、已脱敏且属于当前会话的应用边界懒加载输出
artifact；读取或存储截断会明确显示。Transcript Copy 始终投影稳定的 Activity Summary。详见
[ADR 0030](adr/0030-bounded-interactive-tool-card-details.md) 与
[ADR 0067](adr/0067-tui-bounded-tool-output-details.md)。Mermaid 与媒体仍在该渲染器边界之外。
另见 [ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md)。

应用自有 TUI 文案通过 `UiLanguage` 选择。注入的 `UiPreferencesStore` 端口持久化界面
语言、请求的思考强度和交互模式；JSON 适配器使用与供应商配置分离、原子写入且仅用户可访问的状态
文件。值缺失或无效时分别回退到英语、`high` 和 `normal`。英语和简体中文目录必须具有相同键集合。
切换语言会重新渲染界面外壳和可翻译的本地历史，但可见的用户/模型文本以及已经清理的
工具预览不会翻译，也绝不会送入翻译器。

表现适配器持有一套紧凑的中性深色语义主题，不暴露 Textual 无关的主题与命令面板表面。
三层背景、一种边框、三档前景、一种克制交互 accent、语义结果色与共享间距共同定义层级。
内建命令面板会被禁用，供应商与会话发现通过明确的应用命令完成，会话查询按字面纯文本
渲染。提示框下方的一行无冗余字段名运行投影，在有界左侧区域显示模型、强度与模式，在有界
右侧区域显示上下文占用和压缩工作路径；窄终端会省略过长模型与路径。它由控制器状态在本地化、
供应商故障转移或用户选择变化时主动刷新，不会从对话文本反向解析状态。永久快捷键栏被移除；
`/help` 和 F1 按需显示现有命令参考。纯折叠脉冲状态机由 Textual 定时器推进，并且只在等待模型输出时渲染到待
完成助手文案前。上下文用量先对规范会话项进行供应商中立的本地估算；模型完成事件带有
token 元数据时，运行时发出 `CONTEXT_USAGE_UPDATED`，并用供应商报告的输入加输出用量
替换估算值。分母来自显式 profile 元数据 `context_window_tokens`；字段缺失时只显示已知
token 用量而不编造百分比。受管供应商元数据也按 profile 提供同一个正整数能力字段。

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
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md)。Neuro Code 通过自身的
原生终端测试验证此进程边界。

原生适配器之上，应用会话 owner
`neuro_code.application.sessions.terminal_sessions` 实现共享的
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
工作区变更和终态仍保留有界的 call-ID 状态，并由 TUI 投影到连续活动组中。对于带副作用的本地工具，
运行时只在权限通过后、紧邻执行前后比较有界的只读工作区快照；这份报告只是审计元数据，
既不授予权限，也不代表执行成功。`WorkspaceChangeObserver` 是由 bootstrap 为每个 binding
创建的应用组合依赖；`AgentRuntime` 的构造不承诺为稳定的外部 Python API。详见
[ADR 0028](adr/0028-timed-tool-feedback-and-interaction-modes.md) 与
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md)。

对于当前会话作用域，本地 `/tasks` 会同时渲染有界的存活后台任务元数据和持久计划执行任务
记录，两者都不显示命令文本或输出。周期只读轮询仍只会为每个后台任务终态转换发出一次通知。
`/tasks` 不能修改任何一种任务状态；`kill_task` 仍走普通模型工具与权限路径。`/view-task TASK_ID`
则是用户主动发起的当前会话持久任务精确读取：对于带快照的计划执行记录，它会仅供参考地渲染完整已保存
计划，不会启动轮次或改变任务状态。详见
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md) 与
[ADR 0058](adr/0058-durable-session-task-lifecycle.md) 与
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md)。

TUI 在 Worker 管理的轮次运行时保持提示框可用。`Ctrl+C` 与本地 `/cancel` 会取消该
Worker；审批模态框则把 `Ctrl+C` 限定为拒绝待处理请求。运行时拥有的恢复与工具结果
配对规则见 [ADR 0016](adr/0016-recoverable-turn-cancellation.md)。

`ProfileConversationController` 还持有 `InteractionMode`，让模式切换与活动轮次串行，并
把选择重新应用到替换后的绑定。`normal`、`accept-edits` 与 `plan` 映射为确定性的权限管理
模式；安全分类器实现前，`auto` 默认采用安全的 `accept-edits` 预览，只有显式授权的
`--always-approve` 启动会保留绕过默认值。提示词指引只描述模式，真正权限只来自权限、
工作区和沙箱适配器。

`SessionPlan` 是活跃对话拥有的有界领域值，而不是供应商或 UI 所有的状态。普通、无副作用的
`update_plan` 工具会校验完整替换值。`AgentRuntime` 通过 `SessionStore` 保存已接受的计划，
发出 `PLAN_UPDATED`，并把供应商中立的渲染内容加入后续模型请求。`AgentConversation.open`
会在恢复轮次前载入它，分叉也会复制已保存的值。Textual 界面只读取这一状态：
`/plan DESCRIPTION` 会先安全切换到计划模式再提交说明，`/view-plan`/`/show-plan` 则本地化地
显示已保存状态。用户显式调用 `/execute-plan`/`/run-plan` 后，界面只切换到 `accept-edits`，
再请求应用执行已保存的计划。运行时会创建一条仅含元数据、不透明的 `SessionTask`，并在规范用户消息
之前持久化 `PLAN_EXECUTION_REQUESTED`。它会在对应轮次终态事件之前恰好一次转换为 completed、failed
或 cancelled。该记录可持久化查看，但不会随着会话分叉复制，也不会调度或唤醒后续工作。因此交接
可恢复、可审计，却不授予命令、网络、工作区或沙箱权限。`/tasks` 会保持持久记录摘要有界。只有显式的
`/view-task TASK_ID` 才会调用活动对话精确、限当前会话的 `SessionStore.get_session_task` 读取，并把
保存的不可变快照作为参考渲染。该读取既不会进入模型上下文，也不会更改当前计划、创建轮次、执行工作、
请求审批或具有调度语义；任务不存在或旧记录没有快照时不会显示细节。显式 `/schedule-plan`/`/queue-plan`
命令会在不联系模型的情况下为每个会话保存最多四个排队计划快照。`/run-task TASK_ID` 通过
`SessionStore.start_session_task` 原子认领一个排队快照，然后复用 `/execute-plan` 的计划执行生命周期；
排队任务不会自动启动、重试、唤醒或创建子代理。本切片刻意不写计划文件，也不提供子代理生命周期。当前计划评论是刻意独立且有界的反馈通道：`/comment-plan STEP COMMENT` 会把用户
文本保存到编号计划步骤下，`/view-plan` 会渲染它，下一次模型请求才把它作为临时计划指引提供。评论
不是规范消息、批准、任务或执行请求。计划指纹阻止它泄露到替换后的计划；整体替换或清除计划会删除
过期评论。详见 ADR 0028、[ADR 0057](adr/0057-durable-structured-session-plans.md)、
[ADR 0058](adr/0058-durable-session-task-lifecycle.md) 与
[ADR 0059](adr/0059-bounded-current-plan-comments.md)、
[ADR 0060](adr/0060-plan-execution-revision-snapshots.md) 与
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md) 以及
[ADR 0063](adr/0063-bounded-explicit-plan-task-scheduling.md)。

Stage5CQ 新增显式且有界的 `SubagentExecutionService` 应用工作流. 它在调用注入的
`SubagentExecutor` 前创建只含元数据的 `SUBAGENT` 会话任务, 恰好记录一个终态, 并保留
执行器的结果、失败或取消语义. 请求有界, 不包含父消息、工具、凭据或输出. 执行器必须
构建新的子运行时/上下文, 本服务不会复用父会话. 本切片没有队列、重试、自动调度、ACP
方法、CLI 命令或 TUI 命令. 详见
[ADR 0071](adr/0071-explicit-bounded-subagent-lifecycle.md)。

Stage5CR 在该接缝后增加第一版具体隔离只读运行时. `IsolatedSubagentExecutionService` 创建全新的
子会话，在执行前持久化只含元数据的 `SubagentLink`，移除 Provider 内置工具，并将子注册表限制为
`read_file`、`read_files`、`list_dir`、`list_tree`、`grep`、`grep_many` 和 `skill`。子步数与墙钟执行时间有界，取消会关闭子运行时，删除父会话
会递归删除关联子会话. 该切片仍然是显式且同步的：不改变普通 `AgentRuntime` 循环，不复用父上下文，
不提供 CLI/TUI/ACP 入口，也不调度、重试或递归创建子 Agent. 详见
[ADR 0072](adr/0072-isolated-read-only-subagent-runtime.md)。

Stage5CS 在该运行时之上增加 `ReadOnlySubagentApplicationService` 作为窄的调用方边界。它要求存在持久化
父子链接，并将子运行投影为脱敏且按 UTF-8 有界的 `SubagentResultProjection`，只包含生命周期 ID、任务终态、
步数、可选类型化 outcome 和响应文本。消息、事件、工具参数、凭据及原始子上下文不会跨越该边界。投影只在
内存中返回，不会追加到父 transcript，也不会写成第二条结果记录。参见
[ADR 0073](adr/0073-bounded-read-only-subagent-result-projection.md)。

Stage5CT 通过 `SubagentRelationshipQueryService` 增加只读的父子关系查询边界。它把已有的
`SubagentLink`、`SessionTask` 和子会话摘要记录投影为有界的 `SubagentRelationshipProjection`，只包含生命周期
ID、任务状态、供应商/模型标签、时间戳，以及 `resume`、`fork`、`delete` 能力标签。活动中的子任务不暴露任何
生命周期操作标签；终态任务只暴露标签，实际变更和执行仍由既有生命周期服务负责。该查询不会读取消息、事件、
工具输出、提示词、凭据或原始子上下文，不增加 schema，也不创建 CLI、TUI、ACP、调度器、重放或自动恢复路径。
参见 [ADR 0074](adr/0074-read-only-parent-child-subagent-relationship-projection.md)。

Stage5CU 在现有组合根只读子代理应用服务之上增加一个明确的 CLI 入口：
`neuro subagent --parent-session SESSION_ID PROMPT`. 该命令先执行父会话恢复预检，随后使用
固定只读能力集运行一个全新且有界的子会话，并只输出脱敏后的 `SubagentResultProjection`
（普通响应或稳定的 `--json` 字段）. 它不复用父上下文，不调度/重试/递归创建，也不增加
TUI/ACP 入口. 参见 [ADR 0075](adr/0075-explicit-cli-read-only-subagent-entry.md)。

Stage5CV 增加显式的私有 ACP 扩展 `_neuro-code/session/subagent`. 它只接受外部会话 ID、有界提示词
和有界步数，通过现有 ACP alias 边界解析父会话，并调用与 CLI 相同的组合根只读应用服务. 响应省略内部
ID 和子 transcript 细节，只返回有界响应/状态/步数/截断标记与类型化 outcome 字段. 它不是标准 ACP
capability，也不增加调度、重试、递归、并行子会话或可写工具. 参见
[ADR 0076](adr/0076-explicit-acp-read-only-subagent-extension.md)。

Stage5CW 增加显式 TUI `/subagent PROMPT` 命令。TUI 与 CLI、ACP 共用组合根提供的
`ReadOnlySubagentApplicationService`；没有当前会话或已有其他回合运行时会拒绝启动，并且只渲染有界响应和步骤/状态元数据。
子代理仍保持只读、隔离、同步且可取消；提示词、事件、内部 ID 和临时上下文不会追加到父 transcript。

详见 [ADR 0077](adr/0077-explicit-tui-read-only-subagent-command.md)。

Stage5CX 增加显式 TUI `/subagents` 只读视图,复用已有的
`SubagentRelationshipQueryService`. 它只显示有界的父任务/子会话标识符、Provider/模型标签、任务状态、时间戳和能力标签;
不会执行恢复、分叉或删除,也不会加载子会话 transcript、提示词、工具参数或输出. 会话缺失、服务不可用或关系为空时会失败关闭,
且不会启动模型回合. 详见 [ADR 0078](adr/0078-explicit-tui-subagent-relationship-view.md)。

Stage5CY 增加 `SubagentRelationshipLifecycleService`,作为显式 `resume`、`fork` 和 `delete`
动作的 application owner。它会在委托现有会话生命周期服务前校验父会话拥有的关系和已终止的
`SUBAGENT` 任务。Resume 只返回经过校验的子会话选择,不会运行模型；fork 返回新会话 ID 但不会
自动打开；delete 只针对子会话。TUI 通过 `/subagents ACTION TASK_ID` 暴露这些动作,不访问 SQLite,
并将校验与变更分开,不声明跨进程原子性。详见
[ADR 0079](adr/0079-explicit-subagent-lifecycle-actions.md)。

Stage5CZ 通过有界无头命令
`neuro subagents ACTION TASK_ID --parent-session SESSION_ID` 暴露同一个生命周期 owner。
CLI 通过组合根的恢复边界校验父会话，然后委托 typed application 请求；不会启动模型回合、重放工具或直接
读取 SQLite。普通输出只包含简短生命周期消息，`--json` 只包含有界生命周期标识、规范动作和可选的 fork 会话 ID。
详见 [ADR 0080](adr/0080-explicit-cli-subagent-lifecycle-actions.md)。

Stage5DA 通过私有 ACP 扩展 `_neuro-code/session/subagents` 暴露同一个 owner。严格请求只包含外部父会话
alias、有界父任务 ID 以及 `resume`、`fork` 或 `delete` 之一。Resume 和 fork 返回外部 ACP alias,而不是内部
会话 ID；delete 只返回有界的 deleted 标志。适配器不会启动模型回合、重放工具或暴露子上下文，也不宣称
alias 分配与生命周期变更属于同一事务。详见
[ADR 0081](adr/0081-explicit-acp-subagent-lifecycle-extension.md)。

Stage5DB 加固该响应边界。ACP 适配器会校验生命周期 owner 返回的父 session、父 task 和 action 与请求一致，serializer
会校验非 delete 外部 alias 的 UTF-8 字节上限和控制字符。错误的 owner 结果或 alias 会失败关闭，但有效 wire 响应保持不变。
详见 [ADR 0082](adr/0082-fail-closed-acp-subagent-lifecycle-projection.md)。

交互组合使用 `neuro_code.application.sessions.profile_conversation` 中的
`ProfileConversationController` 包装当前 `AgentConversation`。旧的
`neuro_code.application.runtime.profile_conversation` 路径只保留兼容 facade。它让 profile
选择与轮次串行执行，并且只向 TUI 暴露脱敏的 `ProviderOption` 数据。选择另一个已配置
profile 时，组合根创建不恢复任何会话的新供应商/运行时/会话绑定，旧 SQLite 会话保持
不变。这条严格边界避免跨供应商回放加密推理、托管工具状态、方言元数据和 profile 亲和
上下文。详见 [ADR 0017](adr/0017-safe-interactive-profile-selection.md)。

该控制器还持有一项进程内 `ReasoningEffort` 选择，并让强度切换与轮次串行。profile 或
会话切换安装新对话绑定时，会把请求等级重新应用到新绑定。`low`、`medium`、`high`、
`xhigh` 与 `max` 对应应用层审查指引；`max` 是普通单智能体的最深策略，在工作流编排实现
前，`ultracode` 的明确实际值是 `max`。
TUI 通过 `Ctrl+E`、`/effort` 和 `/reasoning` 暴露选择，CLI 则使用 `--effort`。选择不会
改写供应商配置，也不成为会话身份。

每次模型步骤开始时，`AgentRuntime` 会把所选指引加入仅用于本次请求的系统消息，并将
有类型的请求值写入 `ModelContext`；该指引不会加入规范 `SessionItem` 历史。供应商
适配器可以读取这个类型值。显式的 Kimi K3 与 GLM 5.3/5.2 dialect 会为 `max` 发送配置的
原生 `max` 字段；其他 dialect 会省略原生强度字段，但仍保留应用层指引。详见
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
- `LocalProcessSandbox`：拥有所有模型可控本地 child 的边界，包括管道命令、stdio MCP
  以及本地 PTY/ConPTY 会话；终端调用方提交 typed `SandboxedProcessRequest`，而不是
  直接调用平台 spawn 适配器。
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
- `SessionStore`：追加带版本事件、保留有序 `SessionItem`，拥有有界的持久会话任务元数据，
  提供规范序列与普通消息投影，并返回带类型、可分页的会话标题/内容搜索页。
- `InstructionDiscovery`：在工作区边界内确定性地、有界地、失败关闭地发现 AGENTS.md
  指令文件，返回有序的 `InstructionFile` 列表、`InstructionRejection` 列表和稳定指纹。
  适配器不得从网络读取、不得执行发现的文件、不得跟随逃逸工作区的符号链接。
- `SkillDiscovery`：在 LOCAL、REPO 和 USER 根下确定性地、有界地、失败关闭地发现
  `SKILL.md` 技能文件，返回有序的 `SkillInfo` 列表、`SkillRejection` 列表和正文敏感的稳定指纹。
  适配器不得从网络读取、不得执行发现的文件，也不得把完整正文放入模型上下文；所有
  链接与 reparse point 都会被拒绝。
- `PlatformAdapter`：封装 PTY、进程、信号、路径、剪贴板和沙箱差异。

外部边界的协议模型必须版本化。内部状态优先使用冻结 dataclass 和枚举。未经校验的
字典不得跨越模块边界，已校验的 JSON 载荷除外。

## 供应商 profile 与兼容网关

组合根选择命名 `ProviderProfile`；代理运行时不会按商业供应商名称分支。profile 将线路
协议（`openai-chat`、`openai-responses`、`anthropic-messages` 或
`gemini-generate-content`）与 xAI Responses 等可选方言行为分离。DeepSeek V4 的 DSML 工具流
通过 `dialect = "deepseek-v4"` 显式选择，绝不会根据供应商名、模型名或主机名推断。通用 Responses 适配器实现位于
`neuro_code.infrastructure.providers.openai_responses.OpenAIResponsesProvider`；xAI 行为通过
`dialect = "xai"` 选择，而不是独立的 Python provider 类。开发阶段的 breaking cleanup 已移除
`neuro_code.providers.xai_responses` 和 `XAIResponsesProvider`；Architecture Freeze v1
随后移除了过时的 `neuro_code.providers` 包及其 Provider 子模块 facade。该导入边界决定记录于
ADR 0072。
凭据只能是环境变量引用或通过校验的回环代理占位符。TUI 还通过 `ProviderSettingsStore` 端口管理用户级
profile；其 JSON 适配器把非密钥元数据和凭据分别原子写入仅所有者可访问的文件。
代理模式及可选环境变量名属于非密钥元数据；解析后的代理 URL 仍只存在于环境/适配器边界。
`ProviderProfile.stored_api_key` 不参与对象表示和脱敏配置检查；运行时还会在工具结果进入
模型上下文、事件或持久化之前，按显式配置值再次清除凭据。当前文件型凭据存储不等同于
加密，后续可替换为平台钥匙串适配器。

受管 profile 在 TOML 之后加载。同名受管 profile 会完整替换供应商表而不是深度合并，
因此项目不能把已保存密钥与工作区控制的端点、代理或工具选项组合。TUI 的“保存并使用”
会以有界应用重启码退出；组合根和全部后台 scope 关闭后，才会重新加载配置并创建供应商
绑定。首次设置在应用组合之前执行，所以缺失供应商时不会创建半成品运行时。
普通设置通过一级分类页进入独立的语言/供应商详情页。预设显式映射线路行为：OpenAI
Responses 使用 `openai-responses`，兼容 Chat 使用 standard 方言的 `openai-chat`，DeepSeek 使用
`dialect = "deepseek-v4"` 的 `openai-chat`。供应商详情
会在持久化前运行与运行时相同的 `HttpClientPolicy` 解析器；全局代理默认策略与可选的
单 profile 覆盖都是非敏感元数据；删除元数据与凭据前需要二次
确认，随后请求安全重载。启动预检会把无效的受管默认项送回该详情页，带上脱敏错误并选中
对应 profile；显式 CLI 覆盖和非受管配置仍在 CLI 边界失败。详见
[ADR 0046](adr/0046-global-cli-and-managed-provider-settings.md)和
[ADR 0047](adr/0047-recoverable-managed-provider-proxy-settings.md)。注入的
`ProviderCatalog` 端口为详情页提供独立、由用户触发的只读网络边界。其 HTTPX 适配器会
复用草稿的 `HttpClientPolicy`，只通过协议原生请求头发送凭据，并把 OpenAI 兼容/
Responses、Anthropic 与 Gemini profile 映射到各自模型列表端点。适配器最多读取一兆
字节、返回最多 200 个唯一模型标识，绝不显示错误响应正文，并分类错误供界面本地化恢复。
目录值只存在于当前设置页；凭据和远程响应都不会写入供应商元数据。没有目录接口的兼容
服务仍可手动输入模型。只读连接发现详见
[ADR 0048](adr/0048-bounded-provider-connection-discovery.md)。

受管 provider 值对象（`ManagedProviderProfile`、`ManagedProviderSettings`、
`ManagedProxyPolicy`）以及 `ProviderSettingsStore` 契约的 canonical owner 是
`neuro_code.application.ports.provider_settings`。历史
`neuro_code.domain.provider_settings` facade 已移除，且不包含第二份实现。详见 ADR 0074。

请求、有限结果、分类错误和端口类型的 canonical owner 是
`neuro_code.application.ports.provider_catalog`；原
`neuro_code.domain.provider_catalog` facade 已移除，不再存在第二份实现。

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

Provider 传输和协议失败在进入 resilience 之前会经过类型化边界。
`shared.errors` 中的 `ProviderFailure` 是不可变、有界且已脱敏的事实对象，包含 kind、
安全 detail、可选状态码/`Retry-After`、Provider/模型身份、生命周期 phase 和证据来源
（`provider`、`transport`、`local` 或 `unknown`），但不包含 retry、circuit 或 failover
决策。五个模型 HTTP 适配器先使用保守的通用 HTTP fallback，再分类本协议拥有的精确
结构化字段；通用 404 不断言为模型不存在，没有明确 rate code 的通用 429 不可重试，
通用 413 归为 invalid request。timeout/network 是 transport 事实，损坏的 Provider 流是
protocol 事实，非传输的意外 runtime 是 local 事实。`ConfigurationError` 保持独立，
取消原样传播。

`ProviderFailurePolicy` 独立拥有 retry、circuit 和 failover 决策。server、timeout 和
network 是瞬态熔断输入；明确的限流可以重试或隔离候选项，但不标记 Provider 不健康；
永久的请求、认证、授权、模型和上下文失败不会污染瞬态熔断。Provider/transport unknown
不重试、不计入熔断，但可以在输出前故障转移；local unknown 停在当前候选项。invalid
request 不故障转移，protocol 使用明确的保守策略。第一个模型事件之后同时禁止 retry 和
failover。`consecutive_failures` 表示自上次成功或不计入熔断的失败后，连续的、输出前且
有资格计入熔断的失败数。`ProviderHealth.last_failure_kind` 以及失败事件可选的
`failure_kind`/`status_code` 字段提供稳定且有界的事实，同时保留兼容性的
`last_error_type` 和原有事件字段。协议专属的 Anthropic `rate_limit_error` 和 Gemini
Generate Content `RESOURCE_EXHAUSTED` envelope 是明确的 rate-limit 事实；Anthropic
`billing_error` 仍是 authorization，未结构化或未来的通用 429 仍是 unknown。离线
fixtures 只覆盖列出的官方 envelope，不宣称完整 Provider 兼容或 live 验证。详见
[ADR 0126](adr/0126-provider-typed-failure-taxonomy.md)。

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

有序持久化会话项有独立的应用读取 owner：
`neuro_code.application.sessions.item_queries`。会话恢复/重载和显式会话导出共享其类型化请求与 tuple 投影；旧 session facade 保留保持 identity 的兼容导出。该 owner 不接管计划、评论、生命周期、事件或存储事务职责。

现有 application 消费者也直接导入具体规范 owner：bootstrap composition 与 TUI 使用
`application.providers.service` 和三个 `application.workflows.*` 模块；CLI/bootstrap/ACP
以及 CLI 序列化器使用具体的 session lifecycle、service 和 catalog 模块。聚合 package export
仍是兼容路径；这次导入收敛不创建第二套实现，也不改变工作流、锁、持久化、Runtime、Provider、
Finalizer、TUI 布局、ACP wire 或会话行为。由于 plan/comment/export 读取没有第二个生产消费者或
稳定的跨接口契约，仍由当前 owner 负责，不再重复拆分。

有界工具输出 artifact 应用边界也通过
`neuro_code.application.tools.service` 被 CLI、TUI、ACP、bootstrap 和 CLI 序列化器消费。
package 聚合入口只用于兼容；artifact 句柄、会话可见性、脱敏、字节上限、清理、权限、存储、
Runtime 与协议行为仍由 service 及其端口/适配器负责。

- deny 规则优先于 allow 规则和绕过模式。
- 无头执行把未解决的 `ask` 转换为拒绝。
- 具有副作用的工具在等待审批、被拒绝或审批等待取消后都不能启动。会话批准只覆盖
  完全相同的工具/参数摘要，仅保留在内存中，并从属于新的策略判定。
- 助手消息中持久化的每个本地工具调用，在上下文再次使用之前必须恰好具有一个工具结果。
  取消会给当前调用以及同一模型批次中的所有剩余调用记录错误结果。
- 写入前必须解析并校验目标；工作区工具不能通过 `..` 或符号链接逃逸。
- 平台无法实施显式沙箱要求时必须失败关闭。
- 每个启用的本地 child 都由 `LocalProcessSandbox` 启动器预检；不再存在 controller 范围
  的激活 marker 或挂载 attestation。启动器仍会在暴露 child 前校验可信辅助程序、显式
  挂载、私有状态以及 `strict` 白名单根目录的文件系统。
- 启用的 Linux child 使用 PID 命名空间作为后代生命周期边界，因此 `setsid()` 不能逃离
  timeout、cancel 或 shutdown。显式 POSIX `off` profile 只提供原始进程组清理，不提供
  文件系统、网络、controller 状态或任意后代隔离。
- 进程创建架构 guard 审计内置 production code。同进程 Python 扩展（`additional_tools`、
  注入 executor 与未来 plugin）以 controller 权限运行，因此属于可信代码；不可信 plugin
  必须另建进程/能力边界。
- `read-only` 会移除编辑工具并在直接调用时再次拒绝。`read-only` 与 `strict` 的本地进程
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
激活进程沙箱之前。schema v5 新增带 namespace、外键和一对一约束的外部 session
alias，供协议适配器使用；JSON export schema version 4 不变。schema v6 增加有界 JSON
计划列：该值由领域值校验，不进入可见内容搜索或会话导出，在恢复轮次前载入，并且只会作为持久
会话分叉的一部分复制。schema v7 新增带外键的会话任务表，用于保存不透明的计划执行生命周期
元数据。一个任务拥有一个开始时间和可选的终态时间；其中不含提示词、命令、模型输出或凭据，不会
进入 FTS、导出或导入，也刻意不会随分叉复制。schema v8 新增带外键的 `session_plan_comments`
表，用于保存最多 48 条、按当前计划规范指纹作用域划分的有界评论。它不进入索引、导出或导入；
带计划的分叉会以新的不透明 ID 复制它，而替换或清除计划会删除它。schema v9 会为每个计划执行任务增加可选的不可变计划快照。该快照标识被交接的准确
结构化修订，仍不进入 FTS 或导出/导入，也刻意不会随分叉复制；`/tasks` 只显示其短指纹和已完成步骤
数量。显式的当前会话精确任务查询可以在 TUI 中把同一已保存快照作为只读参考渲染，但它绝不会成为模型
输入或任务控制操作。schema v10 会为每个源会话增加一条带外键的最后安全终态执行记录。它必须引用已经
持久化的 `TURN_COMPLETED` 事件，只保存有类型的状态、reason code、finalized/recoverable 标志、事件序号和
时间戳。它刻意排除 prompt、工具参数/结果、证据、工作区 diff、supervisor snapshot、FTS、导出/导入和
分叉复制。后续成功的普通完成会覆盖此前可恢复的终态结果，因此恢复时可安全地区分最近完成的轮次和已暂停的
轮次，而不会把该记录当作可重放的模型上下文。运行时的终态成功路径使用有类型的
`SessionStore.finalize_turn` 边界：`TURN_COMPLETED` 事件、最终只追加的有序会话项、同步的标题/FTS
投影以及可选的用户轮次执行记录，在存储写锁下的一个短 SQLite 事务中一起提交。后台自动唤醒不传入记录，
因此不会覆盖此前的用户执行记录。这个边界不会让此前的轮次事件、供应商/工具工作或跨进程运行时操作变成原子操作。
在执行记录边界内，SQLite 会串行化写入，并拒绝更旧的事件序号或同一序号的冲突数据，因此过期进程不能覆盖更新的终态结果。
schema v12 新增带外键的 `subagent_links` 表. 每条链接只保存父会话 ID、父 `SUBAGENT` 任务 ID、
子会话 ID 和创建时间；子会话 ID 唯一，删除父会话会递归删除关联子会话. 保存链接是一个独立的短
SQLite 事务，并校验父任务处于运行中且子会话存在. 这不会让子会话创建、模型执行、任务完成和会话
事件成为一个事务. schema v13 新增外键关联的 `session_compaction_items` 表. 每行只保存有界
Provider/窗口元数据、源条目计数、半开候选范围、不透明源指纹、摘要 token 元数据、带时区时间戳以及
已经脱敏且有界的摘要. 它不进入 FTS、会话导出/导入或分叉复制；删除会话时级联清理. 相同 ID 的相同数据
可以幂等重复保存,冲突 ID 或重复源范围会失败关闭. `CompactionResumeRebuilder` 只应用源计数、Provider
来源和指纹均与当前上下文匹配且互不重叠的记录,生成临时合成摘要消息. 它不会调用 Provider、重放工具、
修改存储或声称整轮原子性. 没有压缩记录的既有会话恢复结果不变. Rust 会话由独立的只读适配器解析。
该适配器校验格式版本 0 和 1，以明确上限
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

第一版具体实现会在 Linux 上为 `workspace`、`read-only` 和 `strict` 的本地进程请求使用
子进程范围的 Bubblewrap；`off` 仍是可移植默认值。受信任 controller 不会在命名空间内
重新执行。每个 Bash、后台 Bash、stdio MCP 或启用 profile 的 PTY 请求都会获得独立 child
边界、显式工作区挂载、私有 HOME 和临时目录以及最小环境。只读和 strict child 还会使用
隔离网络命名空间。macOS 使用 child-scoped Seatbelt adapter；Windows 启用 profile 的非 PTY
request 使用下文的 W3 原生 restricted-token runtime；其余不支持的 request 仍然失败关闭。详见
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) 和
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md)。

W0 Windows AppContainer 调查区分了 primitive 可行性与生产就绪状态。AppContainer 文件系统/ACL、
named-pipe、runtime 与 standard-user primitive 均已实际验证，但在无法且不应扩展受保护 ancestor
ACL 的前提下，current stock Git for Windows 仍无法完成完整的非管理员 repository 工作流。因此，
classic stable unpackaged AppContainer 对启用的 Windows `workspace`、`read-only` 和 `strict`
profile 仍不支持并失败关闭；Windows `off` 路径继续使用现有 Job Object/ConPTY 生命周期。
Evidence PR #33--#39 保持未合并，并记录于
[ADR 0112](adr/0112-windows-appcontainer-sandbox-feasibility-decision.md)。

W1/W2 Windows 原生 foundation 记录在
[ADR 0113](adr/0113-windows-native-restricted-token-sandbox-architecture.md)。它增加
平台无关的文件系统/网络 security-capability model、内存内 restricted-token/SID
boundary 以及仅用于 installation 的 setup authority。由于启用的 Windows profile 仍然
失败关闭，W1/W2 actual runtime 文件系统/网络 capability 全部为 `UNSUPPORTED`。独立
native-backend target 是 read `LIMITED`、write `STRONG`、network `STRONG`；strong-read
request 不能由 limited provider 满足。Process lifecycle 继续由独立的
`LocalProcessLifecycleCapability` contract 负责，现有 Job Object/ConPTY path 报告
`STRONG_DESCENDANT_OWNERSHIP`。

W2 setup 维护真实 local user `NeuroSandboxOffline` 和 `NeuroSandboxOnline`、各自解析的
account SID、一个 installation-scoped synthetic restricting SID、DPAPI 保护的实际
account credential，以及显式 read/primary-user-write/restricting-write/read-only-deny/
sensitive-deny ACL plan。synthetic SID 只用于 restricted-token 的仅写 principal，不能作为
read 或 network identity。native
reconciliation 使用 `SetEntriesInAclW`，将 explicit deny canonicalize 到 allow 之前，同时
保留无关 controller ACE 和 owner；credential file 另外设置只针对两个 sandbox user 的
exact deny ACE。Offline outbound block 按真实 Offline account SID 限定；managed block
rule 在任一 dedicated identity 使用期间保持安装，只有显式 cleanup 才删除，且永远不
作用于 Online 或 controller user。setup state 为 `READY`、
`NEEDS_SETUP`、`NEEDS_REPAIR` 或 `UNSUPPORTED`；setup/repair/cleanup 可以需要管理员权限，
而 runtime 工作不需要持续提权。
W2 不启动 child、不连接 MCP、不增加 command runner、不改 Git/Python integration、不重写
Job Object/ConPTY，也不改变 foundation 的 actual capability constant。

W3 为 Windows Bash、后台 Bash 和 MCP stdio 的非 PTY runtime 增加
`CAPTURE`、`MERGED_CAPTURE` 与 argv-safe `PROTOCOL` 模式。每个 request 都先经过 W2
inspect；只有 `READY` 才能创建 child，否则在 child creation 前失败关闭。controller 使用选定的
Offline 或 Online account 启动可信且独立于 workspace 的 runner；runner 使用
restricting set 仅为 installation synthetic write SID 的 `WRITE_RESTRICTED` token 和
kill-on-close Job Object 创建最终 child。controller 与 runner 使用分离的
controller→runner control pipe 和 runner→controller event pipe，specific rights
排除 `FILE_CREATE_PIPE_INSTANCE`。Python `-I` 与显式环境是必要条件但并非 provenance
证明：在 `CreateProcessWithLogonW` 之前，resolved interpreter、runner module、Neuro Code
package root 与 dependency root 必须位于所有模型可写 root 之外。Everyone、logon、sandbox-user 与 controller SID
只作为 object ACL principal。runner 检查 `DISABLE_MAX_PRIVILEGE` 已保留
`SeChangeNotifyPrivilege`，且不会调用 `AdjustTokenPrivileges`。`ISOLATED` 选择 Offline，
`INHERIT` 选择 Online，且不会修改持久化 Offline Firewall rule。
完整接通的 W3 runtime 提供由 focused 原生验收认证的 read `LIMITED`、write `STRONG`、
network `STRONG` provider contract。W1/W2 foundation actual-capability constant 仍为
`UNSUPPORTED`，target constant 不参与 runtime admission。`strict` 因要求 strong read
isolation 而失败关闭。Gate 1–5 执行 7 个 native acceptance test 且 0 skip，证明
final-child identity、文件系统/网络 enforcement、binary/protocol transport、normal wait、
显式 termination、controller-loss cleanup 与 runner kill-on-close ownership。PTY/ConPTY 留给
W4，现有 `off` 路径保持不变；已接受的 W5 workload matrix（run `32374860136`）已通过
W3 与 W4 验证 Python/child Python、PowerShell、Git、Node/npm、curl、NUL 读写模式和动态
BCrypt 启动；未来 developer tool 仍需各自的有界证据行。

启用的 Linux 启动器会在挂载任何授权工作区前，对 controller 状态目录执行有界硬链接审计。
私有常规文件存在另一个 inode 名称时失败关闭，防止工作区中既存硬链接重新引入凭据或会话
状态，同时不扫描整个工作区。专用 Linux CI 必须无 skip 地执行真实 namespace 测试；专用
Windows CI 必须执行原生 Job Object 与 ConPTY 生命周期测试。

前台和受管后台 Shell 命令共享 `ProcessTree`。未启用沙箱的 POSIX 等待会在 Shell 入口退出后继续观察
受控进程组；终止使用有界 TERM→KILL 序列；创建新 session 的后代不属于该 `off` profile
进程组契约。Windows 上的惰性 ctypes 平台适配器会在
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

生命周期能力契约与文件系统、网络权限分离。`LocalProcessSandbox` 及其受管 child/terminal
接缝对启用的 Linux Bubblewrap 与 Windows Job Object 路径报告
`STRONG_DESCENDANT_OWNERSHIP`，对普通 POSIX ProcessTree 报告
`PROCESS_GROUP_BEST_EFFORT`。普通 Bash、background Bash、MCP stdio 和 PTY request 的最低要求是后者；
如果 workload 显式要求强所有权，best-effort 适配器必须在创建 child 前失败关闭。启用的 macOS
profile 使用 Seatbelt adapter 强制文件系统/网络/访问控制，并始终报告
`PROCESS_GROUP_BEST_EFFORT`。详见 [ADR 0110](adr/0110-cross-platform-lifecycle-capability-contract.md)
和 [ADR 0111](adr/0111-macos-seatbelt-local-process-sandbox.md)。

## Stage5DC ACP 生命周期 alias 兼容性

私有子代理生命周期适配器将外部 alias 分配限制为四次尝试，并在写入 wire 前通过
ACP 命名空间再次解析每个已分配的 alias。不可用、无法解析或归属于错误会话的 alias
会重试，耗尽后失败关闭。持久化存储的 `get_or_create` 保证同一子会话在重复
`resume` 请求和 ACP 客户端重连后继续使用同一个 alias。该切片不改变生命周期所有权、
子代理执行、schema、ACP 标准 capability 或明确的单子会话只读边界。详见
[ADR 0083](adr/0083-acp-subagent-alias-reconnect-compatibility.md)。

## 子代理 capability 闭包

所有生产 child-runtime 构造路径的 canonical parent authority 都是实际
`ConversationBinding.capabilities` manifest。无头 CLI 会在启动显式 child 前打开 parent binding；TUI
读取活动 binding；私有 ACP child 扩展要求活动 parent binding。缺少 metadata 时失败关闭。Scheduler 和
显式服务共用由 composition 拥有的 global policy。

显式只读工作流把 `READ_ONLY_SUBAGENT_TOOL_NAMES` 只当作 requested capability。它会在创建 child task 或
binding 前通过 `SubagentCapabilitySet.resolve_child()` 解析
`parent ∩ requested ∩ global_policy`，把同一个 manifest 传给 factory 和
`ApplicationComposition.create_binding(capabilities=...)`，并校验 runtime fingerprint。这阻止受限 child
恢复 root 工作区、工具、沙箱强度、MCP、terminal 或 network authority。旧版任意
`SubagentExecutor` binding 只作为明确标记的测试/内部兼容接缝保留，普通 composition 边界会拒绝它。子代理
关系的 `resume`、`fork` 和 `delete` 不会重新构造 Runtime；普通 ACP fork 是独立的 session binding。本闭包
只证明 `child capability <= actual parent capability`，不等于完整证明 Permission、Workspace、Sandbox、MCP、
Provider transport 或整个 Agent security system。详见 [ADR 0125](adr/0125-subagent-capability-closure.md)。

## Stage5DD 确定性上下文压缩评估

`neuro_code.application.memory.compaction` 是类型化 `ContextCompactionPlanner`、
用量快照、策略、决策和计划的 canonical owner。Planner 根据已知容量阈值以及受保护/近期
条目计数生成有界的半开候选区间；未知容量会明确返回 `UNAVAILABLE`。计划不包含会话条目、
提示词、工具输出、凭据、摘要或 Provider payload。

这只是评估契约，不会修改 `ModelContext`、创建可持久化摘要条目、调用 Provider、改变
`AgentRuntime`，也不会改变会话、CLI、TUI、ACP 或持久化行为。Provider 感知的总结、Provider
亲和回放、可持久化压缩条目和 Runtime 事务边界留待后续能力。详见
[ADR 0084](adr/0084-context-compaction-assessment-contract.md)。

## Stage5DE Provider 感知的摘要请求边界

canonical memory 模块现在还拥有 `ProviderContextWindow` 和
`ContextSummaryRequest`。窗口只记录有界的 Provider/模型标签、可选的上下文亲和标识以及
正的本地容量元数据。用量可以绑定到该窗口，可执行计划可以投影为带有有界、按容量裁剪的
摘要预算和仅包含索引的候选区间的请求。未知容量、不可执行的计划和空候选区间都会失败关闭。

这仍然只是 application contract：不增加 `ModelProvider` 参数、不调用 Provider、不对消息
进行 token 化或总结、不修改 `ModelContext`、不持久化压缩条目，也不改变 Runtime 和接口行为。
参见 [ADR 0085](adr/0085-provider-aware-context-summary-request.md)。

## Stage5DF Provider 感知的脱敏摘要输入

canonical memory 模块现在提供 `ContextSummaryInputBuilder` 以及类型化的
`ContextSummaryInput`、`ContextSummaryItem` 和 `ContextSummarySourceKind` 投影。Builder 接受
一个不可变的 `ModelContext` 与 `ContextSummaryRequest`，只投影候选区间，绝不会复制工具参数、
推理内容或保留的 Provider 载荷。这些内容会用有界固定标记表示。

显式值与形状识别脱敏发生在控制字符清理和 UTF-8 字节截断之前。注入的本地 token 估算器会在
扣除摘要预留后，将输入限制在 Provider 窗口剩余预算内。Builder 限制条目数和单条字节数，无法
放入预算的内容会被省略，结果对象的 repr 不包含条目文本。

这仍然只是输入契约：不调用 Provider、不选择 Provider 专用 tokenizer、不构建提示词、不修改
`ModelContext`、不持久化压缩条目，也不改变 Runtime/接口行为。参见
[ADR 0086](adr/0086-provider-aware-redacted-summary-input.md)。

## Stage5DH Provider 驱动的有界摘要生成

canonical memory 模块现在还拥有 `ProviderContextSummaryGenerator` 和
`ContextSummaryGenerationResult`。生成器只接受经过校验的 `ContextSummaryInput`，从其有界投影构建临时提示上下文，
并恰好使用无工具的 `ModelProvider` 请求与 `ModelToolPolicy.DISABLED`。调用前会校验请求窗口中的 Provider/model 身份。

生成器会缓冲文本增量，存在 `ModelCompleted.response_text` 时优先使用它。缺少完成事件、空响应、重复完成事件或远端工具调用会以
`ProviderError` 失败；Provider 错误与取消不会被隐藏。输出会再次脱敏并限制边界；生成器不会写入持久化、发送事件或修改源上下文。
自动 Runtime 压缩、重试、Provider 专用 tokenizer 和整轮事务语义仍是后续工作。参见
[ADR 0088](adr/0088-provider-backed-bounded-context-summary-generation.md)。

## Stage5DI 显式上下文压缩持久化服务

`neuro_code.application.memory.compaction_service` 现在拥有显式的
`ContextCompactionApplicationService`、`PersistContextCompactionRequest` 和
`ContextCompactionPersistenceResult` 边界。服务从不可变源上下文重新构建脱敏且有界的输入，在联系
Provider 前校验预期源指纹，调用已有的单请求摘要生成器，构建 `DurableCompactionItem`，并通过
`SessionStore.save_compaction_item` 持久化。

调用方提供不透明的 compaction ID 和预期源指纹。源条目数量或指纹漂移会在模型生成前失败。重复
ID 的幂等和冲突行为仍由存储适配器负责。Provider 生成与 SQLite 写入是两个独立操作，Provider 错误、
取消和存储错误不重试并继续传播。这只是显式应用能力：不会由 `AgentRuntime` 触发，不新增事件，
不改变 session item，也不宣称整轮原子性。参见 [ADR 0089](adr/0089-explicit-context-compaction-persistence-service.md)。

## Stage5DJ 压缩传输与回合最终化边界

`DurableCompactionItem` 仍然是优化记录而不是规范会话历史。`SessionExport` 有意排除压缩行，
因此 JSON/Markdown 导出和快照导入保持现有导出 schema 与规范会话条目，不暴露摘要、源指纹或 Provider 亲和元数据。
导入后的会话不包含压缩行。

会话分叉同样只复制规范会话投影，不复制压缩行：子会话可能偏离父会话的源范围和 Provider 窗口。
删除仍通过会话外键级联。

`SessionStore.finalize_turn()` 的原子性仍只覆盖完成事件、有序会话条目、搜索投影和可选执行记录。
压缩持久化是独立的短事务，不会被回合最终化隐式保存、删除或回滚。未来 Runtime 切片若需要跨操作原子性，
必须增加明确的存储契约；连续调用不能提供该保证。详见 [ADR 0090](adr/0090-compaction-transfer-and-turn-boundary.md)。

## Stage5DK 显式上下文压缩触发边界

`neuro_code.application.memory.compaction_trigger` 现在拥有类型化的
`ContextCompactionTriggerMode`、请求、评估、结果以及无状态的
`ContextCompactionTriggerService`。默认的 `DISABLED` 只运行现有确定性规划器，不执行任何 Provider 或存储操作。
`EXPLICIT` 可以把带非空候选区间的计划委托给现有上下文压缩持久化服务，但只有调用方提供会话 ID、压缩 ID、带时区的
时间戳和预期源指纹后才允许执行。过期源、Provider、取消和存储错误保持失败关闭，不会被转换为空操作结果。

触发服务刻意没有接入 `AgentRuntime`。它没有普通回合步骤计数器、重试状态、事件发出或跨操作事务声明。压缩生成和持久化
仍是两个操作，未来 Runtime 接入必须显式定义安全边界和预算语义。见
[ADR 0091](adr/0091-explicit-context-compaction-trigger.md)。

## Stage5DL 显式 Runtime 压缩安全边界

`neuro_code.application.memory.compaction_runtime` 定义了未来 Runtime 调用 Stage5DK 触发器前必须满足的边界。
当前只建模 `BEFORE_MODEL_REQUEST` 和 `AFTER_TOOL_BATCH` 两个安全位置；正在进行的模型请求、工具批次或取消请求都会失败关闭，
不会联系 Provider 或存储适配器。

该门控保持压缩计量与普通回合预算隔离：当前契约只允许一次模型请求、零次工具调用，且绝不继承普通回合限制。只有边界安全且触发器
显式启用并生成可执行计划时，才会委托 `ContextCompactionTriggerService`；否则返回类型化边界决定。这只是契约和测试接缝，
不会修改 `AgentRuntime`、事件或自动阈值触发。见 [ADR 0092](adr/0092-runtime-compaction-safe-boundary.md)。

## Stage5DM 强制执行 Runtime 压缩超时

Runtime 门控现在会在允许的显式压缩操作外层真正执行有限的墙钟预算。`ContextCompactionRuntimeBudget` 默认 30 秒且不能超过 300 秒；限制覆盖一次严格无工具的摘要请求及其后续持久化调用。截止时间会抛出类型化的 `ContextCompactionTimeoutError`，不会返回成功触发结果。Provider 错误、存储错误和任务取消保持不变。关闭、不安全、已取消和不可操作的请求仍不会调用 Provider 或存储。这仍然只是边界契约：普通 `AgentRuntime` 行为和自动压缩没有启用，也不宣称 Provider/SQLite 跨操作事务。见 [ADR 0093](adr/0093-enforced-context-compaction-timeout.md)。

## Stage5DN Runtime 压缩失败投影

`neuro_code.application.memory.compaction_runtime` 现在提供有界的
`classify_context_compaction_failure()` 策略投影。只有
`ContextCompactionTimeoutError` 拥有受控终态投影：`BUDGET_LIMITED`、原因
`WALL_TIME_BUDGET`、`recoverable=True` 且 `finalized=False`。其执行记录策略为
`TURN_FINALIZATION`，因此未来的回合所有者只有在现有回合最终化事务内才可以持久化它。
取消、Provider 错误和存储错误仍然只是传播投影，不携带 outcome，也不请求记录；未知异常
保持未分类。

该投影不保存异常详情，不捕获异常，不修改 `AgentRuntime`，不发出事件，不启用自动压缩，
也不声称 Provider/SQLite 跨操作事务原子性。见 [ADR 0094](adr/0094-runtime-compaction-failure-projection.md)。

## Stage5DO 显式 Runtime 压缩接缝

`AgentRuntime` 现在接受可选的 `compaction_runtime_gate`，默认值为 `None`，并为调用方完整提供的
`ContextCompactionRuntimeRequest` 暴露 `trigger_context_compaction()`。缺少 gate 时以
`ConfigurationError` 失败关闭；注入 gate 后会原样接收不可变的安全边界请求。facade 不推导阈值、
不修改上下文、不增加普通回合 steps、不发事件，也不写 execution record。

`AgentRuntime.run()` 和 ApplicationComposition 保持不变，因此自动压缩和生产 gate 组装仍然关闭。
超时、取消、Provider、存储和回合最终化所有权继续遵循
[ADR 0094](adr/0094-runtime-compaction-failure-projection.md)。见
[ADR 0095](adr/0095-explicit-runtime-compaction-seam.md)。

## Stage5DP：由应用层拥有的显式压缩调用方

`ApplicationComposition.create_binding()` 现在为每个 binding 使用现有 Provider、
`SessionStore`、脱敏值以及压缩触发/持久化服务组装一个
`ContextCompactionRuntimeGate`。gate 会注入 `AgentRuntime`，但仍然只可显式调用：
普通 Agent loop 不检查阈值，也不会自动调用压缩。

`AgentConversation.trigger_context_compaction()` 是应用层拥有的调用方。它在会话现有回合锁下运行，
对 `EXPLICIT` 请求要求匹配的持久化会话，并原样委托调用方提供的不可变
`ContextCompactionRuntimeRequest`。请求上下文是调用方拥有的快照，源指纹负责过期快照保护。
该方法不会修改 transcript 条目、发出事件、重新加载回合，也不声称与 `finalize_turn()` 具有原子性。
详见 [ADR 0096](adr/0096-application-owned-compaction-caller.md)。

## Stage5DQ：显式的回合最终化原子边界

`SessionStore` 现在暴露可选的 `finalize_turn_with_compaction()` 契约。SQLite 实现使用同一个
`BEGIN IMMEDIATE` 事务提交 `TURN_COMPLETED` 事件、会话条目、搜索投影、可选的
`SessionExecutionRecord` 和一个持久化压缩条目。校验、重复事件、压缩所有者/载荷冲突、唯一性、
索引和存储失败都会回滚整个单元。完全相同的已有压缩 ID 仍具有幂等性。

`save_compaction_item()` 和普通 `finalize_turn()` 继续保持独立短事务语义。该契约不包含 Provider
生成，不启用自动压缩，也不会被当前 Runtime 或显式压缩 gate 调用。详见
[ADR 0097](../en/adr/0097-atomic-turn-finalization-with-compaction.md)。

## Stage5DR：由回合记录器拥有的压缩最终化

`TurnEventRecorder.finalize_turn_completion()` 接受一个可选的、已经校验的
`DurableCompactionItem`。传入时，现有应用完成路径要求存在持久化会话，并将事件/条目/记录/压缩条目的组合提交委托给
`SessionStore.finalize_turn_with_compaction()`；普通调用仍使用 `finalize_turn()`。非法输入会在完成事件加入内存列表前失败，
持久化仍然先于 `TURN_COMPLETED` 的交付完成。

记录器只拥有这次最终存储提交。它不会生成摘要、调用 Provider、改变 Agent loop、消费失败投影或启用自动压缩。
详见 [ADR 0098](../en/adr/0098-turn-recorder-compaction-finalization-owner.md)。

## Stage5DS：类型化压缩回合投影

`neuro_code.application.memory.compaction_runtime` 现在提供
`ContextCompactionTurnProjection` 及显式成功/失败辅助函数。成功的显式压缩只转移已经持久化且校验过的
`DurableCompactionItem`。超时为未来回合所有者转移有界、可恢复的
`BUDGET_LIMITED/WALL_TIME_BUDGET` outcome；取消、Provider 和存储失败仍然只能传播，未知异常保持未分类。
投影不保存异常详情或原始摘要，也不执行持久化或发事件。它不会调用 `TurnEventRecorder`、接入普通 Agent loop
或启用自动压缩。详见 [ADR 0099](../en/adr/0099-context-compaction-turn-projection.md)。

## Stage5DT：显式压缩回合所有者

`TurnEventRecorder.finalize_turn_from_compaction_projection()` 是
`ContextCompactionTurnProjection` 的可选消费方。成功投影必须提供调用方的普通回合 outcome，并使用原子
`finalize_turn_with_compaction()` 路径。超时投影提供自身有界的可恢复 outcome，不伪造压缩行。
只能传播的投影和无操作投影会在内存完成事件追加前失败关闭。普通 Agent loop、自动压缩、Provider 生成和会话锁所有权都不属于该接缝。详见 [ADR 0100](../en/adr/0100-explicit-compaction-turn-owner.md)。

## Stage5DU：在回合锁下由应用层拥有压缩

`AgentConversation.run_context_compaction_with_owner()` 是显式且可选的应用层接缝。它校验调用方拥有的不可变请求，并在会话现有 `_turn_lock` 下运行 Runtime 压缩门控及类型化所有者回调。成功结果只转移已持久化的 `DurableCompactionItem`；有界超时转移已有的可恢复 `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome。无操作投影会在调用所有者前失败关闭；取消、Provider、存储和未知失败保留原始异常。

所有者仍负责 `TurnEventRecorder` 和任何最终化事务。本接缝不会进入普通 Agent loop、触发自动压缩、修改 transcript 条目、发出事件，也不声称 Provider 生成与 SQLite 持久化属于同一事务。详见 [ADR 0101](../en/adr/0101-application-compaction-owner-under-turn-lock.md)。

## Stage5DV：上下文用量快照与过期源请求构造

`neuro_code.application.memory.compaction_runtime` 现在提供
`build_context_usage_snapshot()` 与
`build_explicit_context_compaction_runtime_request()` 两个无副作用的应用层辅助入口。用量辅助函数在 Provider 输入/输出计数可用时遵循现有上下文用量事件约定，否则使用有界的 `ModelContext` 估算器并标记为估算。缺失的 Provider 容量保持未知，不从具体 Provider 实现推断。

请求构造器只进行确定性评估。它根据精确的不可变上下文和可执行候选区间计算不透明源指纹，仅在可执行的显式请求中要求调用方拥有的持久化元数据，对不可执行请求不伪造摘要。Provider/存储调用、会话加锁、执行时过期校验和自动压缩仍由现有应用/Runtime 接缝负责。详见 [ADR 0102](../en/adr/0102-context-usage-snapshot-and-stale-source-builder.md)。

## Stage5DW：显式实时上下文压缩命令

`AgentConversation.run_explicit_context_compaction_with_owner()` 现在是可执行显式压缩的窄应用命令。它会先获取现有会话回合锁，再要求 `AgentRuntime` 使用模型请求相同的推理、交互、指令和技能指引构建请求范围上下文快照。随后由已配置的 `ContextCompactionRuntimeGate` 复用 usage 快照，并从这份精确上下文计算过期源保护值。

命令在需要时生成有界身份和时间元数据，并在同一把锁内复用既有 typed owner 投影。它要求持久化会话，不追加 transcript、不发事件、不启动普通模型回合，也不启用自动阈值。Provider 生成和压缩持久化仍不属于同一事务。详见 [ADR 0103](../en/adr/0103-explicit-live-context-compaction-command.md)。

## Stage5DX：显式压缩命令投影

`neuro_code.application.memory.compaction_runtime` 现在提供有界的
`ContextCompactionCommandResult` 和
`project_context_compaction_command_result()` 应用层/接口层投影。
它区分 `completed`、`not_needed` 和受控超时的 `budget_limited` 结果。
成功结果只暴露不透明压缩 ID、源/候选条目数和摘要 token 元数据；绝不暴露摘要、源指纹、提示词、消息、
工具输出或异常详情。Provider、取消、存储和未知失败仍然只能传播为异常。CLI 和 ACP 序列化辅助函数共享同一组
有界字段，但不会启用命令、事件、普通 Agent loop 或自动压缩。详见 [ADR 0104](../en/adr/0104-explicit-compaction-command-projection.md)。

## 统一普通执行预算与临时 REPLAN 指引

`neuro_code.application.execution_policy` 把具名产品档位和旧 `max_steps` 覆盖值解析为
现有领域 `ExecutionBudget`。正式 CLI、TUI 与 ACP 组合路径会把同一个不可变值传给
`AgentRuntime`，因此 loop 硬上限和逐回合监督器共享同一份模型/工具预算。Finalizer 尝试
继续作为独立的有界资源。

在完整工具批次结束的安全边界，非终态 `REPLAN` 决策可以在 `FINALIZE_TERMINAL` 模式下
为下一次请求启用 `SyntheticReason.RUNTIME_SUPERVISION`。`ContextBuilder` 负责这条仅请求
可见的注入以及通用 batch-first 证据收集策略。两者都不会追加到会话条目；REPLAN 消息会
在产生新进展后通过追加一条有界的“已解决”通知来收束，而不是改写已经发送过的请求前缀；
回合退出时也不会持久化。工具执行顺序和现有 stuck 检测保持不变。详见
[ADR 0105](../en/adr/0105-unified-execution-budget-and-replan-guidance.md)。

## 有界长任务 Runtime 指引、压缩与分段

生产 `FINALIZE_TERMINAL` loop 现在通过 `ExecutionBudgetUsage` 投影其规范
`ExecutionBudget`。仅请求可见的指引和 `EXECUTION_BUDGET_UPDATED` 会暴露有界模型/工具计数，但不包含
提示词、工具载荷或监督器指纹。该事件仍可供需要它的接口投影使用；标准 TUI 则有意不在运行时栏展示
原始执行计数。

当 binding 同时具有持久化会话存储和已配置的 Provider 上下文窗口时，现有压缩门控会在
`BEFORE_MODEL_REQUEST` 与 `AFTER_TOOL_BATCH` 安全点评估自动压缩。压缩保持自身独立的一次请求/无工具
预算，保留规范 transcript 条目，不拆开工具调用/结果配对，并从最新兼容的持久化投影恢复。Provider、存储
和取消失败保持现有语义。相同区间不会重复摘要；压缩后仍越过 hard context threshold 时，会以可恢复的
`BUDGET_LIMITED/CONTEXT_WINDOW_BUDGET` 最终化。

持续产生进展的长回合还可以发出持久化且有界的 `EXECUTION_SEGMENT_CHECKPOINTED` 事件，并在下一请求
接收一次临时 checkpoint 指引。segment 阈值不重置或取代全局回合预算，也不承诺崩溃恢复或工作区回滚。
详见 [ADR 0107](adr/0107-bounded-long-task-runtime.md)。

## 面向 Prompt Cache 的模型请求投影与用量

`ContextBuilder` 拥有稳定的请求前缀：请求范围 system 策略、确定顺序的工具定义，以及当前序列化后的
项目指令和技能目录发现结果。发现结果会在每次请求时刷新，以便真实的工作区变更能够生效；但其源内容
不变时，有序序列化结果保持稳定。

可变的计划修订、segment checkpoint、预算压力和 REPLAN 状态不会再写回 system 消息，也不会插入到
持久化会话条目之前。`AgentLoopRunner` 会在安全的会话边界之后追加有界的合成运行时通知。预算指引只会
在离散的 `CONSERVE`、`FOCUS`、`FINAL_STAGE` 压力状态发生转换时追加，不会在每个模型步骤重写精确的
剩余计数。这些通知不会进入会话持久化、恢复重放或压缩源条目。后台任务完成提醒是一个有意保留的
单请求尾部例外：只有 Provider 成功完成后才会确认它。

因此，在未变化的长回合中，请求 *N + 1* 通常等于请求 *N* 加上新追加的持久化会话条目，以及至多一条
新近相关的有界运行时通知。这不承诺一定命中缓存：各 Provider 的缓存键、分词方式、保留时间和可缓存
条件不同；真实项目指令或技能发生变更时，使相应前缀失效正是正确行为。

`ModelCompleted.usage` 现在携带与 Provider 无关的 `ModelUsage` 值：Provider 原始的输入/输出字段，以及可选的
缓存读取（同时以 `cache_hit_tokens` 作为别名）、缓存写入和缓存未命中 token。输入 token 的语义会被明确标识。
大多数 Provider 上报总输入；Anthropic 上报缓存断点之后的未缓存尾部，只有 cache-read 与 cache-creation
字段足以精确计算时，Runtime 才会推导完整处理输入。`CONTEXT_USAGE_UPDATED` 只投影这些有界字段；接口可以
消费它们，但不会获得 prompt、工具参数或隐藏运行时上下文。OpenAI-compatible Provider 保留实际返回的
prompt-cache 字段，OpenAI Responses 保留 cached-input 详情，Anthropic 使用原生顶层 automatic ephemeral cache
control，使缓存断点可随着仅追加的 Agent 会话前移，并保留 cache creation/read 用量，Gemini 保留其实际返回的
implicit cached-content 用量。未上报字段保持 `None`；Runtime 不从总输入 token 推断缓存拆分，也不会声称命中了缓存。
