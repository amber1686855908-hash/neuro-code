# ADR 0151：ACP Transport 边界

- 状态：Accepted
- 日期：2026-08-31
- 范围：堆叠在 PR #78 之上的 ACP transport 结构切片
- 依赖：ADR 0145、ADR 0146、ADR 0147、ADR 0148、ADR 0149 和 ADR 0150

## Context

冻结的 PR #78 base 是
`99af1d1e9339b3baaa657b1a946279a7ecffff61`。在该 base 上，
`neuro_code.acp` 已经提取 prompt/content conversion、history/live update
projection、client filesystem/terminal adaptation、MCP declaration
conversion、binding resource closure 和 per-session runtime ownership，
但仍把 protocol-agent semantics 与 ACP SDK connection、stdio
startup/framing 以及 WebSocket transport loop 混在一起。

本切片只提取该 transport boundary。它保留 public
`neuro_code.acp.serve_acp` 与 `serve_acp_websocket` entrypoint、既有 private
compatibility name、SDK router workaround、全部 framing 和 size bounds，以及
既有 close、cancellation 和 Agent shutdown 行为。不重设计 client port、
capability negotiation、session model、background task semantics 或任何其他
ACP boundary。

## 提取前 transport audit

审计针对 PR #78 exact head 完成，并在移动代码前结束。

### Transport 专属 symbol

`neuro_code.acp` 中属于 transport 的 symbol 是：

- `ACP_STDIO_BUFFER_LIMIT_BYTES`；
- `_build_acp_router`，包括 SDK 0.11 stable `session/delete` route workaround；
- `_AcpSdkConnection`；
- `_WebSocketWriter`；
- `serve_acp` 的 stdio stream/connection loop；以及
- `serve_acp_websocket` 的 server、feeder、newline framing 和 per-connection
  close loop。

client filesystem 与 terminal adapter 不属于本次审计：它们已经依据 ADR 0147
由 `neuro_code.interfaces.acp.client_io` 拥有。Capability negotiation 仍保留在
`NeuroCodeAcpAgent`。

### State 与 lifecycle

SDK connection adapter 只拥有 SDK `Connection` instance 和注入的 Agent attachment。
WebSocket writer 拥有 pending byte buffer 与 closed flag。每个 WebSocket handler
拥有一个有界 `StreamReader` 和一个 feeder task。Feeder 将 text 转成 UTF-8，接受
bytes，在需要时追加 newline，并在 finalizer 中以 EOF 关闭 reader。

Transport 不拥有 retained background terminal task、pending terminal start、terminal
watcher、task completion state、output state、timeout、kill/release policy、session
shutdown state、session lock、capability snapshot 或 permission state。这些仍由既有
client-I/O、session-runtime、application 或 Agent owner 负责。

### Agent call site 与 capability gate

`serve_acp_websocket` 是唯一的 WebSocket bootstrap call site。它接收 application
service，为每个 accepted connection 构造一个 `NeuroCodeAcpAgent`，并将其传给
connection loop。`serve_acp` 为 stdio process 构造一个 `NeuroCodeAcpAgent`，并将其
传给 stdio loop。

Transport 本身没有 capability check。Agent 仍在 `initialize` 中协商 capability，
其 `_client_file_system` 与 `_client_terminal` property 仍决定是否创建已经 canonical
的 client-I/O adapter。没有 capability decision 被移动到 transport module。

### Dependency direction 与冻结行为

提取前，顶层 ACP adapter 直接导入 SDK router、connection、schema、stdio stream 和
normalization helper。提取后的目标方向是：

```text
neuro_code.acp public wrapper
        -> neuro_code.interfaces.acp.transport
        -> ACP SDK connection/router/schema/stdio primitive
        -> 注入的 ACP Agent protocol
```

Canonical transport module 不得导入 `neuro_code.acp`、bootstrap、infrastructure、
providers、stores 或 application composition。它不得从 service 构造 Agent、检查
session registry、作 permission decision、校验 workspace、配置 sandbox、注册 tool
或调用 Provider。

既有 behavior 已由 `tests/test_acp.py`、`tests/test_acp_raw_stdio.py` 和
`tests/test_acp_e2e.py` 固定，包括 router dispatch、private alias 使用、stdio setup
与 shutdown、WebSocket dependency failure、host/port validation、text/binary conversion、
newline framing、message bounds、writer batching、feeder cancellation 和 per-connection
cleanup。

## Decision

`neuro_code.interfaces.acp.transport` 是以下内容的 canonical owner：

