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
阶段 2A 将 `neuro_code.application.settings.ApplicationSettings` 和
`neuro_code.bootstrap.composition.ApplicationComposition` 建立为 canonical 路径。
`neuro_code.application` 仅保留惰性的 `ApplicationSettings` 包级导出；组合必须从
`bootstrap.composition` 显式导入，因此普通的 `application.ports` 导入不会加载 bootstrap 或
具体 infrastructure。审批交互契约现在只位于 `neuro_code.application.permissions.contracts`。
开发阶段的 breaking cleanup 已移除根级的 `PermissionApproval`、`PermissionApprovalKind`、
`PermissionRequest` 和 `build_permission_request` re-export；
`neuro_code.permissions` 只保留同步权限策略实现。

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

应用运行时行为现阶段位于 `neuro_code.application.runtime` 的明确 canonical 子模块：
`background_task_reminders`、`agent`、`conversation`、`profile_conversation`、
`terminal_sessions`、`approval`、`instruction_tracker` 和 `skill_tracker`。
开发阶段的 breaking cleanup 已移除 `neuro_code.runtime`；运行时应用行为仅可通过这些
明确的 canonical 子模块获得。`neuro_code.application.runtime.__init__` 现阶段保持最小，
不提供 aggregate API；内部生产代码直接导入 canonical 子模块。

`neuro_code.config` 现阶段负责 `AppConfig` 和 `ProviderProfile`、TOML 与 CC Switch
配置、环境覆盖、路由、managed overlay、sandbox 策略、stored credential 注入以及 HTTP
proxy policy。`neuro_code.configuration.managed_provider_settings` 中的同步 managed JSON
reader 负责 schema、protocol 和 dialect 检查、文件大小限制、metadata/credentials 合并、
结构校验以及 `ManagedProviderSettings` 构造。`neuro_code.adapters.provider_settings` 负责
`JsonProviderSettingsStore`、异步持久化、原子写入和 POSIX 私有权限。它通过私有绑定使用
canonical reader，不再 re-export 它。`neuro_code.config` 同样通过私有绑定使用 reader，且不再
导入 provider-settings adapter；该边界中的 `ProviderProfile` 和 `AppConfig` 取代已移除的
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

`AgentConversation` 是位于单轮 `AgentRuntime` 之上的可复用应用边界。它串行执行轮次，
并在每次持久提交后继续携带有序会话项、会话 ID 和供应商来源元数据。打开已有会话时，
它会校验记录的工作区与请求工作区是否指向同一文件系统位置。无头 CLI 和 Textual 界面
组合相同的控制器，因此恢复和供应商回放规则不会因界面不同而分叉。

发生失败或取消时，`AgentConversation` 会在释放轮次锁之前，从 `SessionStore` 重新加载
规范有序项和供应商来源。所以下一条提示会复用持久状态，而不是过期的内存前缀。取消的
用户消息仍保留在历史中；首 token 前回退是另一项尚未实现的交互策略。

## 仓库级 AGENTS.md 指令发现

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
通过 `domain/skills.py` 中的 `apply_skill_substitutions()` 展开正文中的
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
访问工具（`read_file`、`list_dir`、`grep`）触碰某路径时，`check_path()`
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
目录中的技能不再被包含。`SkillTracker.check_path()` 由 `ReadFileTool`、
`ListDirTool` 和 `GrepTool` 在已有的 `InstructionTracker.check_path()` 调用
旁边调用。`SearchReplaceTool` 不移动技能目标（其指令追踪器另有写入预检），
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
`ClientFileSystem` 应用端口。既有 `read_file` 工具仍会先通过选定的主工作区或额外工作区
根解析每个路径，再将绝对路径及有界行范围委托给 `fs/read_text_file`；它绝不会回退调用未声明
的客户端操作。当客户端同时声明文本读写能力时，`search_replace` 会使用同一端口读取、保留现有
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
查询与任务 ID 等有界白名单参数可进入调用摘要。每个本地工具调用再按调用 ID 持有一张
稳定卡片，后续在原地更新权限路径、脱敏结果预览、耗时和有界工作区变更报告。读取类调用
默认投影为一条完成说明，用户仍可打开已有的有界详情；编辑报告则自动展开变更切片，差异
角色同时使用前景色和淡色背景。详见
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
反向解析状态。纯折叠脉冲状态机由 Textual 定时器推进，并且只在等待模型输出时渲染到待
完成助手文案前。上下文用量先对规范会话项进行供应商中立的本地估算；模型完成事件带有
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
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md)。Neuro Code 通过自身的
原生终端测试验证此进程边界。

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
请求审批或具有调度语义；任务不存在或旧记录没有快照时不会显示细节。本切片刻意不写计划文件，也不提供
任务调度器或子代理生命周期。当前计划评论是刻意独立且有界的反馈通道：`/comment-plan STEP COMMENT` 会把用户
文本保存到编号计划步骤下，`/view-plan` 会渲染它，下一次模型请求才把它作为临时计划指引提供。评论
不是规范消息、批准、任务或执行请求。计划指纹阻止它泄露到替换后的计划；整体替换或清除计划会删除
过期评论。详见 ADR 0028、[ADR 0057](adr/0057-durable-structured-session-plans.md)、
[ADR 0058](adr/0058-durable-session-task-lifecycle.md) 与
[ADR 0059](adr/0059-bounded-current-plan-comments.md)、
[ADR 0060](adr/0060-plan-execution-revision-snapshots.md) 与
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md)。

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
`gemini-generate-content`）与 xAI Responses 等可选方言行为分离。通用 Responses 适配器实现位于
`neuro_code.providers.openai_responses.OpenAIResponsesProvider`；xAI 行为通过
`dialect = "xai"` 选择，而不是独立的 Python provider 类。开发阶段的 breaking cleanup 已移除
`neuro_code.providers.xai_responses` 和 `XAIResponsesProvider`。
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
Responses 使用 `openai-responses`，兼容 Chat 与 DeepSeek 使用 `openai-chat`。供应商详情
会在持久化前运行与运行时相同的 `HttpClientPolicy` 解析器；删除元数据与凭据前需要二次
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
输入或任务控制操作。Rust 会话由独立的只读适配器解析。该适配器校验格式版本 0 和 1，以明确上限
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
