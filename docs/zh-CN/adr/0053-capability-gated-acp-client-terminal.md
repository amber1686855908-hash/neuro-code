# ADR 0053：由能力协商控制的 ACP 客户端终端执行

**简体中文** · [English](../../en/adr/0053-capability-gated-acp-client-terminal.md)

- 状态：已接受
- 日期：2026-07-29

## 背景

ACP 定义了由客户端持有的 `terminal/create`、`terminal/output`、
`terminal/wait_for_exit`、`terminal/kill` 与 `terminal/release` 请求。既有 `bash`
工具刻意保留本地 Shell 语义、受控进程树、后台任务生命周期和可选的操作系统沙箱强制。若直接把它
替换为客户端调用，Shell 选择会变成平台相关行为，也可能悄悄绕过显式沙箱。

在本 ADR 接受时，标准 ACP 终端表面也比交互式 PTY 更窄：Neuro Code 适配器当时没有可移植的
Shell 选择约定、终端输入、尺寸调整、游标读取或后台任务生命周期。

## 决策

- 增加 canonical 且按 session 作用域工作的 `ClientTerminal` 应用端口，用于一次前台可执行
  文件、参数向量、有界输出和终端退出状态。应用代码绝不导入 ACP SDK 类型。
- 只有已连接客户端明确声明 `terminal: true` 时才绑定该端口，且覆盖新建、加载、恢复和分叉
  session。Bootstrap 经 composition root 传递该端口。
- 只有 `off` 沙箱 binding 拥有该端口时才注册 `terminal_exec`。它接收可执行文件和独立参数，
  不是 Shell 命令。既有 `bash` 行为和受管后台任务继续保持本地且不变；两个工具之间没有透明
  回退。
- 常规有副作用权限、工作区、事件和输出脱敏路径继续生效。不会把已配置的 Neuro Code 环境变量或
  凭据转发给客户端终端。
- 一次执行依次进行 create、wait、output 和 release。输出限制为 1 MiB；畸形或失败的客户端
  响应会变为不含原始详情的稳定失败关闭 `ToolError`。超时或取消时请求 kill，所有已打开终端都会
  尽力 release。
- 此前台切片不提供客户端终端输入、尺寸调整、交互式 framing 或通用 ACP 请求代理。ADR 0056 另行增加
  有界标准后台直接可执行文件生命周期；它并不会增加 Shell 代理或交互式终端语义。

## 影响

- 支持能力且未启用沙箱的 ACP 客户端可以在其自身工作区进程中执行直接前台命令，同时用户仍保留
  常规审批流程。
- 显式沙箱绝不会获得未受约束的替代执行路径：`terminal_exec` 不会暴露，直接调用也会失败关闭。
- 结果刻意小于跨平台 Shell 或交互式终端 API。交互式输入、尺寸调整、PTY framing/backpressure
  以及通用终端协议代理仍不属于本决定，需要独立的能力与生命周期设计。
