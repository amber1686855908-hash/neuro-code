# ADR 0056 — 有界 ACP 客户端后台终端

**简体中文** · [English](../../en/adr/0056-bounded-acp-client-background-terminals.md)

## 状态

已接受

## 背景

由能力协商控制的 ACP 客户端终端端口最初只暴露前台 `terminal_exec` 调用。标准 ACP 终端方法已经
为直接可执行文件提供了安全生命周期：create、output、wait-for-exit、kill 和 release；但它仍未定义
终端输入、resize、游标读取或 Shell 选择。私有 PTY 扩展会让协议声明失真，也会绕开 SDK 的可移植契约。

## 决策

对于声明 `terminal: true` 且使用 `off` 沙箱 binding 的客户端，绑定到 session 的
`ClientTerminal` 端口现在也持有有界后台直接可执行文件。工具表面为：

- `terminal_start`：接收可执行文件与独立参数；
- `terminal_output`：获取有界状态/输出快照；
- `terminal_wait`：执行有界 `wait_any`/`wait_all`；以及
- `terminal_kill`：终止任务。

接口生成不透明的 Neuro Code task ID，而不会暴露客户端 terminal ID。每个 ACP session 最多接受八个
运行中任务和 32 个保留任务，保留输出最多为客户端提供的上限（绝不超过 1 MiB），也绝不会转发已配置
环境变量或凭据。watcher 会把退出映射为有界快照，超时时请求 kill，并在收集最终可用输出后 release
远程终端。

session close、delete、断连和 shutdown 都会 kill 并 release 每个仍在运行的客户端终端。格式错误或失败的
客户端响应会失败关闭，且不携带原始客户端详情。`terminal_start` 与 `terminal_kill` 仍有副作用，因此
普通本地策略和 ACP 审批路径继续生效。

这是一套独立的客户端终端生命周期：它不改变本地 `bash` 或其受管后台任务，不提供 Shell 命令代理，
也不为客户端任务增加自动完成提醒。

## 后果

支持能力的客户端可以运行并检查直接后台命令，而不会泄露可复用的客户端终端句柄，也不会在 ACP session
结束后遗留工作。文档接口保持诚实：交互式输入、resize、PTY framing/背压和通用终端协议扩展，仍会等到
ACP 将其标准化或另行设计经过协商的扩展后才支持。
