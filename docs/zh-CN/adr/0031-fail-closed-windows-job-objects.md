# ADR 0031：使用 Windows Job Object 失败关闭地掌控进程

**简体中文** · [English](../../en/adr/0031-fail-closed-windows-job-objects.md)

- 状态：已接受
- 日期：2026-07-19
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

`CREATE_NEW_PROCESS_GROUP` 能为 Windows 子进程建立控制信号边界，却不能提供稳定掌控全部
后代的句柄。事后通过 `taskkill /T /F` 重新发现进程树依赖可复用的 PID；只等待直接 Shell
入口还可能在后代仍运行时报告完成。这些语义弱于前台和受管后台命令已经使用的 POSIX
进程组边界。

固定 Rust 基线会创建匿名 Job Object，启用
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，把每个已启动入口进程加入 Job，并通过 Job 句柄终止
整棵进程树。Python 切片需要交付相同的用户能力，同时不能让领域层或应用层依赖 Windows
软件包。

## 决策

私有 `windows_job` 平台适配器使用 `ctypes`，并且只在 Windows 上惰性加载
`kernel32.dll`。它在启动子进程前创建并配置匿名、不可继承的 Job Object。`ProcessTree`
会把该句柄借给 [ADR 0033](0033-atomic-windows-job-process-creation.md) 定义的原子扩展
创建边界，使入口进程在任何代码运行前已经属于 Job。只要 `ProcessTree` 仍掌控子进程树，
就会强持有 Job 句柄。

Job 创建、限制配置、原子进程加入、记账查询、终止和句柄关闭都有明确错误路径。创建失败
会关闭已配置的 Job；创建后的流或句柄失败会终止并回收直接子进程，kill-on-close 继续作为
失败关闭兜底。Neuro Code 不会静默退回 `taskkill`，也不会请求
`CREATE_BREAKAWAY_FROM_JOB` 逃离宿主约束。如果宿主 Job 层级拒绝嵌套加入，命令创建会
显式失败。

自然等待先保留直接入口的退出码，再轮询 Job 的 `ActiveProcesses`，直到全部已加入后代
退出。Windows 上的显式终止会立即调用 `TerminateJobObject`；跨平台 grace 参数在该平台
不承诺 POSIX 式温和阶段。终止与关闭均幂等，并发终止会串行化，Job 句柄只关闭一次。
前台 Bash、受管后台任务、绑定替换和应用关闭复用同一个 `ProcessTree` 边界。

可移植的假 API 测试会在所有开发平台覆盖 Win32 权限掩码、限制、记账、原子属性值、受限
继承、错误清理与句柄生命周期。Windows CI 会创建真实后代，验证入口退出后 `wait()` 仍
保持等待，以及终止后代不能越过所属进程树继续存活。

## 影响

- Windows 命令现在会在入口开始执行前通过稳定句柄取得所有权；终止过程不依赖入口 PID
  是否被复用。
- 在受支持的 Windows 版本上，Job Object 可以嵌套在宿主约束内；不兼容的宿主策略会被
  报告，而不会弱化保证。
- 原先的标准库 asyncio `spawn` 到加入窗口已经由
  [ADR 0033](0033-atomic-windows-job-process-creation.md) 消除，并且没有使用 asyncio
  私有 transport、`CREATE_SUSPENDED` 修补序列或 breakaway 标志。
- Windows 原生 ConPTY 生命周期证据由
  [ADR 0032](0032-native-windows-conpty-lifecycle-evidence.md) 独立交付；Job 所有权本身仍不
  代表终端模式已经对齐。

源证据来自固定提交中 `crates/codegen/xai-tty-utils/src/lib.rs` 的 Job Object
`ProcessGroup` 实现，以及 Windows 本地终端的进程所有权路径。
