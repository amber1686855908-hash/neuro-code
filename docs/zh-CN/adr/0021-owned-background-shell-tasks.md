# ADR 0021：在应用生命周期内掌控后台 Shell 任务

**简体中文** · [English](../../en/adr/0021-owned-background-shell-tasks.md)

- 状态：已接受
- 日期：2026-07-18
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

编码智能体需要运行耗时较长的测试、构建、服务器和监控命令，同时不能一直阻塞模型步骤。
仅用 `&` 启动 Shell 后将其遗忘并不能满足要求：输出可能无限增长或无法读取，取消只能到达
Shell 入口进程，CLI 退出后还可能留下后代进程。

固定的 Rust 基线把两类能力分开处理。其 Bash 终端后端会为后台命令返回任务 ID，并提供
快照/等待/终止工具；交互式 PTY 会话则通过 ACP 表面额外提供输入和尺寸调整。当前 Python
运行时可以先完整交付前一种本地工具纵向切片，而无需过早把 M3 进程所有权与未来 M4 ACP
协议耦合。

## 决策

`BackgroundTaskManager` 是会话作用域应用端口，`LocalBackgroundTaskManager` 是第一版
应用监督器/平台适配器。CLI/TUI 组合根创建一个监督器；每个 TUI 会话绑定通过
`ToolContext` 获得隔离的管理器作用域，单次无头运行则使用监督器根作用域。监督器掌控每个
`ProcessTree`、监听任务及有界的 stdout/stderr 合并预览。整个进程最多允许 16 个任务同时
运行，每个作用域最多保留 64 条任务记录；开始新任务前会淘汰更早的已完成记录。

面向模型的契约如下：

- `bash` 接受可选 `is_background`。`false` 保留前台超时行为，`true` 立即返回任务 ID。
- 后台任务省略 `timeout_seconds` 表示没有工具级截止时间；显式正数会在对应时间后终止任务。
- `task_output` 默认返回非阻塞快照，也可以等待最多 30 秒。状态包括 `running`、
  `completed`、`failed`、`timed_out` 和 `cancelled`。
- `wait_tasks` 对请求任务执行有界的事件驱动任意/全部等待，具体由
  [ADR 0024](0024-event-driven-multi-background-task-wait.md) 细化。
- `kill_task` 具有副作用，因此仍经过普通权限/审批策略；对已知且已完成的任务重复调用是
  幂等的。

后台输出不会无限增长。适配器统计实际收到的全部字节，但只在内存中保留配置大小的首尾
预览。创建进程时把 stderr 合并到 stdout，使捕获结果遵循操作系统管道顺序。前台与后台
启动前都会移除供应商及代理凭据；启用沙箱时也使用同一个 `ShellSandbox` 启动计划。

`ProcessTree.wait` 会等待直接子进程，再等待受控 POSIX 进程组或 Windows Job Object。
因此，即使命令内部使用后台运算符并且 Shell 入口先退出，该任务仍由系统掌控。POSIX 上的
显式超时、`kill_task`、启动仍在进行时的取消和管理器关闭会复用已有的有界 TERM→KILL
序列；Windows 则立即终止整个 Job，并以 kill-on-close 作为兜底。无头运行结束与 TUI 退出
一定会调用监督器关闭。切换 TUI provider profile 或会话时，会先验证新绑定，再关闭旧
会话作用域；系统不会有意把任务从所属会话或应用中分离出去。

## 影响

- 模型可以通过可测试能力启动、检查、等待和终止长时间命令，不需要 sleep 轮询或不受控
  Shell 作业。
- 任务记录与输出预览只存在于当前进程，不会写入 SQLite，也不能跨应用重启或会话恢复。
- 单次无头运行可在工具循环中使用后台任务，但返回时会终止仍在运行的任务；TUI 任务可以
  在同一绑定中跨轮次存活，直到自然完成、被终止、绑定切换或 TUI 退出。
- TUI 元数据可见性和本地完成通知由
  [ADR 0022](0022-session-scoped-background-task-visibility.md) 进一步规定，明确模型边界的
  完成元数据则由
  [ADR 0023](0023-model-visible-background-task-completion-reminders.md) 定义。完整输出文件、
  模型自动唤醒、前台自动转后台，以及与子代理共享任务命名空间仍是后续切片。
- ACP PTY 的创建/输入/尺寸调整/环形缓冲/关闭仍是独立能力；后续会复用进程所有权边界，
  而不是改变当前工具契约。
- Windows Job Object 的所有权与失败行为由
  [ADR 0031](0031-fail-closed-windows-job-objects.md) 规定；创建时原子加入 Job 和受限
  标准句柄继承由 [ADR 0033](0033-atomic-windows-job-process-creation.md) 规定。

源证据来自固定提交中的历史 Bash、任务输出、任务终止、本地终端及后台任务用户指南行为。
