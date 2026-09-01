# ADR 0147：ACP Client I/O Adapter 边界

- 状态：Accepted
- 日期：2026-08-30
- 范围：V1 Interface Boundary Consolidation 的第三个结构切片
- 依赖：ADR 0052、ADR 0053、ADR 0056、ADR 0145 和 ADR 0146

## Context

冻结的 PR #74 HEAD 是
`18a686222190f5251e269bb68e1ebfeb7744cede`。顶层的
`neuro_code.acp` adapter 仍然包含多个不相关职责。本轮下一个 cohesive boundary 是
ACP client-side filesystem 与 terminal adaptation，它实现既有的 application port
`ClientFileSystem` 和 `ClientTerminal`。

这是一次结构提取，必须保留现有 ACP wire behavior、能力门控、session binding、私有兼容
名称、bounds、取消行为、后台任务 ownership 和 cleanup。它不重设计 application port，
也不重设计 ACP capability negotiation。

## 提取前审计

审计针对 PR #74 exact head 完成，并在移动代码前结束。

### Filesystem adaptation

filesystem 专属 symbol 是 `_AcpClientFileSystem`。它拥有 ACP SDK `Client`、绑定的外部
ACP `session_id`，以及两个经过协商的布尔值：`supports_read` 与 `supports_write`。

`read_text_file` 在没有 read capability 时先失败关闭，然后把绑定的 session ID、path、可选
line 和可选 limit 转发到 `fs/read_text_file`，将 UTF-8 response 限制为 1 MiB，传播
`CancelledError`，并把其他 client failure 转成既有稳定的 `ToolError`。`write_text_file`
在调用 `fs/write_text_file` 之前使用相同的 1 MiB UTF-8 bound，保留 cancellation 与
`ToolError`，并将其他 failure 转成既有稳定错误。

### Terminal adaptation

terminal 专属 symbols 是：

- `_AcpClientTerminalTask`；
- `_AcpClientTerminal`；
- `_client_terminal_command`；
- `_client_terminal_cwd`；
- `_client_terminal_limits`；
- `_client_terminal_background_limits`；
- `_client_terminal_wait_seconds`；
- `_client_terminal_id`；
- `_client_terminal_task_id`；
- `_client_terminal_exit_status`。

adapter 专属 constants 是 `MAX_CLIENT_FILE_BYTES`、
`MAX_CLIENT_TERMINAL_COMMAND_BYTES`、`MAX_CLIENT_TERMINAL_ARGUMENTS`、
`MAX_CLIENT_TERMINAL_ARGUMENT_BYTES`、`MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES`、
`MAX_CLIENT_TERMINAL_ID_BYTES`、`MAX_CLIENT_TERMINAL_SIGNAL_BYTES`、
`MAX_CLIENT_TERMINAL_TASKS` 和 `MAX_CLIENT_TERMINAL_RETAINED_TASKS`。
共享的 1 MiB terminal output bound 仍由既有 application terminal port 拥有；
`MAX_BACKGROUND_TASK_WAIT_IDS` 及 background task result/status values 仍由 domain 拥有。

terminal adapter 拥有以下 state 和 lifecycle：

- ACP `Client` 与绑定的 session ID；
- retained task map 及其 async lock；
- 用于 running-task bound 的 pending-start counter；
- closed/shutdown flag；
- 每个 task 的 opaque task ID、ACP terminal ID、command、cwd、output limit、timeout、
  status、output、最大观测 output bytes、truncation flag、exit status、finish time、kill
  与 timeout/failure flags；
- 每个 task 的 completion event、output lock、termination lock 和 watcher；
- cancellation-safe terminal creation 与 cleanup；
- foreground wait、timeout/cancel kill、output retrieval 与 release；
- background watcher、timeout、kill、release、retention 与 shutdown behavior。

### Agent call sites 与 capability gates

`NeuroCodeAcpAgent._client_file_system` 继续拥有 capability decision。当没有 client、没有
协商到 filesystem capability，或 `read_text_file is True` 与 `write_text_file is True` 都不
成立时，它返回 no adapter；否则使用两个布尔值构造 adapter。`_client_terminal` 继续只有
在 client 已连接、capabilities 已协商且 `terminal is True` 时构造 adapter。

`new_session`、`_activate_persisted_session` 和 `fork_session` 构造这些绑定到 session 的
adapter，并且只有在成功 publish 后才把它们转交给 service binding。失败路径仍会 shutdown
未转移的 terminal。`_AcpSession` 与 `_cleanup_session` 继续拥有 terminal reference 和
session cleanup。`initialize` 中的 capability negotiation 仍在 `NeuroCodeAcpAgent`。

### Dependency 与 behavior audit