- SDK router extension 与 `_AcpSdkConnection`；
- WebSocket writer bridge；
- 官方 stdio stream entrypoint；
- 有界 WebSocket server/reader feeder；
- transport-local 1 MiB buffer bound；以及
- 外层 connection close 与注入式 Agent shutdown lifecycle。

Canonical API 接收 stdio 的已构造 Agent，以及 WebSocket 的 Agent factory。可选的
connection、stream 和 writer factory 是窄的 test/compatibility seam；它们不引入第二套
实现，也不改变 ownership。

`neuro_code.acp` 只保留这些 entrypoint 的 service-to-Agent public wrapper，以及移动
symbol 的保持 identity 的 private import。`NeuroCodeAcpAgent` 仍拥有 protocol semantics、
capability negotiation、connection attachment、session registry/publication、extension
dispatch、live MCP orchestration 和 application-facing lifecycle routing。

## Router 与 SDK connection

Canonical router 继续调用官方 SDK
`build_agent_router(agent, use_unstable_protocol=True)`。它只使用既有的
`AGENT_METHODS`、`MessageRouter` 和 `normalize_result` 行为，补上 SDK 0.11 Agent router
遗漏的 generated stable `DeleteSessionRequest` route。不引入自制 JSON-RPC dispatcher。

`_AcpSdkConnection` 继续使用 canonical router 创建 SDK `Connection`，传入
`listening=False`，然后使用 connection client surface 调用注入 Agent 的 `on_connect`。
它的 `listen`、`close`、`session_update` 和 `request_permission` 方法继续保留既有的
SDK notification/request schema 与 normalization。

## STDIO boundary

`serve_stdio` 继续从官方 SDK `stdio_streams` 获取 stream，并传入
`limit=ACP_STDIO_BUFFER_LIMIT_BYTES`，创建一个 SDK connection，然后等待其 main loop。
它使用 `asyncio.shield` 关闭 connection，再使用 `asyncio.shield` shutdown 注入 Agent，
保留既有 exception propagation 与 cleanup order。即使 stream setup 失败，也会 shutdown
Agent。旧 public wrapper 构造 Agent，并注入历史的 `neuro_code.acp.stdio_streams` alias，
因此既有 private test patching 继续有效。

## WebSocket boundary

`serve_websocket` 保留 host 与 port validation；缺少可选 `websockets` dependency 时，
仍以既有 `ConfigurationError` 失败关闭。它使用 1 MiB maximum message size 和
`max_queue=16` 配置官方 server。

每个 accepted connection 都得到一个 fresh Agent、相同 1 MiB limit 的 `StreamReader`、
`_WebSocketWriter` 和 SDK connection。Text frame 编码为 UTF-8，bytes 原样转发；不支持的
frame value、空消息和超限消息使用既有 connection error 失败关闭。缺少 trailing newline
时，在数据进入 SDK reader 前追加。Feeder 会先 cancel 并 join，再关闭 connection，随后
恰好 shutdown Agent 一次。Writer 继续保留既有批量 `write`/`drain`、closed-state 和
no-op `wait_closed` behavior。

Interactive stdin、resize、PTY framing、cursor streaming 和通用 WebSocket transport
framework 仍不在范围内。

## Compatibility 与 architecture contract

`neuro_code.acp` 从 canonical module 导入 `ACP_STDIO_BUFFER_LIMIT_BYTES`、
`_build_acp_router`、`_AcpSdkConnection`、`_WebSocketWriter` 和 `stdio_streams`。移动后的
private class 与 helper 的 `__module__` 报告 canonical module，而旧 import 保留 object
identity。Public entrypoint signature 仍基于 service。

Architecture test 断言 transport definition 只存在于 canonical module，legacy name 是
保持 identity 的 alias，canonical module 没有 reverse/concrete application dependency、
Agent construction 或 session/permission state，并且 Agent 继续拥有 capability 与 session。
既有 ACP test 断言 observable protocol 与 cleanup behavior。

## 明确的非目标

本 ADR 不改变 client filesystem/terminal adapter、capability negotiation、Agent protocol
semantics、session lifecycle 或 runtime ownership、MCP、permissions、workspace 或 sandbox
policy、provider behavior、background terminal task、retry、replay、checkpoint/rollback、
automatic delegation、writable subagent、parallel/dataflow execution、UI behavior 或 ACP
application service。

## 状态与验证

本 ADR 已标记为 Accepted。Stacked pull request 的 merge-ref CI 已全绿，Local focused
test 与 repository quality gate 也已通过。该 Accepted 状态只适用于本 bounded transport
slice，不扩大 ACP consolidation scope。
