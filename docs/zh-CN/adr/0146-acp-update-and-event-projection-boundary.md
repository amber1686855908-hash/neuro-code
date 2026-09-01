# ADR 0146：ACP Update 与 Event Projection 边界

- 状态：Accepted
- 日期：2026-08-30
- 范围：V1 Interface Boundary Consolidation 的第二个结构切片
- 依赖：ADR 0035、ADR 0036 和 ADR 0145

## Context

`neuro_code.acp` 仍然是 ACP/JSON-RPC 入站适配器，拥有 connection state、session
lifecycle、prompt coordination、client capability、permission request coordination、MCP
和 transport handling。可是它的持久 history replay projection 与实时 `AgentEvent` mapping
仍和这些职责一起实现于同一个大型 adapter module 中。

Update projection 是一个清晰的 interface boundary：它接收已经类型化的 domain history 或
runtime event，并输出有界 ACP `session_update` value。它不能获取 session、调用 provider、
执行工具或决定 authority。本次提取必须保留冻结的 ACP wire contract，不能借新模块之机重设计
event semantics。

## Decision

`neuro_code.interfaces.acp.updates` 是以下两个 cohesive outward projection path 的
canonical owner：

- `_history_updates`，将有界 `Sequence[SessionItem]` 映射为有序 `HistoryUpdate` values；
- `_AcpEventMapper`，将显式的 runtime `AgentEvent` allowlist 映射为 client
  `session_update` notifications。

该 module 同时拥有 update-specific tool-kind map、history/update limits、tool-location
presentation，以及这些 projection 所需的小型 invalid-parameter factory。它导入 ACP schema
types、类型化 domain message/event、用于构造 pending presentation value 的类型化 permission
request contract，以及已有的中立 ACP serialization helpers。

现有 `neuro_code.interfaces.acp.serialization` module 是 `_bounded_identifier` 的 shared
owner。该 helper 同时用于 ACP session error metadata 和 update projection，因此不会在
`updates.py` 中重复，也不会保留对 `neuro_code.acp` implementation 的依赖。

## 保留的 history projection

History replay 保持现有行为与 bounds：

- durable item count、emitted update count、per-field text/content limit 和 aggregate serialized
  UTF-8 byte limit 均不变；
- user 与 assistant 可见文本继续使用 fresh UUID message ID，并保留顺序；
- assistant tool call 继续保留有界/脱敏名称、mapped tool kind、有界/脱敏 allowlisted location
  和 pending start update；
- tool result 继续保留匹配的 tool ID、有界/脱敏 content 与 completed progress update；
- 未匹配或仍 pending 的 tool 继续以既有 failed progress update 关闭；
- 不可见 message class、任意 arguments、provider context、raw input/output 和 `_meta` 继续被
  排除。

完整 projection 仍会在发送第一条 replay update 之前完成校验。Redaction、control sanitization、
UTF-8 truncation 和 serialized-size accounting 继续使用既有 shared helpers。

## 保留的 live event projection

`_AcpEventMapper` 继续只处理已有的显式 event kind：

| Runtime event | ACP projection |
|---|---|
| `SESSION_STARTED` | 仅执行内部 session-binding callback |
| `TEXT_DELTA` | 每个 mapper 使用一个稳定 message ID 的有界 `agent_message_chunk` |
| `TOOL_REQUESTED` | 带可选有界 location 的 `tool_call` / `pending` |
| `TOOL_STARTED` | 必要时合成 start，然后发送 `tool_call_update` / `in_progress` |
| `TOOL_COMPLETED` | 有界/脱敏的 `tool_call_update` / `completed` |
| `TOOL_FAILED` | 有界/脱敏的 `tool_call_update` / `failed` |
| `CONTEXT_USAGE_UPDATED` | 仅在 usage 有效且窗口已知时发送有界 `usage_update` |
| `TURN_COMPLETED` | 仅执行内部 stop-reason projection |

未知 event 与现有非 outward event kind 继续忽略。文本同时受 per-update limit 与 per-turn
aggregate byte limit 约束；truncation 继续保证 UTF-8 安全。Tool start 先于 progress，name
与 started ID tracking、location、stop reason mapping 保持现状，所有 outward text field 继续
应用 explicit redaction。

本 ADR 不引入新的 `AgentEvent` kind、ACP update type、message-ID strategy 或 tool-ID strategy。

## Permission projection semantics

`permission_tool_call` 只作为 `_AcpEventMapper` 上的 presentation helper 移动。它继续为既有
`PermissionRequest` 创建相同的有界 pending `ToolCallUpdate`。这次移动不转移 authority：
`PermissionManager`、`SessionApprovalBroker`、`PermissionDecision`、exact action matching、
workspace/sandbox gates、grant behavior 和 fail-closed approval handling 仍由原有 application
boundary 拥有。Approval 仍然发生在 pending presentation update 之后、tool execution 之前。

## State ownership

Canonical updates module 只拥有 transient projection state：stable answer message ID、sent text
byte count、tool-name/start tracking、explicit redaction values、由 caller 提供的 bound ACP
client/session target，以及 mapped stop reason。

`neuro_code.acp` 继续拥有 `_AcpSession`、session publication 与 cleanup、binding 与 turn
coordination、client capability negotiation、MCP 与 transport resources、permission
orchestration，以及调用这些 projection 的 call sites。本次提取不会把顶层 module 变成 facade。

## Dependency direction

本切片允许的方向是：

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.updates
        -> neuro_code.interfaces.acp.serialization
        -> application permission contracts / domain conversation types
```

`interfaces.acp.updates` 不导入 `neuro_code.acp`、bootstrap、具体 infrastructure、providers、
session stores 或 workspace implementations。它不执行 resource I/O、global registration、
provider call、tool execution、session lookup 或 lifecycle coordination。

## Compatibility 与分阶段策略

`neuro_code.acp` 直接从 canonical module 导入 `_history_updates` 与 `_AcpEventMapper`。它们是
保持 identity 的私有 compatibility aliases，不是 wrapper 或重复定义。因此既有 ACP call site
仍可使用原有私有名称，同时 test 可以断言 canonical module ownership。`_bounded_identifier` 同样
通过从 shared serialization 导入，继续从 legacy module 以相同 identity 提供。

这些 private helper 不加入 public package barrel 或 `__all__`。本切片不迁移 ACP
client-capability、agent/server、session 或 transport responsibilities。后续每个 boundary 都
必须单独完成 audit、compatibility proof 和 behavior-preserving validation。

## 明确的非目标

本 ADR 不改变 history ordering、pending-tool reconstruction、redaction、limits、serialized-size
accounting、event allowlisting、tool identity、message identity、stop reasons、permissions、
workspace authority、sandbox behavior、MCP behavior、ACP capabilities、transport behavior 或
provider behavior。它不增加 retry、replay、checkpoint/rollback、parallel execution、dataflow、
UI/ACP feature work 或任何新的 orchestration surface。

## Validation

验证覆盖既有 ACP history、live event、raw stdio 与 E2E path；canonical-definition 与 private-alias
identity checks；dependency 与 import contracts；documentation parity；完整 repository quality
gates；以及最终 pull-request merge-ref CI。验收标准是结构性的 projection-boundary 提取，同时
保持可观察 ACP 行为不变。
