# ADR 0049：渐进式模块化单体架构边界

**简体中文** · [English](../../en/adr/0049-progressive-architecture-boundaries.md)

- 状态：已接受
- 日期：2026-07-22
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
- 其中关于保留 Provider facade 的部分已由 Architecture Freeze v1 后的 ADR 0072 取代。
- 其中关于保留 Adapter、Tool 和 Domain 平面 facade 的部分已由兼容性清理审计后的 ADR 0074 取代。

## 背景

Neuro Code 已经通过领域值、带类型端口、应用编排和具体适配器交付纵向能力，但当前包结构
还没有一致地表达这些职责：`application.py` 以组合根身份选择具体适配器，部分应用运行时
模块直接导入工具和平台实现，CLI 与 ACP 界面也会直接构造或访问基础设施。

一次性重排所有包会把导入变更与行为修改混在一起，使会话、权限、沙箱、凭据、ACP 和
进程所有权回归难以隔离。因此，在移动实现之前，需要先确定目标依赖模型并建立可执行的
现状基线。

## 决策

Neuro Code 继续使用单一发行包和单一导入包，采用模块化单体与 Ports and Adapters。目标
职责如下：

- `domain`：纯领域值、不变量和规则；
- `application`：代理轮次、对话、权限、会话和流程编排；
- `application/ports`：应用行为所依赖的抽象；
- `infrastructure`：模型供应商、SQLite、文件系统、进程、PTY、沙箱、工具、MCP、HTTP
  和设置实现；
- `interfaces`：CLI、TUI、ACP 和其他入站适配器；
- `bootstrap`：配置加载、工厂、生命周期所有权和依赖装配；
- `shared`：错误、有界异步辅助、脱敏以及类似的小型跨层原语。

允许的依赖方向为：