提取前，`neuro_code.acp` 直接包含这些 adapter，并导入 ACP SDK client/schema、application
ports、domain background-task types 与 `ToolError`。本 cohesive interface-layer dependency
direction 为：

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.client_io
        -> ACP SDK client/schema
        -> application ClientFileSystem / ClientTerminal ports
        -> domain background-task types
```

canonical module 不得导入 `neuro_code.acp`、bootstrap、具体 infrastructure、providers、stores
或 workspace implementations。它不执行 capability negotiation、session lookup、permission
decision、workspace validation、sandbox setup、tool registration 或 provider call。

既有 behavior 已由 `tests/test_acp.py` 固定，包括 filesystem capability combinations、
forwarded session/path/range values、1 MiB file bounds、terminal capability gating、foreground
create/wait/output/release、invalid response handling、no-environment forwarding、background
start/get/wait/kill、timeout/cancellation、retention 和 shutdown。下游 port consumers 仍由
`tests/test_tools.py` 覆盖。

## Decision

`neuro_code.interfaces.acp.client_io` 是 ACP client filesystem 与 terminal adapters、
adapter 专属 bounds 和 validation helpers 的 canonical owner。实现按结构移动，不改变 method
signatures 或 control flow。

`neuro_code.acp` 继续负责 capability negotiation、capability-gated construction、session
binding/publication、lifecycle ownership 和 cleanup。application ports 仍是 tools 消费的
seam；ACP SDK type 不会传入 application tool code。

## 保留的 filesystem behavior

canonical filesystem adapter 继续：

- 把每个 request 绑定到构造时提供的 ACP session；
- 只有对应协商 capability 为 true 时才暴露 read/write；
- 保留既有 path、line 和 limit forwarding；
- 执行既有 1 MiB UTF-8 response/write bounds；
- 保留 cancellation propagation 与稳定的 fail-closed errors；
- 将最终 client-side filesystem semantics 留给 client。

## 保留的 terminal behavior

canonical terminal adapter 继续：

- 接受 direct executable 和有界 argument vector，而不是 shell command；
- 使用既有 messages 与 bounds 校验 command、arguments、cwd、output、timeout、IDs 和 exit
  status；
- 对每个 foreground terminal 执行 create、wait、read 和 release；
- foreground timeout、cancellation、wait/output failure 和 cleanup 时执行 kill；
- 不转发任何 Neuro Code 配置环境值；
- 最多保留 8 个 running 与 32 个 retained background tasks；
- 保留 opaque task IDs、ordered wait results、missing IDs、timeout semantics、idempotent
  kill、output accounting 和 status transitions；
- 在 task/session shutdown 期间等待 owned watchers 并释放 terminals。

Interactive stdin、resize、cursor streaming、PTY framing 和 backpressure 仍不支持。

## State ownership 与 lifecycle

`client_io` 只拥有审计中列出的 adapter-local state。它不拥有 `_AcpSession`、session
registry、binding publication、capability snapshot、permission broker、workspace、sandbox
或 transport。terminal task watcher 仍然拥有 background completion state；session cleanup
继续调用 adapter 的幂等 shutdown。

## Compatibility

`neuro_code.acp` 直接从 `client_io` 导入移动后的 private classes、helpers 和 adapter 专属
constants。这些是保持 identity 的 private compatibility aliases，不是 wrappers 或重复定义。
因此既有 private test 与 integration references 仍保持 behavior，同时 classes/helpers 的
`__module__` 报告 canonical module。

此前从 `neuro_code.acp` 导入的 shared `ClientTerminalResult` 与 terminal output bound 仍作为
从 application port 导入的 compatibility imports 保留；canonical adapter 不重新定义它们。

## Authority、permission 与 sandbox boundary

本次提取不改变 permission authority、workspace validation、sandbox policy 或 tool
registration。普通 side-effect permissions 仍 gate terminal starts 与 kills；enabled sandbox
仍通过既有 capability construction gate 阻止 client terminal 暴露。

`permission authority non-migration = PROVEN within this slice`：这只表示提取后的 module
没有取得 permission authority，不表示完整 Neuro Code permission subsystem 已由本 ADR 全局重新
证明。

## 明确的非目标

本切片不改变 client ports、ACP capability negotiation、agent/server adaptation、session
lifecycle、permissions、workspace/sandbox policy、MCP、transport、provider behavior、task
semantics、output bounds、retry、replay、checkpoint/rollback、automatic delegation、writable
subagents、parallel/dataflow execution、UI behavior 或 interactive terminal features。

## Validation

验证包括 canonical-definition 与 alias identity contracts、dependency/import contracts、既有
ACP unit/raw-stdio/E2E behavior、documentation parity、完整 repository quality gates，以及最终
pull-request merge-ref CI。只有新的 merge-ref CI 全绿才接受；不能只凭本地测试把该结构边界标记为
frozen。
