# ADR 0150：ACP Session Runtime 所有权边界

- Status：Accepted
- Date：2026-08-31
- Scope：叠加在 PR #77 之上的有界 ACP session runtime 切片
- Depends on：ADR 0145、ADR 0146、ADR 0147、ADR 0148 和 ADR 0149

## Context

精确冻结的 PR #77 base 是
`d7dbbc645b15daf987128b7f5264cd29172b1bf8`。在该 base 上，
`neuro_code.acp` 已经抽取 prompt/content、update projection、client I/O 和 MCP
declaration conversion，并把 `ConversationBinding.close()` 建立为 binding cleanup authority。
剩余的 `_AcpSession` dataclass 仍把所有 per-session 可变 interface state、resource reference、
同步、prompt coordination、approval presentation、internal identity 和 aggregate cleanup 混在
connection adapter 中。

审计得到的 per-session 字段与 owner 如下：

| State | 可变性 | 修改前访问方 | 本 ADR 后的 canonical access |
|---|---|---|---|
| 外部 `session_id` | 不可变 | Agent registry 与 protocol response | `AcpSessionRuntime.session_id` |
| binding、MCP tools/name、client terminal | resource reference 可变；name 是不可变 snapshot | 构造、MCP、protocol operation、cleanup | runtime 构造及有界 resource/MCP snapshot |
| approval broker 与 context-window snapshot | broker reference 稳定；context snapshot 不可变 | 构造、prompt/permission coordination | runtime-owned reference 与只读 snapshot |
| internal session ID | 可变但受 identity 约束 | alias binding、prompt、fork/artifact lookup | 同步的 begin/commit/abort identity method |
| prompt task、event mapper、cancel flag | 可变 | prompt、cancel、permission callback、cleanup | task-owner prompt gate 与 cancellation method |
| pending approval ID | 可变 | permission callback 与 prompt finalization | owner-safe approval begin/finish method |
| closing/closed 与 lock | 可变同步状态 | Agent lifecycle 与 cleanup | runtime lifecycle 与 cleanup lock |

Connection 级 client state、capability negotiation、registry、pending reservation、list cursor 和
transport 仍由 Agent 拥有。

## Decision

引入 `neuro_code.interfaces.acp.session.AcpSessionRuntime`，作为唯一的 per-session runtime
owner。它不反向引用 `NeuroCodeAcpAgent`，不持有 application service locator，也不感知
bootstrap、provider、store 或 transport。

Runtime 只提供以下窄操作：

- active-state 与 binding snapshot；
- 一个按 task identity 保证安全的 prompt gate 与 prompt finalization；
- 只取消精确当前 prompt task 的 cancellation；
- 一个 pending ACP approval presentation 与 owner-safe release；
- 同步的两阶段 in-memory internal-session identity transition；
- 有界 MCP reference/name snapshot 与 refresh-name publication；
- 对 prompt task、MCP tools、client terminal 和 binding 的 aggregate cleanup。

`NeuroCodeAcpAgent` 继续拥有 client connection 与 negotiated capability、session registry 与
registry lock、reservation/publication、外层 `new`/`load`/`resume`/`fork` routing、list/delete/close
dispatch、`ext_method` dispatch、live MCP orchestration 和 transport。Application 的
`SessionTurnService`/`ConversationRunner` 继续拥有实际 turn execution、turn lock、durable history
和 recovery；不引入 `PromptController` 或重复 execution state machine。

私有 `_AcpSession` 名称继续作为 canonical runtime 的 identity-preserving alias，供行为测试兼容使用。
它不是第二个 class，也不是 public export。

## Resource ownership

成功 registry publication 之前，Agent 的 local construction path 保留 binding、MCP context 和
client terminal 的 rollback ownership。publication 之后，`AcpSessionRuntime` 是活动 session 的
唯一 cleanup owner，locals 会清空。构造或 publication 失败时，仍只关闭 local path 仍持有的资源。

Runtime 保留既有顺序：先 cancel/wait prompt task，再关闭 MCP tools、shutdown client terminal，最后
调用 `ConversationBinding.close()`。Runtime 永远不调用 `binding.background_tasks.shutdown()`，也不读取
binding resource scope。因此 binding-owned LSP 与 background resource 仍由 PR #77 的 close authority
控制。

## Locking and concurrency

Agent registry lock 只保护 membership 与 reservation/publication。Runtime state lock 保护单个
session 的可变 state。Runtime cleanup lock 串行化 aggregate cleanup。等待 provider/model turn、MCP
operation、terminal shutdown、binding close 或 client permission request 时，不持有 runtime state lock。

Prompt finalization 会比较结束 task 与当前存储的 owner，因此 stale task 不能清除后续 prompt。Cancellation
在 state lock 内捕获同一个当前 task，Agent 释放 lock 后才 cancel。关闭会在 cleanup 前先标记 cancellation，
并阻止新的 interface operation。Approval finalization 只清除自己拥有的 call ID。这些 transition 不声称
已经消除所有已经开始的 outer operation 所持有的 stale reference。

MCP refresh 仍由 Agent orchestration。Runtime 会阻止关闭中的 session 发布新的 MCP name snapshot，
但在 close 之前已经捕获 MCP reference 的 operation 仍可能与底层 refresh/close 边界重叠。该 race 被明确
记录为后续 hardening debt，不做未经证明的结论。

## Permissions and identity

Runtime 可以持有 application-owned `SessionApprovalBroker` 和 pending ACP presentation ID，但不拥有
`PermissionManager`、policy 或 scoped-grant authority。Durable alias write 仍由 Agent 通过 application
service 执行。Runtime 只在 state lock 下 reserve/commit in-memory identity，并拒绝同一个外部 ACP
session 切换到另一 identity。

## Non-goals

本 ADR 不移动或重设计 ACP transport、capability negotiation、`ext_method`、MCP infrastructure、
client I/O adapter、application runner、CLI/TUI/domain/persistence boundary 或 checkpoint/rollback。
不引入 retry、replay、cleanup-error aggregation 或通用 session repository。既有 cleanup error propagation
保持不变：若前一个 resource close 抛错，后续资源的 exhaustive cleanup 仍属于未来 hardening。

## Validation

Focused runtime 与 architecture tests 证明 canonical class identity、没有 Agent back-reference 或
forbidden concrete import、registry typing、prompt/approval ownership、cancellation/task identity、
同步 identity binding、close-state rejection、并发 cleanup 幂等、资源顺序，以及
`ConversationBinding.close()` 继续是 authority。已有 ACP、raw stdio、WebSocket、E2E、client-I/O、MCP、
permission 和 PR #77 resource closure tests 继续纳入验证集合。