```text
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

具体规则如下：

- `domain` 只能依赖标准库、`domain` 和 `shared`；
- `application` 可以依赖 `domain`、`application/ports` 和 `shared`；
- `infrastructure` 可以依赖 `domain`、`application/ports`、`shared` 和其他基础设施内部
  模块，但不能依赖 interfaces 或 bootstrap；
- `interfaces` 可以依赖面向应用的契约、领域值和 shared 辅助，但不能构造具体基础设施；
- `bootstrap` 是唯一允许同时依赖 `interfaces`、`application` 和 `infrastructure` 的层；
- `shared` 不得成为另一套组合根或无边界的依赖杂物箱。

application 和 domain 模块不得导入具体 infrastructure 实现。所有副作用继续通过带类型
端口以及现有权限、工作区、沙箱和平台边界。

配置加载属于 bootstrap，但被多个层引用的配置值对象不得定义在 bootstrap 中，否则这些
层会被迫依赖组合根。其最终归属在专门的配置拆分阶段确定。在此之前，
`neuro_code.config` 是明确的过渡边界，不会被过早归入 bootstrap。

架构迁移采用渐进策略：

1. 增加 canonical 新模块路径；
2. 保留旧路径，并从旧路径兼容 re-export 同一对象；
3. 切换内部导入并验证行为；
4. 只有在后续独立、明确批准且版本化的变更中才能删除旧兼容路径。

文件移动不得与行为修改发生在同一迁移阶段。移动代码的阶段只改变导入和装配；行为修改
必须作为独立纵向切片并带有自己的测试。

阶段 0 使用 Python 标准库 AST 增加依赖测试。当前每一条已知禁止直接导入都记录来源模块、
目标模块和原因。活动 allowlist 必须与源码中的实际违规完全一致，并且只能是冻结初始集合
的子集。消除违规时必须同时删除活动 allowlist 项；新增违规会让测试失败。修改冻结基线
属于架构决策，不是日常 allowlist 维护。

阶段 0 不移动实现，也不改变 CLI 参数、输出、退出码、运行时事件、配置优先级、数据库或
会话格式、ACP 行为、权限、沙箱或安全语义。

### 实施状态——2026-07-28

1. 应用运行时行为已 canonical 化到明确的 `neuro_code.application.runtime` 子模块。
2. 开发阶段的 breaking cleanup 已移除 `neuro_code.runtime`；运行时应用行为仅可通过
   明确的 canonical 子模块获得，且 `neuro_code.application.runtime.__init__` 保持最小。
3. 开发阶段的 breaking cleanup 已移除 `neuro_code.ports`；端口契约仅可通过
   `neuro_code.application.ports.*` 获得。
4. 开发阶段的 breaking cleanup 已移除根级 shared compatibility 模块
   `neuro_code.errors`、`neuro_code.async_utils` 和 `neuro_code.redaction`；其原语仅可通过
   对应的 `neuro_code.shared.*` 模块获得。
5. 开发阶段的 breaking cleanup 已移除 `neuro_code.application` 的包级 composition facade；
   其 `ApplicationSettings` 包级导出仍保留，composition 仅可从
   `neuro_code.bootstrap.composition` 获得。
6. 开发阶段的 breaking cleanup 已移除 `neuro_code.cli.main`。console scripts 和
   `python -m neuro_code` 继续使用 `neuro_code.bootstrap.entrypoints:main`，注入式
   `neuro_code.cli.run` 则保持为 CLI 核心。
7. managed-provider JSON reader 已拆分到
   `neuro_code.configuration.managed_provider_settings`。
8. `neuro_code.config` 不再导入 provider-settings adapter。
9. 开发阶段的 breaking cleanup 已移除 adapter 和 config namespace 中的 managed-provider loader
   re-export，并移除 `neuro_code.config.ProviderConfig`；此边界的公开 API 为 canonical
   reader、`JsonProviderSettingsStore`、`ProviderProfile` 和 `AppConfig`。
10. active temporary dependency allowlist 现已为空。
11. Stage 0 frozen baseline 仍是历史上限记录，未被重写。
12. 通用 dynamic-import architecture guard 现已扫描生产源码。开发阶段的 breaking cleanup 已移除
   ACP composition facade：`serve_acp` 只接受 `AcpApplicationService`。唯一剩余的 Bootstrap
   窄边是 canonical `neuro_code.__main__` package-executable entrypoint；它不属于待清除的兼容债务。
13. 通用 Responses 适配器只在
   `neuro_code.providers.openai_responses.OpenAIResponsesProvider` 中实现。xAI 仍是由
   `ProviderProfile` 选择的 `openai-responses` 方言；开发阶段的 breaking cleanup 已移除
   `neuro_code.providers.xai_responses` 和 `XAIResponsesProvider`。
14. 开发阶段的 breaking cleanup 已移除 `neuro_code.permissions` 中根级的审批契约
    re-export。请求和响应契约现在只可从
    `neuro_code.application.permissions.contracts` 获得，根级模块保留同步权限策略。
15. 其他 compatibility path 的删除仍是独立、版本化的决策。

16. 阶段 1 将 `neuro_code.domain.conversation` 确立为消息、模型上下文、Agent 事件和规范化
    Model 事件的 canonical owner。原有的 `neuro_code.domain.messages`、`events`、
    `model_context`、`model_events` 和 `context_usage` 模块保留为兼容 facade，并 re-export
    同一批对象。生产代码使用新的 canonical 路径；旧 facade 的删除仍是独立的兼容性决策。
17. 阶段 2A 将 `neuro_code.domain.execution` 确立为 canonical package。其公共聚合入口从
    `outcomes.py`、`tasks.py` 和 `checkpoints.py` re-export 唯一实现，验证辅助函数保持为该
    package 内部私有实现。公开的 `neuro_code.domain.execution` 导入保持兼容；原有 flat
    `execution.py` 实现被移除，而不是复制一份。
18. 阶段 2B 将 `neuro_code.domain.plans` 确立为 canonical package，由 `models.py` 作为唯一
    实现 owner。聚合 package 保留现有公共常量、计划值对象、fingerprint 和 update 校验 API；
    原有 flat `plans.py` 实现被移除，而不是复制一份。sessions、tools 和后台任务边界仍作为
    后续独立切片处理。
19. 阶段 2C 将 `neuro_code.domain.sessions` 确立为 canonical package，由 `models.py` 作为唯一
    实现 owner。聚合 package 保留原有标题规范化、会话摘要和会话快照 API；原有 flat
    `sessions.py` 实现被移除，而不是复制一份。会话搜索投影和存储适配器仍保持独立边界，
    不在本切片中移动。
20. 阶段 2D 将 `neuro_code.domain.tools` 确立为 canonical package，由 `models.py` 作为
    `ToolDefinition` 和 `ToolResult` 的唯一实现 owner。工具注册表、执行器、权限、sandbox、
    MCP 和后台任务生命周期仍位于这个纯值对象 package 之外。
21. 阶段 2E 将 `neuro_code.domain.session_tasks` 确立为 canonical package，由 `models.py`
    作为有界 `SessionTask` 状态机的唯一实现 owner。后台唤醒账本仍保持独立边界，因为其重启、
    预算和持久化语义需要单独进行迁移与事务审计。
22. 阶段 2F 将 `neuro_code.domain.background_tasks` 确立为 canonical package，由 `models.py`
    作为任务快照和唤醒账本值对象的唯一实现 owner。此次 package 迁移不改变 manager 副作用、
    SQLite 事务边界、进程树归属、取消或唤醒重试语义。
23. 阶段 2G 将 `neuro_code.domain.terminal` 确立为 canonical package，由 `models.py` 作为
    终端尺寸、信号、输出块和限制常量的唯一实现 owner。PTY、进程、sandbox、权限和 terminal
    manager 实现仍位于 domain package 之外。
24. 阶段 2H 将 `neuro_code.domain.sandbox` 确立为 canonical package，由 `models.py` 作为
    纯 `SandboxProfile` 策略值对象的唯一实现 owner。Shell sandbox port、bubblewrap/进程适配器、
    权限和取消实现仍位于 domain package 之外。
25. 阶段 2I 将 `neuro_code.infrastructure.sandbox.process_tree` 确立为具体
    `ProcessTree` 适配器的 canonical owner。旧 `adapters.process_tree` 路径保留为兼容 facade；
    进程所有权、终止、Windows Job Object、PTY、sandbox 策略和取消语义均不改变。
26. 阶段 2J 将 POSIX PTY 和 Windows ConPTY wrapper 适配器收敛到
    `neuro_code.infrastructure.sandbox`。旧 `adapters.posix_pty` 和 `adapters.windows_pty` 路径
    保留为兼容 facade；native ConPTY、Job Object、bubblewrap、权限和 terminal session 行为保持
    不变，且不复制第二份实现。
27. 阶段 2K 将 `neuro_code.infrastructure.sandbox.sandbox` 确立为子进程沙箱
    安全辅助函数的 owner。PR5 用规范的 `LocalProcessSandbox` 子进程启动边界替代了
    旧的 controller 范围 Linux bubblewrap `ShellSandbox` 实现。native Windows
    ConPTY 和 Job Object 实现仍保留在独立 adapter 中；sandbox 策略、fail-closed
    检查、权限和取消语义均不改变。
28. 阶段 2L 将无状态的 Windows 环境块原语确立为
    `neuro_code.infrastructure.sandbox.windows_process` 的 canonical owner。旧
    `adapters.windows_process` 路径保留为兼容 facade；Job Object、ConPTY 和 Windows 进程生命周期
    实现仍留在原 adapter，等待各自的平台切片。sandbox package 对具体平台模块采用惰性加载以避免
    循环导入；进程、权限和取消语义不改变。
29. 阶段 2M 将 `WindowsJobObject` 的唯一实现确立为
    `neuro_code.infrastructure.sandbox.windows_job` 的 canonical owner。旧
    `adapters.windows_job` 路径保留为兼容 facade；ProcessTree、JobProcess 和 ConPTY 使用
    canonical 对象，但 Win32 创建、kill-on-close、句柄所有权、终止和取消语义不改变。
30. 阶段 2N 将 `WindowsJobProcess` 的唯一实现确立为
    `neuro_code.infrastructure.sandbox.windows_job_process` 的 canonical owner。旧
    `adapters.windows_job_process` 路径保留为兼容 facade；ProcessTree 使用 canonical 进程包装器，
    但原子进程创建、继承句柄策略、流读取器、关闭、终止和取消语义均不改变。
31. 阶段 2O 将 native `WindowsPseudoConsoleSession` 的唯一实现确立为
    `neuro_code.infrastructure.sandbox.windows_conpty` 的 canonical owner。旧
    `adapters.windows_conpty` 路径保留为兼容 facade，共享的 `windows_pty` wrapper 使用 canonical
    类。伪控制台创建、resize、输入/输出 drain、中断、终止、关闭和取消语义均不改变。
32. 阶段 2P 将 `neuro_code.infrastructure.sandbox` 聚合边界固化为显式导入契约。
    其 `ProcessTree` 导出保持惰性并维持规范对象身份；导入任一平台专属 sandbox 模块都不得
    提前加载进程树实现。该检查用于保护跨平台导入隔离，且不移动或改变任何 sandbox 实现。
33. 阶段 2Q 将有界、只读的 HTTP 模型目录适配器唯一实现确立为
    `neuro_code.infrastructure.providers.provider_catalog`。旧
    `adapters.provider_catalog` 保留为兼容 facade，bootstrap 使用 canonical owner。Provider
    请求/流式契约、HTTP 脱敏、响应大小限制、错误映射和模型排序保持不变；Provider settings
    持久化明确作为独立切片处理。
34. 阶段 2S 为 `neuro_code.infrastructure.providers` 增加窄范围的惰性聚合导出，提供
    `HttpProviderCatalog` 和 `JsonProviderSettingsStore`。访问一个适配器只加载该适配器，不加载
    另一个适配器或模型 Provider SDK；聚合入口不拥有 Provider stream 行为或配置策略。
35. 阶段 2T 将无直接副作用的 `UpdatePlanTool` 适配器唯一实现确立为
    `neuro_code.infrastructure.tools.plans` 的 canonical owner。旧 `neuro_code.tools.plans` 路径
    保留为兼容 facade，registry 使用 canonical 工具。计划校验、脱敏、metadata 结构、SessionStore
    处理、权限和工作区行为保持不变；可执行工具明确留待独立切片。
36. 阶段 2U 将只读、有界的 `SkillTool` 适配器唯一实现确立为
    `neuro_code.infrastructure.tools.skills` 的 canonical owner。旧
    `neuro_code.tools.skills` 路径保留为兼容 facade，registry 使用 canonical 工具。符号链接/重解析点
    拒绝、workspace root 校验、有界读取、变量替换、脱敏、输出限制和取消语义保持不变；文件写入、
    shell 执行和 discovery 所有权仍是独立边界。
37. 阶段 2V 将只读 filesystem 工具（`ReadFileTool`、`ListDirTool` 和 `GrepTool`）及其
    共享 workspace-path helper 确立为 `neuro_code.infrastructure.tools.filesystem`
    的 canonical owner。旧 `neuro_code.tools.filesystem` 路径对这些工具保留兼容 facade，
    同时继续承载 `search_replace` 写工具；registry 使用 canonical 只读工具。路径解析、
    workspace 显示、主工作区跟踪、sandbox 边界、输出限制、脱敏和取消语义保持不变；
    写工具、shell 执行和后台任务 manager 有意推迟到后续切片。
38. 阶段 2W 将 `ToolRegistry` 和 `default_tool_registry` 工厂确立为
    `neuro_code.infrastructure.tools.registry` 的 canonical owner。旧
    `neuro_code.tools.registry` 路径保留为兼容 facade，公共 `neuro_code.tools`
    聚合入口 re-export 同一对象；bootstrap 使用 canonical 工厂。registry 是纯装配：
    工具实现只在工厂被调用时惰性导入，导入 registry 本身不会加载 bash、后台任务、
    client terminal、filesystem、plan 或 skill 实现。注册顺序、工具 identity、
    sandbox/workspace 门控和权限边界保持不变。
39. 阶段 2X 将只读后台任务工具（`TaskOutputTool` 和 `WaitTasksTool`）及其共享
    参数、快照和渲染 helper 确立为 `neuro_code.infrastructure.tools.background_tasks`
    的 canonical owner。旧 `neuro_code.tools.background_tasks` 路径对这些工具保留
    兼容 facade，同时继续承载 `kill_task` 写工具；registry 使用 canonical 只读工具。
    两个只读工具仅通过后台任务 port 观察受管任务，并经 `mark_completions_reported`
    消费完成簿记；task-id 校验、输出预览与截断、错误 metadata、取消、脱敏和 registry
    门控保持不变。后台任务 manager、bash 和进程所有权有意推迟到后续切片。
40. 阶段 2Y 将只读 ACP client terminal 工具（`ClientTerminalOutputTool` 和
    `ClientTerminalWaitTool`）及其共享 task-id/wait/渲染和 capability 门控 helper
    确立为 `neuro_code.infrastructure.tools.client_terminal` 的 canonical owner。
    旧 `neuro_code.tools.client_terminal` 路径对这些工具保留兼容 facade，同时继续承载
    `terminal_exec`、`terminal_start` 和 `terminal_kill`；registry 使用 canonical 只读工具。
    两个只读工具仅通过 `ClientTerminal` port 的 `get`/`wait` 方法观察受管任务；
    task-id 校验、wait mode/timeout 边界、输出预览与截断、错误 metadata、sandbox 门控、
    脱敏和取消保持不变。terminal 会话所有权、ACP capability 协商和前台执行路径有意推迟。
41. 阶段 3A 开始 Runtime Kernel 拆分：将 typed 工具观察协作器 `ToolObservationBuilder`
    确立为 `neuro_code.application.runtime.tool_pipeline` 的 canonical owner。
    builder 拥有 metadata-fact 白名单、workspace/background progress token、
    plan-from-tool-result 解析，以及原先内嵌在 `AgentRuntime` 的 fail-open 观察构建；
    `agent.py` 保留 `AgentRuntime`/`AgentRunResult` identity，并在调用点保留 fail-open
    日志。事件顺序、工具/ToolResult 配对、取消、supervision checkpoint 和 SessionStore
    事务保持不变；本切片不重接 `run()` 控制流。
42. 阶段 3B 将每轮 `TurnEventRecorder` 协作器确立为
    `neuro_code.application.runtime.event_recorder` 的 canonical owner。recorder
    拥有原先内嵌在 `AgentRuntime.run()` 闭包中的事件序列与 `emit` 持久化/投递、
    session-task 结束、turn 失败记录和终态完成记录。`AgentRuntime` 将 recorder 方法
    绑定为局部名，因此所有调用点、事件顺序、取消、pristine-rewind 跟踪和 SessionStore
    事务边界保持不变；`run()` 控制流仍未重接。
43. 阶段 3C 将 `ContextBuilder` 协作器确立为
    `neuro_code.application.runtime.context_builder` 的 canonical owner。builder
    拥有请求级 reasoning/interaction/plan guidance、仓库指令刷新、技能列表注入，
    以及可变的 `reasoning_effort`/`interaction_mode`/`plan`/`plan_comments` 状态。
    `AgentRuntime` 保留公共属性与 setter 作为薄委托，并保留
    `_model_items_with_reasoning_guidance` 私有委托 seam（现有测试直接调用）。
    guidance 注入顺序、plan comment 校验、权限模式应用和每步刷新语义保持不变；
    `run()` 控制流仍未重接。
44. 阶段 3D 完成工具管线切片：`ToolExecutor` 与 `ToolObservationBuilder` 一起成为
    `neuro_code.application.runtime.tool_pipeline` 的 canonical owner。executor 拥有
    原先内嵌在 `AgentRuntime` 的权限决策与交互审批、工具分发与执行、workspace
    快照/变更报告捕获、plan 交接持久化和未启动调用记录；`AgentRuntime` 的
    `_execute_tool` 调用点改为委托 executor。工具/ToolResult 配对、事件顺序、取消、
    脱敏、workspace 变更时机和 SessionStore 事务保持不变；`run()` 控制流仍未重接。
45. 阶段 3E 将每步 provider 流归一化确立为 `neuro_code.application.runtime.model_step`
    的 canonical owner。`ModelStepProcessor` 拥有七个模型事件分支、thinking 完成计时、
    provider 起源采纳簿记和 pristine cancel-eligibility 更新；`ModelStepResult`
    携带归一化的 step 文本、reasoning、工具调用和 completion 状态。`AgentRuntime.run()`
    每步通过 `on_imperfect` 回调消费一个 processor，pristine-rewind 标志仍由事件
    recorder 拥有。事件顺序、provider 事件、取消和 step 持久化保持不变；
    `run()` 控制流仍未重接。
46. 阶段 3F 完成 Runtime Kernel 拆分：`agent_loop` 成为每轮主循环
    （`AgentLoopRunner`）与 turn 结果值（`AgentRunResult`）的 canonical owner。
    runner 拥有原先内嵌在 `AgentRuntime.run()` 的步骤循环、supervision checkpoint
    序列、批次决策、终态化编排和证据收集；`AgentRuntime.run()` 现在是薄委托，并为
    兼容性 re-export `AgentRunResult`/`EventSink`。事件顺序、工具/ToolResult 配对、
    取消、事务和批次边界保持不变。

47. 阶段 4A 建立 `neuro_code.application.sessions` 作为第一个应用用例边界。
    `StartSessionRequest`、`SessionInspection` 和 `SessionApplicationService`
    通过 `SessionStore` port 提供类型化的启动/检查操作。`ApplicationComposition`
    持有一个可供入站适配器共享的服务实例。服务只返回安全的会话和执行记录投影，
    不暴露消息、prompt、工具参数、SQLite 细节或 Runtime 控制。会话创建仍是存储
    适配器的原子创建操作；创建后的 summary 读取和 execution record 投影则明确是
    分开的操作。本切片不重接 `AgentConversation`、AgentRuntime、CLI、TUI 或 ACP
    的行为。

48. 阶段 4B 在该会话服务旁建立类型化 turn seam：`RunTurnRequest` 只携带 prompt、内容片段、
    取消策略、turn 来源和可选的预期 session identity；`ResumeSessionRequest` 提供经过校验的
    只读 resume 预检。`SessionTurnService` 绑定已有 conversation runner，但不接管它的锁、task
    scope、持久化 context、事件 sink 或取消恢复。`SessionApplicationService.prepare_resume()`
    只返回安全的 summary/execution-record 投影；现有 `AgentConversation.open()` 和 composition
    binding 继续负责 workspace/sandbox 校验与 context 重建。CLI/TUI/ACP 入口不在本切片重接，
    resume 预检不会启动模型 turn。

49. 阶段 4C 只将 CLI 单轮路径接入应用层 turn seam。CLI 恢复会话时先执行只读的
    `ResumeSessionRequest` 预检，然后仍由现有 composition 创建 binding，再通过
    `SessionApplicationService.bind_runner()` 执行经过校验的 `RunTurnRequest`。绑定的 runner
    继续拥有 turn lock、task scope、持久化 context、事件 sink 投递、取消恢复和关闭顺序；CLI
    的 plain/JSON/JSONL 渲染及错误行为保持不变。TUI 和 ACP 保留现有路径，等待各自独立的受控
    切片。

50. 阶段 4D 将 ACP prompt 接入同一应用层 turn seam，但不改变 ACP wire 协议。
    `AcpApplicationService` 复用 composition 提供的 `SessionApplicationService`；
    `NeuroCodeAcpAgent.prompt()` 用转换后的内容片段和已知内部 session identity 创建
    `RunTurnRequest`，再委托给 `SessionTurnService`。现有 ACP alias 解析、resume 预检、binding
    所有权、事件映射、取消、ProviderError 映射和清理仍由原有 owner 负责。本切片不改变 CLI 和 TUI。

51. 阶段 4E 将 TUI 用户 turn 和 resume 边界接入共享的会话应用层 seam。bootstrap 对命令行 resume
    和交互式会话选择执行只读 `ResumeSessionRequest` 预检，然后通过 `SessionTurnService` 绑定现有的
    `ProfileConversationController`。`NeuroCodeApp` 只在用户 turn 中使用该 service；后台唤醒和计划
    操作继续使用原有 controller 契约。controller 接受类型化的内容片段，同时保持 turn lock 和
    runner 生命周期。布局、快捷键、流式输出、取消、持久化和 TUI 渲染行为保持不变。

52. 阶段 4F 增加 `ForkSessionRequest` 和
    `SessionApplicationService.fork_session()`，作为持久会话副本的共享类型化应用用例。service
    只校验不透明的源 session intent，并委托 `SessionStore.fork_session()` 的原子操作；不暴露
    SQLite 行、context、消息或 provider 状态。ACP adapter 在保留既有工作区、alias、binding、MCP、
    发布和回滚门控后，改为调用这个共享 service。本切片不改变 CLI/TUI 行为、Runtime turn、权限流
    或存储事务语义。

53. 阶段 4G 建立类型化的 `ApproveToolRequest` 与 `ToolApprovalService` 交互工具审批
    seam。service 只接收已经有界的 `PermissionRequest` contract，并委托现有
    `PermissionApprover` port；它不拥有策略、会话批准缓存、UI handler、原始参数或工具
    执行。`ApplicationComposition` 在构造 Runtime 前为每个 binding 包装 approver，使
    CLI/TUI/ACP binding 共享同一个应用边界，同时保持审批顺序、取消传播、fail-closed 行为
    和持久化语义不变。

54. 阶段 4H 建立 `ProviderChangeService` 作为 `ChangeProviderRequest` 用例的非拥有型
    application seam。现有 `ProfileConversationController` 继续是 Provider 可用性校验、turn
    lock、新 `ConversationBinding` 创建、策略传播以及新旧后台 task scope 关闭的唯一 owner。
    bootstrap 将该 owner 绑定到类型化 facade，TUI 通过 `change_provider()` 提交经过校验的请求。
    本切片不改变 Provider 协议、会话持久化、模型 turn、取消或资源所有权语义；其他入站适配器
    保持原路径，等待单独审计。

55. 阶段 4I 建立 `PlanExecutionService` 作为 `ExecutePlanRequest` 的首个类型化应用层
    workflow seam。现有 `ProfileConversationController` 继续拥有已保存计划校验、turn
    锁、SessionTask 生命周期、权限、事件投递和取消语义；`ApplicationComposition` 负责
    绑定该 owner，TUI 的直接 `/execute-plan` 入口通过 facade 提交类型化请求。排队任务
    调度和 `run_session_task()` 在独立生命周期切片完成审计前保持原 controller 路径；
    本阶段不新增 workflow engine、不重写 AgentRuntime，也不新增持久化。

56. 阶段 4J 建立 `QueuedPlanExecutionService` 作为显式 `/run-task <task_id>` 入口的
    类型化应用层 seam。`RunSessionTaskRequest` 只携带经过校验的任务 identity；现有
    conversation/runtime owner 继续负责 queued→running 原子 claim、计划快照校验、任务
    完成/失败/取消更新、事件投递和 turn 锁。计划调度及其 SessionStore 创建操作在独立
    切片审计前保持原 controller 路径；本阶段不新增状态机或持久化。

57. 阶段 4K 建立 `PlanSchedulingService` 作为 `/schedule-plan` 入口的类型化应用层
    seam。由于调度操作的是当前已保存计划，`SchedulePlanRequest` 是空的、不可变的命令，
    不携带 SQLite identity 或计划副本。现有 conversation/profile controller 继续拥有已保存
    计划和 session 校验、turn 锁、排队上限、`SessionTask` 创建以及
    `SessionStore.create_session_task()`；TUI 在绑定 service 时通过 facade 调用，并为旧测试
    构造保留 controller fallback。本阶段不改变队列状态转移、持久化事务、取消或任务执行。

58. 阶段 5A 建立 `neuro_code.infrastructure.persistence.sqlite_session` 作为公开的
    `SqliteSessionStore` canonical identity。bootstrap composition 现在从该 infrastructure
    模块构造 store；`neuro_code.adapters.sqlite_session` 仍作为兼容 facade 保留已有导入和
    低层 SQLite 测试 seam。首个 persistence 切片刻意不改变 schema 版本、迁移、连接锁、事务或
    `SessionStore` port；实现主体的实际迁移另作为独立审计步骤完成。

59. 阶段 5B 将 SQLite SessionStore 的实现主体以及 schema、迁移、序列化、搜索和行转换 helper
    全部迁移到 `neuro_code.infrastructure.persistence.sqlite_session`。旧 adapter 现在只做
    单向兼容 re-export，并与 canonical class 保持完全相同的对象身份。生产 composition 和
    entrypoints 继续使用 infrastructure 路径，低层测试改为在 canonical owner 上 patch。
    schema 版本、迁移顺序、连接锁、事务边界、port 行为和公开 SessionStore 语义均保持不变。

60. 阶段 5C 将两个会话级 MCP transport 实现迁移到
    `neuro_code.infrastructure.mcp.stdio` 和 `neuro_code.infrastructure.mcp.http`。旧的
    `mcp_stdio` 与 `mcp_http` adapter 保留为单向兼容 facade，bootstrap 以及 HTTP transport
    直接依赖 canonical stdio owner。MCP 限制、脱敏、取消、进程所有权、官方 SDK 行为和 ACP
    wiring 均保持不变；本切片只改变实现 owner。

61. 阶段 5D 将文件系统 workspace 路径边界与有界变更观察实现迁移到
    `neuro_code.infrastructure.workspace.paths` 和
    `neuro_code.infrastructure.workspace.changes`。旧的顶层
    `workspace.py` 与 `workspace_changes.py` 保留为单向兼容 facade，bootstrap 和只读文件系统
    工具改用 canonical owner。workspace identity 匹配、路径逃逸拒绝、额外根目录边界、快照限制、
    脱敏、diff 序列化、observer checkpoint、权限/sandbox 顺序和 Runtime workspace report
    语义均保持不变。

62. 阶段 5E 将具体模型 Provider 适配器、failover 链、图像引用 helper 和 Provider factory
    迁移到 `neuro_code.infrastructure.providers`。旧的 `neuro_code.providers` package 与各
    Provider 子模块保留为单向兼容 facade，bootstrap 改用 canonical factory。Provider 请求 payload、
    `ModelToolPolicy`、流式事件、failover 顺序、取消和错误语义保持不变；本阶段只迁移实现 owner，
    不改变 Provider 协议或 Runtime 行为。

63. 阶段 5F 将 Bash shell 命令工具的实现迁移到
    `neuro_code.infrastructure.tools.bash`。旧的 `neuro_code.tools.bash` 保留为单向兼容 facade，
    canonical registry 以 lazy import 使用 infrastructure owner。权限与 sandbox 校验、前台/后台提升、
    进程树终止、有界输出、取消、超时和 ToolResult 语义保持不变；本阶段不迁移写工具或后台 manager 的 owner。

64. 阶段 5G 将原子 `SearchReplaceTool` 实现迁移到与只读文件工具同属的 canonical
    `neuro_code.infrastructure.tools.filesystem` owner。旧的 `neuro_code.tools.filesystem` 保留为
    单向 facade，canonical registry 从 infrastructure 导入写工具。workspace 路径解析、instruction
    preflight、权限/sandbox 边界、client filesystem 委托、原子替换、脱敏、取消和 ToolResult 语义保持不变；
    本阶段不迁移其他写工具或后台 manager。

65. 阶段 5H 将直接 `ClientTerminalTool`（`terminal_exec`）实现迁移到 canonical
    `neuro_code.infrastructure.tools.client_terminal` owner。旧的
    `neuro_code.tools.client_terminal` 继续拥有会话生命周期相关的 `terminal_start` 与
    `terminal_kill`，并 re-export 已迁移的执行工具。client-terminal 能力与 sandbox 校验、命令/参数/超时
    校验、有界输出、状态校验、取消和 ToolResult metadata 语义保持不变；本阶段有意不迁移终端会话生命周期 owner。

66. 阶段 5I 完成 client-terminal 工具的 canonical owner 迁移，将
    `ClientTerminalStartTool` 与 `ClientTerminalKillTool` 也迁移到
    `neuro_code.infrastructure.tools.client_terminal`。旧的
    `neuro_code.tools.client_terminal` 变为五个 client-terminal 工具的单向 facade，canonical registry
    从 infrastructure 导入完整工具族。client-terminal port 调用、能力与 sandbox 校验、任务生命周期、
    取消、有界输出、metadata 和 ToolResult 语义保持不变；本阶段不迁移后台任务 manager。

67. 阶段 5J 完成后台任务工具的 canonical owner 迁移，将 `KillTaskTool` 与
    `TaskOutputTool`、`WaitTasksTool` 一起放入
    `neuro_code.infrastructure.tools.background_tasks`。旧的
    `neuro_code.tools.background_tasks` 现在只作为单向兼容 facade，canonical registry
    从 infrastructure 导入完整后台工具族。后台任务 manager、进程 ownership、SQLite 记账、取消、
    completion-reporting、权限和 ToolResult 语义均保持不变；本阶段只迁移工具 implementation owner。

68. 阶段 5K 将进程驱动的 `LocalBackgroundTaskManager` 实现迁移到
    `neuro_code.infrastructure.background_tasks`。旧的
    `neuro_code.adapters.background_tasks` 变为单向兼容 facade，bootstrap 直接组合 canonical owner。
    `BackgroundTaskManager`/`BackgroundTaskSupervisor` port、进程树 ownership、scope 隔离、有界输出、超时、
    取消、任务保留、completion reporting 和公开 snapshot 语义均保持不变；SQLite 持久化和 Runtime 生命周期
    ownership 不在本阶段迁移。

69. 阶段 5L 将有界 filesystem instruction discovery 实现迁移到
    `neuro_code.infrastructure.workspace.instructions`。原
    `neuro_code.adapters.instruction_discovery` 保留为只导出
    `FilesystemInstructionDiscovery` 的单向兼容 facade；bootstrap、skill discovery 和
    canonical skill tool 都直接从 canonical owner 导入共享安全 helper。workspace 边界校验、
    symlink/reparse 拒绝、具备 TOCTOU 防护的有界读取、编码/控制字符校验、fingerprint、限制、
    脱敏和 instruction tracker 语义保持不变。本阶段有意不迁移 skill discovery 自身的实现 owner。

70. 阶段 5M 将有界 `FilesystemSkillDiscovery` 实现迁移到
    `neuro_code.infrastructure.workspace.skills`。原
    `neuro_code.adapters.skill_discovery` 保留为单向兼容 facade，bootstrap 和 skill discovery 测试
    使用 canonical owner。skill scope 排序、git-root 与 home/workspace 边界、symlink/reparse 拒绝、
    有界遍历与读取、frontmatter 解析、变量替换、去重、fingerprint、脱敏、取消和 tracker/tool 语义保持不变。
    本阶段有意不迁移 `SkillTool` 实现自身。

71. 阶段 5N 将原子 `JsonUiPreferencesStore` 实现迁移到
    `neuro_code.infrastructure.persistence.ui_preferences`。原
    `neuro_code.adapters.ui_preferences` 保留为单向兼容 facade，bootstrap 和直接持久化测试
    使用 canonical owner。偏好 schema 校验、English/high/normal 默认值、序列化写入、私有文件权限、
    原子替换、写入串行化和 `UiPreferencesStore` port 均保持不变；更大范围的 Rust session importer
    有意留到独立阶段。

72. 阶段 5O 将只读的上游 Rust session importer 实现迁移到
    `neuro_code.infrastructure.persistence.rust_session`。原
    `neuro_code.adapters.rust_session` 保留为单向兼容 facade；bootstrap、Rust 导入测试和 persistence
    aggregate 对外暴露 canonical owner。summary 与 JSONL 安全限制、时间戳和 sandbox 校验、有界内容转换、
    上下文保留、源文件不可变性以及 `SessionError` 语义均保持不变；本阶段只改变实现 owner，不改变
    SessionStore、resume、Runtime、Provider 或会话协议语义。

73. 阶段 5P 将确定性的权限策略实现迁移到
    `neuro_code.application.permissions.policy`。旧的根模块
    `neuro_code.permissions` 保留为单向兼容 facade；Runtime、settings、composition、CLI
    和 terminal-session owner 直接导入 canonical policy。权限模式、效果、规则匹配、bash
    命令分析、审批契约、脱敏和拒绝行为保持不变；本阶段只改变策略实现 owner，并明确不让
    策略模块重新导出 approval contract。

74. 阶段 5Q 将纯函数、保守的 Bash 命令分解器确立为
    `neuro_code.domain.permissions.bash_commands` 的 canonical owner。旧的根模块
    `neuro_code.bash_commands` 保留为单向兼容 facade，application permission policy 和
    approval contracts 直接导入 canonical domain 模块。词法分析、wrapper 处理、fail-closed
    分类、递归限制、脱敏边界和权限语义保持不变；本阶段只改变值对象/解析器的 ownership 边界。

75. 阶段 5R 将应用配置加载、Provider profile 模型、代理策略和配置覆盖实现确立为
    `neuro_code.configuration.app` 的 canonical owner。原
    `neuro_code.config` 保留为单向兼容 facade，同时保留历史 `Path` patch seam。
    配置解析、managed-provider settings 集成、sandbox 与 provider 覆盖、代理解析、脱敏和错误语义保持不变；
    本阶段只改变实现 owner，并增加 canonical import isolation 护栏。

76. 阶段 5S 将纯会话搜索投影、fallback 标题生成和可搜索文本构建实现确立为
    `neuro_code.domain.sessions.search` 的 canonical owner。原
    `neuro_code.domain.session_search` 保留为单向兼容 facade；domain aggregate、storage port、SQLite
    persistence、Runtime 会话搜索、CLI、bootstrap 和测试均改用 canonical owner。搜索值校验、system-reminder
    过滤、Provider 私有上下文排除和会话持久化语义保持不变；本阶段只改变 domain ownership 与 import 边界。

77. 阶段 5T 将纯会话交互模式枚举及其供应商中立指引实现确立为
    `neuro_code.domain.conversation.interaction_mode` 的 canonical owner。原
    `neuro_code.domain.interaction_mode` 保留为单向兼容 facade；domain aggregate、application port/runtime、
    infrastructure 偏好持久化、bootstrap、TUI 和测试均改用 canonical owner。模式值、glyph、循环切换、指引文本、
    权限映射、持久化和用户可见行为保持不变；本阶段只改变 domain ownership 与 import 边界。

78. 阶段 5U 将供应商中立的 `ReasoningEffort` 值对象及审查指引实现确立为
    `neuro_code.domain.conversation.reasoning` 的 canonical owner。原
    `neuro_code.domain.reasoning` 保留为单向兼容 facade；ModelContext、application settings/ports/runtime、
    bootstrap、CLI、ACP、TUI、偏好持久化和测试均改用 canonical owner。强度值、glyph、`ultracode` 的实际回退、
    指引文本、偏好持久化和用户可见行为保持不变；本阶段不增加供应商私有 reasoning 参数，也不启动 workflow 编排。

79. 阶段 5V 将绑定级 AGENTS.md 指令发现 tracker 的实现确立为
    `neuro_code.application.memory.instruction_tracker` 的 canonical owner。原
    `neuro_code.application.runtime.instruction_tracker` 保留为单向兼容 facade；Composition
    直接导入 application memory owner。工作区边界、子树隔离、每步重新发现、写入前置检查、指令注入和 tracker
    行为保持不变；skill tracker 与 Runtime 主循环明确不在本阶段范围内。

80. 阶段 5W 将绑定级 SKILL.md 技能发现 tracker 的实现确立为
    `neuro_code.application.memory.skill_tracker` 的 canonical owner。原
    `neuro_code.application.runtime.skill_tracker` 保留为单向兼容 facade；Composition
    直接导入 application memory owner。工作区边界、子树隔离、每步重新发现、技能注入和 tracker
    行为保持不变；本阶段只改变 application ownership 与 import 边界。

81. 阶段 5X 将 `ProviderConnectionSpec`、`ProviderCatalogResult`、
    `ProviderCatalogError` 和 `ProviderCatalog` 端口的 canonical owner 确立为
    `neuro_code.application.ports.provider_catalog`。原
    `neuro_code.domain.provider_catalog` 保留为单向兼容 facade，并按旧端口路径
    进行正式分类。凭据承载的探测输入、有限模型目录、脱敏错误行为和 HTTP 供应商发现行为保持不变；
    本阶段只改变契约 ownership 与 import boundary。

82. 阶段 5Y 将 `ManagedProviderProfile`、`ManagedProviderSettings`、
    `ManagedProxyPolicy` 和 `ProviderSettingsStore` 端口的 canonical owner 确立为
    `neuro_code.application.ports.provider_settings`。原
    `neuro_code.domain.provider_settings` 保留为单向兼容 facade，并按旧端口路径进行正式分类。
    managed profile 校验、代理与后台唤醒覆盖语义、凭据脱敏、JSON 持久化和 TUI 行为保持不变；
    本阶段只改变契约 ownership 与 import boundary。

83. 阶段 5Z 将跨层 `UiLanguage` 原语的 canonical owner 确立为
    `neuro_code.shared.ui_language`。原 `neuro_code.domain.ui_preferences` 保留为单向兼容 facade。
    UI 偏好端口、持久化、TUI 和本地化文案直接导入 shared owner；语言值、持久化格式和 UI 行为保持不变。
    本阶段只改变 shared 原语的 ownership 与 import boundary。

84. 阶段 5AA 将纯 `AGENTS.md` 指令值对象和有界投影辅助函数的 canonical owner
    确立为 `neuro_code.domain.workspace.instructions`。原
    `neuro_code.domain.instructions` 模块只保留单向兼容 facade；文件系统发现实现仍位于
    `neuro_code.infrastructure.workspace.instructions`。指令校验、fingerprint、合成消息构造、
    发现上限、脱敏边界和 tracker 行为保持不变；本阶段只改变 domain owner 和 import boundary。
85. 阶段 5AB 将纯 `SKILL.md` 元数据值对象、有界解析和投影辅助函数的 canonical owner
    确立为 `neuro_code.domain.workspace.skills`。原
    `neuro_code.domain.skills` 模块只保留单向兼容 facade；文件系统发现仍位于
    `neuro_code.infrastructure.workspace.skills`，技能正文读取仍由只读基础设施工具负责。
    技能校验、替换、fingerprint、目录渲染、合成消息构造和发现行为保持不变；本阶段只改变
    domain owner 和 import boundary。
86. 阶段 5AC 将应用层会话/profile 协调器及其 typed binding/selection 投影的 canonical owner
    确立为 `neuro_code.application.sessions.profile_conversation`。原
    `neuro_code.application.runtime.profile_conversation` 模块只保留单向兼容 facade。
    Provider 选择、会话选择、交互模式与推理强度策略、轮次串行化、binding 替换、后台任务作用域
    关闭和 runner 委托行为保持不变；本阶段只改变 application owner 与 import boundary，不迁移
    Runtime Kernel、Provider、SessionStore、workflow 执行或入站 UI 协议。

87. 阶段 5AD 将有界交互式终端会话实现的 canonical owner 确立为
    `neuro_code.application.sessions.terminal_sessions`。原
    `neuro_code.application.runtime.terminal_sessions` 模块只保留单向兼容 facade。
    权限、工作区、匹配沙箱、输出环、进程生命周期、取消、关闭以及
    `InteractiveTerminalManager` 端口行为保持不变；本阶段只改变 application owner 与
    import boundary，不迁移原生平台适配器、AgentRuntime kernel、ACP framing 或终端 wire 契约。

88. 阶段 5AE 将多轮 `AgentConversation` 控制器的 canonical owner 确立为
    `neuro_code.application.sessions.conversation`。原
    `neuro_code.application.runtime.conversation` 模块只保留单向兼容 facade。轮次锁、会话恢复、
    供应商来源校验、计划/任务协调、后台唤醒委托、执行记录重载、取消恢复以及现有
    Runtime/Provider/SessionStore 行为保持不变；本阶段只改变 application owner 与 import boundary，
    不拆 Runtime Kernel，也不重接入站协议。

89. 阶段 5AF 将 `ApprovalHandler` 与 `SessionApprovalBroker` 的 canonical owner 确立为
    `neuro_code.application.permissions.broker`。原
    `neuro_code.application.runtime.approval` 模块只保留单向兼容 facade。交互式审批路由、会话范围缓存、
    无 UI handler 时的 fail-closed 行为、审批对象 identity、取消和工具执行行为保持不变；本阶段只改变
    application owner 与 import boundary。

90. 阶段 5AG 完成 canonical 沙箱策略 owner
    `neuro_code.domain.sandbox.models` 的生产 import 收敛。原
    `neuro_code.domain.sandbox` 包级模块继续作为单向兼容 facade；application、infrastructure、bootstrap
    和 configuration 消费者直接从 models owner 导入 `SandboxProfile`。沙箱 profile 解析、失败关闭策略、进程隔离、
    持久化、取消和用户可见行为保持不变；本阶段只改变 import boundary，并增加阻止生产代码重新导入 facade 的负向契约。
91. 阶段 5AH 完成 canonical 终端值对象 owner
    `neuro_code.domain.terminal.models` 的生产 import 收敛。application 终端端口、application 终端会话 owner、POSIX PTY、
    Windows PTY 以及 domain aggregate 直接导入 terminal models；原
    `neuro_code.domain.terminal` 包级模块继续作为单向兼容 facade。终端尺寸校验、输出环语义、信号处理、PTY 生命周期、
    取消传播和公共 import identity 保持不变；新增负向 import contract，防止生产代码重新依赖 facade。本阶段只改变值对象的 import boundary。
92. 阶段 5AI 完成 canonical 应用配置 owner
    `neuro_code.configuration.app` 的生产 import 收敛。Bootstrap、CLI 的类型契约、TUI 代理策略解析以及 infrastructure Provider factory
    现在直接从该 owner 导入配置契约。原 `neuro_code.config` 模块继续作为单向兼容 facade，并保留历史 `Path.home` patch seam。
    配置加载、Provider/Sandbox 覆盖、代理校验、脱敏和 Runtime 行为保持不变；新增负向 import contract，防止生产代码重新依赖 facade。
93. 阶段 5AJ 完成 canonical 后台任务领域值对象 owner
    `neuro_code.domain.background_tasks.models` 的生产 import 收敛。Application ports/runtime/session、configuration、ACP/TUI、后台管理器、
    infrastructure tools 和 persistence 现在直接导入 models owner；原
    `neuro_code.domain.background_tasks` 模块继续作为单向兼容 facade。唤醒账本校验、任务快照/结果语义、后台执行、取消、持久化和 UI 行为保持不变；
    新增负向 import contract，防止生产代码重新依赖 facade。
94. 阶段 5AK 完成 shared `UiLanguage` owner
    `neuro_code.shared.ui_language` 的剩余生产 import 收敛。domain aggregate 现在直接导入 shared 原语；原
    `neuro_code.domain.ui_preferences` 模块继续作为旧调用方的单向兼容 facade。语言值、UI 偏好持久化、本地化、TUI 行为和公共 identity 保持不变；
    新增负向 import contract，防止生产代码重新依赖 domain facade。
95. 阶段 5AL 在最近一次消费者收敛后审计兼容 facade 隔离。新增明确的 AST 护栏，列举已经迁移的旧路径，并拒绝
    其他生产模块导入这些路径；每个 facade 自身的 re-export 文件和公开 aggregate 入口被排除，因为它们本身就是
    兼容边界。审计确认剩余导入仅属于 facade 自身或兼容测试，未删除任何 facade，也未改变运行时行为。在删除兼容
    路径之前，仍需要版本化的删除决策以及外部调用者证据。
96. 阶段 5AM 完成公开 `neuro_code.tools` 聚合入口中剩余的生产 import 收敛。该入口现在直接从
    `neuro_code.infrastructure.tools.*` 规范 owner 导入公开工具，不再从兼容子模块导入。旧工具模块仍作为
    旧调用方使用的单向 facade 保留；导出对象 identity、registry 懒加载、工具权限、sandbox、取消和输出语义保持不变。
    本阶段不删除任何兼容 facade；版本化删除决策仍需要外部调用方证据。
97. 阶段 5AN 统一会话领域兼容 facade 的 quarantine 清单。
    旧路径 `neuro_code.domain.messages`、`events`、`model_events`、`model_context` 和
    `context_usage` 现在纳入中央显式 facade inventory。生产代码已经直接导入规范的
    `neuro_code.domain.conversation.*` owner；现有 identity 和旧导入测试继续覆盖旧路径调用方。
    本阶段只加强架构护栏，不删除 facade，也不改变消息、事件、上下文、Provider 或 Runtime 行为。
98. 阶段 5AO 为 CLI 建立类型化的会话目录应用接缝。
    `SessionApplicationService` 现在负责有界的 list/search/rename 用例，并返回安全的会话检查投影，
    其中包括持久化执行投影，但不会暴露消息、提示词、工具参数或快照。CLI 仍是入站适配器，
    并保持 plain、JSON 及参数校验行为；SQLite、SessionStore 语义、Runtime 执行、TUI、ACP 和 Provider 行为不变。
    本服务明确不声称提供批量读取或跨行事务原子性；后续切片可以优化投影，但不会把存储所有权移入接口层。
99. 阶段 5AP 将 TUI 的工作区会话目录、搜索和重命名闭包改为通过现有
    `SessionApplicationService` 调用。工作区匹配仍是 bootstrap 的组合策略；存储访问、安全投影、恢复预检和标题修改
    继续隐藏在 application 接缝之后。现有会话绑定、Provider 选择、工作区过滤、错误文本和 TUI 行为保持不变；本阶段不移动 TUI 包、
    不新增 wire 协议，也不优化当前有界的逐条投影读取。
100. 阶段 5AQ 为 `SessionApplicationService` 增加注入式的
    `SessionWorkspaceMatcher` 接缝。`ApplicationComposition` 提供现有文件系统工作区策略，应用服务负责有界的工作区会话列表/搜索投影。
    TUI 消费者不再重复存储查询和过滤机制；ACP 分页与别名语义仍由专用服务维护，因为它们需要游标扫描和外部 ID 协议语义。
    本阶段不新增存储 schema、bulk-read 保证、Provider、Runtime 或用户可见协议变化。
101. 阶段 5AR 为 `SessionStore` 端口及其规范 SQLite 实现增加有序的批量执行记录投影。
     CLI 会话 list/search 现在在一次有界只读快照中读取执行记录，不再为每个结果单独加载一条记录；请求顺序、重复 ID、无记录会话以及非法完成事件错误都保持明确且确定。
     该批量操作只读，不声称与会话事件或执行记录写入具有联合原子性。TUI 工作区目录、ACP 游标/别名语义、Runtime 执行、schema 和用户可见协议保持不变。
102. 阶段 5AS 为 `SessionApplicationService` 增加类型化的键集分页查询，并让 ACP 的安全摘要分页读取通过该接缝完成。
     application 接口负责校验游标字段并只返回 `SessionSummary`；ACP 仍负责游标令牌、扫描上限、工作区过滤、别名分配和 wire 序列化。
     CLI offset/search 投影和 TUI 工作区投影继续使用原有契约。本阶段不新增执行投影、存储 schema、Provider、Runtime 或 ACP wire 字段。
103. 阶段 5AT 审计阶段 5AR 批量执行投影和阶段 5AS application 分页接缝之后的兼容边界。
     规范 `SqliteSessionStore` 实现 `SessionStore` 端口的全部 37 个方法；仓库内其他实现只有通过显式 cast 使用的局部测试替身，
     `neuro_code.adapters.sqlite_session` 仍是保持对象身份的一向兼容 facade。没有外部实现证据时，不新增生产 fallback 或第二套存储协议。
     现有单条读取/分页方法继续保留；删除兼容路径前必须获得外部调用方证据并确定版本化弃用窗口。
104. 阶段 5AU 增加类型化的 `DeleteSessionRequest` 和
     `SessionApplicationService.delete_session()` 用例。ACP 继续负责工作区可见性检查以及别名、活动绑定和协议清理，
     然后只把存储端拥有的删除操作委托到 application 接缝。删除语义、错误、别名、会话生命周期、schema、Runtime、Provider 和 ACP wire 字段保持不变。
105. 阶段 5AV 增加类型化的 `GetSessionSummaryRequest` 和
     `SessionApplicationService.get_session_summary()` 用例。ACP 在 fork/delete 前使用该接缝执行工作区可见性预检，
     而 ACP 别名操作和工作区匹配仍由适配器负责。该摘要读取不加载执行记录或消息；存储 schema、Runtime、Provider、Finalizer 和 ACP wire 行为保持不变。
106. 阶段 5AW 审计 `SessionStore` 的三个 alias 操作。当前唯一生产消费者是 ACP；其 `acp-v1` 命名空间、旧 raw-ID 回退、外部 ID 分配、alias 唯一性和协议错误映射均属于 ACP 专属语义。
     由于没有第二个入站消费者或稳定的跨接口 alias 契约，alias 继续由 `AcpApplicationService` 负责；不新增通用 application alias DTO 或第二套协议。
107. 阶段 5AX 将 `ApplicationComposition.config_for_session_resume()` 的摘要读取改为通过
     `SessionApplicationService.get_session_summary()` 完成。Composition 继续负责 Provider 恢复、工作区匹配、Sandbox 兼容性和 context affinity 选择；application 接缝只提供持久化摘要。
     初始 Sandbox pinning、会话上下文加载、CLI 输出、ACP 行为、schema、Runtime 和 Provider 行为保持不变。
108. 阶段 5AY 增加类型化的 `ExportSessionRequest` 和 `SessionExport` 投影到
     `SessionApplicationService`。application 接缝现在负责显式导出所需的持久化摘要、会话项和事件读取；CLI
     仍负责 Markdown/JSON 渲染、`schema_version=4` 负载、输出路径处理以及现有的显式原始会话/工具数据导出边界。
     Markdown 导出跳过事件读取；JSON 导出显式读取事件。本阶段不新增存储 schema，不改变 Runtime、Provider、Finalizer、ACP、TUI
     或会话导出字段。
109. 阶段 5AZ 增加类型化的 `LoadSessionItemsRequest` 用例到
     `SessionApplicationService`，并让 `AgentConversation` 的 resume 和持久化状态重载通过该接缝完成。
     该接缝只拥有按顺序读取持久化 `SessionItem`；计划、执行记录、工作区和 sandbox 校验仍由各自所有者负责。
     原有读取顺序、上下文内容、取消恢复、SessionStore/SQLite、Runtime、Provider、Finalizer、ACP、CLI 和 TUI 行为保持不变。
110. 阶段 5BA 增加类型化的 `LoadExecutionRecordRequest` 用例到
     `SessionApplicationService`。`inspect_session()` 以及 `AgentConversation` 的 resume/重载路径使用该安全执行投影接缝；
     存储解析器、缺失记录行为、非法终态事件错误、执行记录持久化、Runtime、Provider、Finalizer、ACP、CLI 和 TUI 行为保持不变。
111. 阶段 5BB 为 `SessionApplicationService` 增加类型化的
     `LoadSessionPlanRequest` 用例。`AgentConversation` 的 resume 和持久化状态重载通过该应用层计划读取接缝完成；计划评论、计划修改、任务排队、Provider 来源恢复以及 workspace/sandbox 策略仍由原有所有者负责。
     计划内容、评论读取顺序、SessionStore/SQLite、Runtime、Provider、Finalizer、ACP、CLI 和 TUI 行为保持不变。
112. 阶段 5BC 为 `SessionApplicationService` 增加类型化的
     `ListPlanCommentsRequest` 用例。`AgentConversation` 的 resume/重载和只读评论列表通过该应用层读取接缝完成；评论写入、计划修改、任务排队、Provider 来源恢复以及 workspace/sandbox 策略仍由原有所有者负责。
     计划 fingerprint、评论顺序、SessionStore/SQLite、Runtime、Provider、Finalizer、ACP、CLI 和 TUI 行为保持不变。
113. 阶段 5BD 复用已有类型化的 `GetSessionSummaryRequest` 接缝，覆盖
     `AgentConversation` 的 resume 和 Provider 来源重载。会话层仍负责 workspace/sandbox 校验和来源字段赋值；应用服务只提供持久化摘要。
     本阶段不新增 DTO，不改变 Provider/配置行为、SessionStore/SQLite schema、Runtime、Finalizer、ACP、CLI 或 TUI 行为。
114. 阶段 5BE 为 `SessionApplicationService` 增加类型化的
     `ListSessionTasksRequest` 和 `GetSessionTaskRequest` 读取接缝。
     会话层的任务列表、排队数量检查和排队任务查找现在通过应用接缝完成；任务创建、启动、完成、权限和执行仍由现有会话/运行时生命周期 owner 负责。
     任务顺序、有界限制、状态校验、SessionStore/SQLite、Runtime、Provider、Finalizer、ACP、CLI 和 TUI 行为保持不变。
115. 阶段 5BF 对剩余计划/任务写入进行了审计,没有进行机械迁移。
     计划排队、直接执行计划和执行排队任务已经具备类型化 workflow 门面；底层会话/运行时 owner 仍正确拥有回合锁、计划校验、任务状态转换、权限、事件发布和取消。
     计划评论目前只有一个生产入站消费者（绑定会话的 TUI），并且依赖当前计划和会话锁；在出现第二个消费者或稳定的跨接口契约前，不新增独立的存储写入 application DTO。本阶段不修改生产代码或存储 schema。
116. 阶段 5BG 增加类型化的 `ImportSessionRequest` 和
     `SessionApplicationService.import_session()` 用例。CLI 的 Rust 会话解析与导入报告渲染仍由适配器负责；应用接缝校验规范的
     `SessionSnapshot`，并委托既有的原子 `SessionStore.import_session()` 写入。
     导入统计、JSON/文本输出、SQLite schema、Runtime、Provider、Finalizer、ACP、TUI 和会话行为保持不变。
117. 阶段 5BH 审计剩余入站和持久化侧的 `SessionStore` 消费者。CLI 和 TUI 已没有直接的存储业务调用；ACP alias 操作仍属于 ACP 协议 owner，bootstrap 的 sandbox 预检与存储初始化仍属于组合根，Runtime/Conversation 写入仍属于生命周期 owner。
     对后台唤醒状态、计划评论写入、任务创建和会话创建，没有发现第二个入站消费者或稳定的跨接口契约，因此不引入机械 DTO 或存储 facade。
118. 阶段 5BI 审计 59 个显式隔离的兼容 facade 及仓库内消费者。生产 import 已经收敛到规范 owner；剩余仓库内旧路径引用属于兼容测试、live fixture 或公共 identity 检查。
     当前 checkout 无法证明外部包调用清单或版本化弃用窗口，因此继续保留保持 identity 的单向 facade。
     删除前必须有明确 release 决策、下游调用方证据和迁移测试窗口；本阶段不删除 facade，也不改变 Runtime 行为。
119. 阶段 5BJ 审计被隔离 facade 的公开契约证据。项目当前仍是 pre-alpha（`0.1.0.dev0`），公开入口
     `neuro` 和 `neuro-code` 由 bootstrap 持有；仓库中没有版本化弃用计划、下游包调用清单，或授权删除旧导入路径的 release note。
     compatibility matrix 和本 ADR 将旧路径描述为过渡/兼容边界；import-contract 测试已经固定单向、保持 identity 的 facade
     以及规范生产 import。因此本阶段冻结当前兼容契约并定义删除门槛，不增加 warning、不删除路径、不改写 alias，也不改变 Runtime 行为。
     未来删除阶段必须明确 release 边界、证明下游迁移、同步两套文档，并保留 import/identity 迁移测试窗口。
120. 阶段 5BM 增加会话作用域的工具输出 artifact 应用查询。
     `SessionToolOutputArtifactApplicationService` 先确认会话，再从该会话已持久化的工具终态事件中只派生有界
     artifact 句柄，并通过既有读取端口委托内容读取。损坏元数据会被忽略，未关联句柄会在不泄露跨会话或文件系统
     存在性的前提下被拒绝；不新增原始输出、参数、绝对路径、SQLite 表、Runtime 或事件类型变化。CLI、TUI 和 ACP
     暴露仍属于后续入站切片。
121. 阶段 5BN 将会话作用域 artifact 查询注入 TUI，同时不暴露文件系统基础设施。工具卡片只保留有界的不透明
     artifact ID；用户展开卡片时，通过应用服务和既有读取上限异步读取当前会话已关联的 artifact。缺失或跨会话
     artifact 只显示通用本地化提示。不新增事件类型、SQLite schema，也不改变 Runtime、Provider、权限或会话行为。
122. 阶段 5BO 增加只读 CLI `sessions artifacts SESSION_ID [ARTIFACT_ID]` 查询。列出和读取都通过
     会话作用域 artifact 应用服务完成；CLI 不直接读取状态目录。输出省略文件系统路径和原始 metadata，
     有界读取只提供已脱敏内容。不改变 SQLite schema、事件、Runtime、Provider、TUI 或 ACP 行为。
123. 阶段 5BP 增加显式的 `sessions artifacts --prune` artifact 生命周期维护操作。应用服务先扫描所有
     持久化会话的工具终态事件 metadata,再调用垃圾回收端口；文件适配器只删除规范、未引用且超过一小时
     宽限期的文件,并保留格式异常文件、符号链接、非普通文件、已引用文件和近期文件。引用扫描与文件
     unlink 有意保持为 best-effort 操作,不宣称 SQLite/文件系统跨边界事务。删除会话、fork、导入、导出、
     启动、Runtime、Provider、Finalizer、TUI、ACP、schema 和事件行为保持不变。
124. 阶段 5BQ 增加私有命名空间 ACP 扩展 `_neuro-code/session/artifacts`.
     扩展通过已有 alias 命名空间解析外部 ACP ID,并将有界列出/读取委托给会话作用域 artifact 应用服务.
     响应只包含不透明 ID、有界脱敏内容、字节/事件事实和截断标记,不暴露路径、原始 metadata、参数或 secret.
     该扩展不作为标准 ACP capability 宣告,且不改变 schema、事件、Runtime、Provider、Finalizer、权限、TUI 或 SQLite 行为.
125. 阶段 5BS 复用已有类型化 `GetSessionTaskRequest` 应用接缝，收口 Runtime 对排队计划任务的读取。
     `AgentLoopRunner` 在现有任务启动状态转换前，通过 `SessionApplicationService.get_session_task()` 获取排队任务投影；
     任务创建、启动、完成、事件顺序、取消和持久化所有权仍由 Runtime/会话生命周期 owner 保持。
     服务中仅供类型检查使用的 Runtime 类型导入被隔离，避免新增模块循环依赖。本阶段不改变存储 schema、Provider、Finalizer、ACP、CLI、TUI 或任务状态行为。
126. 阶段 5BT 在 `AgentLoopRunner` 为未提供会话 ID 的回合创建新会话时，复用已有类型化
     `StartSessionRequest` 应用接缝。服务返回规范的 `SessionSummary`，其 ID 用于初始化不变的事件序列和回合记录器路径。
     会话创建仍是 `SessionStore` 适配器提供的原子操作；应用服务不声称它与回合事件、会话项或执行记录处于同一事务。
     任务生命周期、事件顺序、取消、Provider、Finalizer、ACP、CLI、TUI 和 schema 行为保持不变。
127. 阶段 5BU 让会话作用域工具输出 artifact 应用服务复用已有类型化会话摘要和键集分页接缝。
     artifact 关联仍只读取已持久化的工具终态事件投影，清理仍在调用文件系统垃圾回收端口前扫描全部会话。
     本阶段只移除重复的会话存在性/分页转发，不向入站适配器暴露原始事件、路径或 metadata；artifact、事务、Runtime 和 schema 行为保持不变。
128. 阶段 5BV 为 `SessionApplicationService` 增加类型化的会话别名请求，
     并让 `AcpApplicationService` 通过该接缝委托别名绑定、解析和分配。
     ACP 继续拥有外部 ID 校验和 wire 行为，存储仍拥有别名唯一性与持久化冲突语义。
     不改变别名 schema、Runtime、Provider、Finalizer、事件或用户可见行为。
129. 阶段 5BW 在别名接缝之后审计剩余的直接 `SessionStore` 消费者。
     Runtime 事件/会话项/终态写入仍由 Runtime 记录器事务负责；Conversation 的计划评论、任务和唤醒状态写入
     仍由带锁的会话控制器负责；artifact 服务保留 raw 终态事件读取，因为它需要不可信 metadata 投影。
     当前没有第二个生产消费者或稳定跨接口契约足以支持新的 DTO，因此本次审计不新增生产接缝。
130. 阶段 5BX 将 CLI 的纯输出投影下沉到规范的
     `neuro_code.interfaces.cli.serialization` 模块。执行结果、执行记录、有界工具输出 artifact 句柄、会话搜索页和
     Markdown 会话渲染保持原有 wire 结构与脱敏边界；CLI 仍负责命令分发和副作用编排。新的接口模块不依赖存储、Provider、
     Runtime 或基础设施，因此不改变 CLI 行为。
131. 阶段 5BY 将 ACP 的类型化执行结果投影下沉到规范的
     `neuro_code.interfaces.acp.serialization` 模块。合法停止原因和有界执行 metadata 的映射仍只属于协议层；ACP 会话
     生命周期、MCP 转换、工具执行和错误处理仍由现有适配器/应用 owner 负责。该投影保持“类型化结果优先”的行为，且不暴露
     snapshot、digest、工具参数或 provider 内部信息。
132. 阶段 5BZ 将 ACP 的有界文本和 payload 大小原语下沉到规范的
     `neuro_code.interfaces.acp.serialization` 模块。控制字符清理、UTF-8 安全截断、显式值脱敏和规范 JSON 字节大小计算
     仍属于协议安全操作；ACP 会话生命周期、MCP 传输、工具执行和错误处理仍由现有 owner 负责。该拆分保持所有限制和
     wire 行为，不引入第二套 serializer，也不暴露原始输出。
133. 阶段 5CA 将 TUI 终态 metadata 解析下沉到规范的
     `neuro_code.interfaces.tui.execution` 模块。该投影只接受现有可恢复的 `STUCK` 和 `BUDGET_LIMITED` 状态，
     对未知、不可恢复或非终态值 fail-closed。TUI 布局、本地化、事件顺序、会话行为和 Runtime 决策保持不变；
     新模块不访问持久化，也不依赖 Textual。
134. 阶段 5CB 为 `SessionApplicationService` 增加类型化的
     `LoadSessionEventsRequest` 读取接缝。会话导出和会话作用域的工具输出 artifact 应用服务现在通过该接缝消费
     复制且不可变的事件行投影。事件行仍是不可信的存储投影，而不是第二套领域事件模型；事件解码、生命周期写入、
     SQLite 事务、Runtime、Provider、Finalizer、CLI、TUI 和 ACP wire 行为继续由原有边界负责。
135. 阶段 5CC 将 `neuro_code.application.sessions.catalog` 建立为只读会话目录和检查查询的规范 owner。
     `SessionCatalogApplicationService` 负责有界的 list/search/page/workspace 投影和安全的执行记录检查投影。
     现有 `SessionApplicationService` 保留兼容性的应用门面并委托这些读取，CLI 则直接消费目录服务。
     会话生命周期写入、别名、会话项、计划、任务、事件解码、Runtime、Provider、Finalizer、ACP wire 行为和存储事务保持不变。
136. 阶段 5CD 将 `neuro_code.application.sessions.turns` 建立为单个会话回合类型化应用边界的规范 owner。
     `RunTurnRequest`、`SessionTurnRunner` 和 `SessionTurnService` 从宽泛的会话生命周期模块中移出，
     同时通过 `neuro_code.application.sessions` 与 `.service` 的单向兼容 re-export 保持对象身份。
     CLI、TUI、ACP 以及应用层消费者直接导入规范的 turns 模块。运行器仍拥有锁、持久化上下文、事件发送、
     取消和 Runtime 行为；不改变会话 schema、Provider、Finalizer、workflow 或 wire 行为。
137. 阶段 5CE 将共享的 `ProviderOption` 与 `ProviderSelectionResult` 投影类型的规范 owner 建立为
     `neuro_code.application.providers.contracts`。profile 会话控制器仍负责绑定替换与会话选择，Provider
     应用服务、bootstrap 入口和 TUI 则从各自的 Provider 应用接缝消费这些投影。profile 模块、package 导出
     以及旧 runtime facade 保留保持 identity 的兼容 re-export；Provider 生命周期、选择行为、会话回放和 wire
     行为保持不变。
138. 阶段 5CF 将类型化会话绑定契约的规范 owner 建立为
     `neuro_code.application.sessions.binding`。`ConversationBinding` 与 `ConversationRunner` 被 ACP、bootstrap、
     会话应用服务和面向 Runtime 的消费者共享；`ProfileConversationController` 继续拥有 profile 专属的会话选择
     与绑定替换。历史 profile 与 runtime 路径保留保持 identity 的兼容 re-export；回合锁、事件发送、取消、持久化、
     Provider、Finalizer 和 wire 行为保持不变。
139. 阶段 5CG 将不可变的会话选择与交互策略投影的规范 owner 建立为
     `neuro_code.application.sessions.contracts`，包括 `SessionOption`、`SessionSelectionResult`、
     `ReasoningEffortSelectionResult` 和 `InteractionModeSelectionResult`。TUI 直接消费这些投影，
     `ProfileConversationController` 继续拥有选择、策略应用、锁和绑定替换。profile 与旧 runtime 导入保留保持
     identity 的兼容 re-export；会话恢复、Provider 选择、交互模式、推理强度和 wire 行为保持不变。
140. 阶段 5CH 将交互式会话列表、选择和重命名的规范入站接缝建立为
     `neuro_code.application.sessions.selection`。`SessionSelectionService` 是现有
     `ProfileConversationController` 的非拥有型门面；锁、工作区校验、绑定替换、恢复生命周期和执行记录投影仍由
     控制器负责。TUI 通过该门面执行选择操作，同时保留旧控制器注入以兼容当前执行记录投影。
     不改变会话 schema、Runtime、Provider、Finalizer、ACP wire 行为或 TUI 布局。

141. 阶段 5CI 将 Runtime、CLI 和 ACP 应用边界共享的类型化持久化会话生命周期命令的规范 owner
     建立为 `neuro_code.application.sessions.lifecycle`。`StartSessionRequest`、`ImportSessionRequest`、
     `RenameSessionRequest`、`ForkSessionRequest`、`DeleteSessionRequest` 与 `SessionLifecycleService`
     从宽泛的 session service 中移出；原 service 保留保持 identity 的兼容 re-export 并委托这些命令。
     ACP 仍在该服务外负责工作区可见性和活动会话清理；CLI 仍负责解析、渲染和文件 I/O。生命周期服务不获取
     回合锁、不替换 binding、不执行 Runtime、不接触 Provider 或 Finalizer，也不改变会话 schema、事务或 wire 行为。
142. 阶段 5CJ 将 Runtime 与多回合会话控制器共享的类型化只读会话任务查询规范 owner 建立为
     `neuro_code.application.sessions.task_queries`。`ListSessionTasksRequest`、`GetSessionTaskRequest`、
     `SessionTaskQueryController` 与 `SessionTaskQueryService` 从宽泛的 session service 中移出；原 service
     保留保持 identity 的兼容导出并委托读取。任务创建、排队、启动/完成状态转换、权限、执行、锁、取消、
     SessionStore/SQLite 写入、Runtime、Provider、Finalizer 与 wire 行为仍由现有 owner 负责。
143. 阶段 5CK 将会话恢复、bootstrap 配置、ACP 工作区校验和会话作用域工具输出 artifact 读取共享的
     类型化只读会话摘要查询规范 owner 建立为 `neuro_code.application.sessions.summary`。
     `GetSessionSummaryRequest`、`SessionSummaryQueryController` 与 `SessionSummaryQueryService` 从宽泛的
     session service 中移出；原 service 保留保持 identity 的兼容导出并委托查询。不改变会话生命周期写入、
     事件/会话项读取、schema、事务、Runtime、Provider、Finalizer 或 wire 行为。
144. 阶段 5CL 将类型化的只读执行记录投影查询规范 owner 建立为
     `neuro_code.application.sessions.execution_queries`。
     `LoadExecutionRecordRequest`、`LoadExecutionRecordsRequest`、
     `SessionExecutionQueryController` 与 `SessionExecutionQueryService` 由会话目录和会话恢复/重载路径共享；
     宽泛的 session service 保留保持 identity 的兼容导出。单条和批量读取仍委托既有 `SessionStore` 端口。
     不改变执行记录写入、schema、事务、Runtime、Provider、Finalizer、TUI、ACP 或 wire 行为。
145. 阶段 5CM 将复制后的只读会话事件投影规范 owner 建立为
     `neuro_code.application.sessions.event_queries`。
     `LoadSessionEventsRequest`、`SessionEventQueryController` 与 `SessionEventQueryService` 由会话导出和
     会话作用域工具输出 artifact 应用服务共享；宽泛的 session service 保留保持 identity 的兼容导出。
     事件行仍是不可信 mapping，不解码为第二套领域事件模型。事件写入、生命周期事务、Runtime、Provider、
     Finalizer、TUI、ACP 与 wire 行为保持不变。
146. 阶段 5CN 将有序只读会话项投影规范 owner 建立为
     `neuro_code.application.sessions.item_queries`。
     `LoadSessionItemsRequest`、`SessionItemQueryController` 与 `SessionItemQueryService` 由会话恢复/重载和
     显式会话导出共享；宽泛的 session service 保留保持 identity 的兼容导出。会话项写入、计划、评论、事件、
     生命周期事务、Runtime、Provider、Finalizer、TUI、ACP 与 wire 行为保持不变。由于当前没有第二个生产消费者，
     计划与评论读取仍由会话 owner 负责。
147. 阶段 5CO 将现有 application 消费者的导入收敛到规范子模块。
     bootstrap composition 与 TUI 现在从 `application.providers.service` 以及三个
     `application.workflows.*` owner 直接导入；CLI/bootstrap/ACP/会话序列化器从具体的
     session lifecycle、service 和 catalog owner 导入。聚合 package export 仍作为保持
     identity 的兼容路径供外部和旧调用方使用。本阶段仅收敛导入边界，不改变请求类型、
     service 所有权、锁、持久化、Runtime、Provider、Finalizer、TUI 布局、ACP wire 或工作流
     行为。已审计 plan/comment/export 读取；由于没有第二个生产 owner 或稳定跨接口契约，
     不再重复拆分。
148. 阶段 5CP 将所有生产消费者对有界工具输出 artifact 应用 owner 的导入收敛到规范模块。
     CLI、TUI、ACP、bootstrap composition/entrypoints、ACP application 编排和 CLI 序列化器现在直接导入
     `neuro_code.application.tools.service`；package 聚合入口仍保留保持 identity 的兼容导出。artifact 句柄、
     会话可见性校验、脱敏、字节上限、清理、权限、存储、Runtime 与协议行为保持不变。本阶段不暴露文件系统路径，
     也不创建第二套 artifact 模型。

## 影响

- 在目录迁移开始前，目标依赖方向已经可以执行验证。
- 现有债务保持可见，并且可以逐条直接导入地减少。
- 兼容模块在迁移期间保持导入对象身份，代价是暂时增加模块和测试。
- bootstrap 可以包含配置加载器和工厂，但不能拥有跨层共享的配置契约。
- 本 ADR 不决定兼容 re-export 的删除时间；删除需要后续 ADR 或等价的版本化兼容决策。

## 被否决的方案

- 一次性把所有包移动到目标结构：这会掩盖行为回归，且难以安全回滚。
- 静默允许现有顶层包之间的全部导入：这会让迁移开始前的架构债务继续增长。
- 把所有配置类型放入 bootstrap：这会使 application 和 infrastructure 消费者产生反向
  依赖。
