# ADR 0109：规范本地进程沙箱边界

## 状态

已接受。PR 1 建立了规范端口；PR 2 已交付 Linux 子进程范围的 Bash 迁移；PR 3 已交付 Linux 子进程范围的 stdio MCP 迁移；PR 4 已让本地 PTY/ConPTY 创建通过同一端口；PR 5 已删除旧的 controller 范围 Bubblewrap 重执行和命名空间 attestation。

## 背景

Neuro Code 的受信任 controller 承载 AgentRuntime、Provider 和 HTTP 连接、会话持久化、凭据、权限，以及
入站 UI 或 ACP 接口。模型可控的本地命令不应从这些层直接拥有原始操作系统进程原语。

历史上，Bash、受管后台 Bash 和 stdio MCP 分别通过不同路径访问 `ProcessTree`。POSIX PTY 以及 Windows
ConPTY / Job Object 路径同样是具体进程所有者。这使未来的子进程范围平台沙箱难以审计，也容易产生意外绕过。

## 决策

引入 application `LocalProcessSandbox` 端口和 typed `SandboxedProcessRequest`。请求声明产品用途、工作目录、
显式工作区访问、沙箱 profile、所请求的网络和环境策略、stdio 模式以及有界的进程树生命周期。端口返回
`OwnedLocalProcess`，而不是原始 `ProcessTree` 或 subprocess。

`ProcessTreeLocalProcessSandbox` 是面向基于管道的 Bash、后台 Bash、stdio MCP 以及未启用沙箱的本地 PTY/ConPTY
创建的临时基础设施桥接器。它保留既有的 POSIX 进程组和 Windows Job 所有权语义。AST 架构测试守卫直接进程创建：只有
`infrastructure/sandbox/` 可以调用 `ProcessTree.spawn_*`、subprocess 创建、原生 `CreateProcessW`、常见
`os`/`pty`/`multiprocessing` 进程族或更低层终端 `spawn_exec` 适配器。
`interfaces/tui/clipboard.py` 是唯一经过审计的宿主 helper 例外，用于用户请求的桌面剪贴板命令；它不是模型可控的
进程启动器。

受信任 controller 保持在子进程沙箱外。权限审批仍独立于操作系统边界，不能放松请求的 profile 或声明的策略。

AST guard 是针对内置 production code 的仓库契约，不是 Python 运行时安全监控器。sandbox infrastructure
适配器与清单中的剪贴板 helper 属于经审查的可信代码。`additional_tools`、注入的 subagent executor 和未来
任何同进程 Python plugin 都以 controller 权限执行，因此属于可信扩展；恶意扩展可以通过动态 Python、
`ctypes` 或 native code 绕过该端口。若要支持不可信 plugin，必须另建进程/能力边界，不能声称由
`LocalProcessSandbox` 约束。

## 迁移边界

本决策集中所有权。PR 2 现已为基于管道的 Bash 和后台 Bash 提供子进程范围的 Linux 边界：

- `LinuxBubblewrapLocalProcessSandbox` 为每个前台或受管后台 Bash 进程创建一个 Bubblewrap child。它从空根目录开始，
  只挂载执行 child 所需的运行时和显式声明的工作区根目录，绝不挂载 controller 状态目录。
- 每个启用的 child 都拥有私有 `HOME` 和临时目录、`--clearenv` 加小型环境白名单，以及与 profile 匹配的工作区挂载。
  `READ_ONLY` 和 `STRICT` 请求还要求网络命名空间；无法建立或预检该边界时必须 fail closed。
- 该适配器通过各自的管道、协议或 PTY 传输接受 `BASH`、`BACKGROUND_BASH`、`MCP_STDIO`
  和 `INTERACTIVE_TERMINAL` 请求。不支持的 profile/传输组合会被拒绝，不会悄悄在宿主执行。

PR 5 完成了本地进程迁移。controller 不再在 Bubblewrap 命名空间内重执行，也不再使用进程级命名空间 marker 或挂载
attestation。`LinuxBubblewrapLocalProcessSandbox` 负责权威的、失败关闭的预检，并为 Bash、后台 Bash、stdio MCP
以及启用 profile 的 PTY 请求分别创建独立 child 边界。`ProcessTreeLocalProcessSandbox` 桥接器仍是明确的 `off`
profile 实现，不被描述为操作系统隔离。没有具备对应 profile 能力的 child launcher 时，启用的请求会 fail closed。

启用的 Linux child 使用独立 PID 命名空间与 Bubblewrap 父进程死亡生命周期，因此 child 创建的
`setsid()` 后代不能逃离命名空间所有者的终止。POSIX 上的 `off` 只保留原始进程组清理，不提供更强的
任意后代保证。Linux 适配器还会在挂载授权工作区前拒绝存在多个硬链接的 controller 状态文件，防止工作区
inode 别名重新引入 controller 私有数据。

启用 Linux 的生命周期边界由外层 Bubblewrap supervisor 的 pidfd 拥有。`linux_pidfd.py` 在两个包装器都存在时
优先使用 Python 标准库接口。如果某个 Python 发行版没有编译出其中一个包装器，但运行中的 Linux libc 导出了
`pidfd_open` 和 `pidfd_send_signal`，受信任的 sandbox infrastructure 会通过带 close-on-exec 的 native wrapper
调用这些 libc 接口。内核、libc 或架构能力不足时仍然失败关闭；不会把基于 PID 或进程组的信号回退描述为无竞态。
当 libc 只导出 `syscall(2)` 时，wrapper 仅对明确支持的 x86_64、aarch64 和 riscv64 ABI 使用 Linux UAPI 编号；未知
架构仍失败关闭。同一个 pidfd 所有权也传递给 Linux POSIX PTY 适配器，而前台终端中断仍保留既有的进程组语义。

## 影响

每个新增的模型可控本地进程都必须通过 `LocalProcessSandbox`，其用途和生命周期可在一个规范请求中审查。平台适配器
在无法满足请求的 profile、传输或能力时可以 fail closed。远程 ACP terminal 操作仍由 client 委托，不会被误当成
本地 child 进程。

迁移期间刻意保留后台任务管理器的兼容方法。它们现在把旧参数投影为显式规范请求，从而避免调用方重新获得直接
`ProcessTree` 所有权。

专用 CI 门禁与可移植 unit test 明确分离：Linux 必须无 skip 地运行真实 Bubblewrap 文件系统、环境、网络、
硬链接、timeout、cancel、shutdown、grandchild 与 detached-descendant 测试；Windows 必须运行原生 Job
Object 所有权与 ConPTY 生命周期测试。宿主不能建立请求的 namespace 时，安全 job 必须失败，不能把证据缺失
变成绿色结果。
