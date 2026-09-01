# ADR 0146: ACP Update and Event Projection Boundary

- Status: Accepted
- Date: 2026-08-30
- Scope: second structural slice of V1 Interface Boundary Consolidation
- Depends on: ADR 0035, ADR 0036, and ADR 0145

## Context

`neuro_code.acp` remains the ACP/JSON-RPC inbound adapter. It owns connection
state, session lifecycle, prompt coordination, client capabilities, permission
request coordination, MCP, and transport handling. Its durable history replay
projection and live `AgentEvent` mapping were nevertheless implemented in the
same large adapter module as those responsibilities.

The update projection is a useful interface boundary: it accepts already
typed domain history or runtime events and emits bounded ACP
`session_update` values. It must not acquire sessions, call providers, execute
tools, or decide authority. The extraction must preserve the frozen ACP wire
contract rather than use the new module as an opportunity to redesign event
semantics.

## Decision

`neuro_code.interfaces.acp.updates` is the canonical owner of the two cohesive
outward projection paths:

- `_history_updates`, which maps a bounded `Sequence[SessionItem]` to ordered
  `HistoryUpdate` values; and
- `_AcpEventMapper`, which maps the explicit runtime `AgentEvent` allowlist to
  client `session_update` notifications.

The module also owns the update-specific tool-kind map, history/update limits,
tool-location presentation, and the small invalid-parameter factory required
by these projections. It imports the ACP schema types, typed domain messages
and events, the typed permission request contract used to build a pending
presentation value, and the existing neutral ACP serialization helpers.

The existing `neuro_code.interfaces.acp.serialization` module is the shared
owner of `_bounded_identifier`. That helper is used both by ACP session error
metadata and by update projection, so it is not duplicated in `updates.py` or
left as an implementation dependency on `neuro_code.acp`.

## Preserved history projection

History replay retains the existing behavior and bounds:

- durable item count, emitted update count, per-field text/content limits, and
  aggregate serialized UTF-8 byte limits remain unchanged;
- user and assistant visible text retains fresh UUID message IDs and order;
- assistant tool calls retain bounded/redacted names, mapped tool kinds,
  bounded/redacted allowlisted locations, and pending start updates;
- tool results retain their matching tool ID, bounded/redacted content, and
  completed progress update;
- an unmatched or still-pending tool is closed with the existing failed
  progress update; and
- non-visible message classes, arbitrary arguments, provider context, raw
  input/output, and `_meta` remain excluded.

The complete projection is still validated before the first replay update is
sent. Redaction, control sanitization, UTF-8 truncation, and serialized-size
accounting continue to use the existing shared helpers.

## Preserved live event projection

`_AcpEventMapper` continues to handle only the existing explicit event kinds:

| Runtime event | ACP projection |
|---|---|
| `SESSION_STARTED` | internal session-binding callback only |
| `TEXT_DELTA` | bounded `agent_message_chunk` with one stable message ID per mapper |
| `TOOL_REQUESTED` | `tool_call` / `pending` with optional bounded location |
| `TOOL_STARTED` | synthesized start when needed, then `tool_call_update` / `in_progress` |
| `TOOL_COMPLETED` | bounded/redacted `tool_call_update` / `completed` |
| `TOOL_FAILED` | bounded/redacted `tool_call_update` / `failed` |
| `CONTEXT_USAGE_UPDATED` | bounded `usage_update` only with valid usage and known window |
| `TURN_COMPLETED` | internal stop-reason projection only |

Unknown events and the existing non-outward event kinds remain ignored. Text
is bounded by both the per-update limit and the per-turn aggregate byte limit;
truncation remains UTF-8 safe. Tool starts precede progress, names and started
IDs are tracked, locations and stop reasons retain their current mapping, and
explicit redactions continue to apply to every outward text field.

No new `AgentEvent` kind, ACP update type, message-ID strategy, or tool-ID
strategy is introduced by this ADR.

## Permission projection semantics

`permission_tool_call` moves only as a presentation helper on
`_AcpEventMapper`. It creates the same bounded pending `ToolCallUpdate` for
the existing `PermissionRequest`. The move does not transfer authority:
`PermissionManager`, `SessionApprovalBroker`, `PermissionDecision`, exact
action matching, workspace/sandbox gates, grant behavior, and fail-closed
approval handling remain owned by their existing application boundaries.
Approval still occurs after the pending presentation update and before tool
execution.

## State ownership

The canonical updates module owns only transient projection state: the stable
answer message ID, sent text byte count, tool-name/start tracking, explicit
redaction values, the bound ACP client/session target supplied by the caller,
and the mapped stop reason.

`neuro_code.acp` continues to own `_AcpSession`, session publication and
cleanup, binding and turn coordination, client capability negotiation, MCP and
transport resources, permission orchestration, and the call sites that invoke
the projections. The extraction does not make the top-level module a facade.

## Dependency direction

The allowed direction for this slice is:

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.updates
        -> neuro_code.interfaces.acp.serialization
        -> application permission contracts / domain conversation types
```

`interfaces.acp.updates` does not import `neuro_code.acp`, bootstrap, concrete
infrastructure, providers, session stores, or workspace implementations. It
performs no resource I/O, global registration, provider call, tool execution,
session lookup, or lifecycle coordination.

## Compatibility and staged strategy

`neuro_code.acp` imports `_history_updates` and `_AcpEventMapper` directly from
the canonical module. These are identity-preserving private compatibility
aliases, not wrappers or duplicate definitions. The existing ACP call sites
therefore retain their private names while tests can assert canonical module
ownership. `_bounded_identifier` likewise remains available through the
legacy module as an identity-preserving import from shared serialization.

The private helpers are not added to a public package barrel or `__all__`.
This slice does not migrate ACP client-capability, agent/server, session, or
transport responsibilities. Each later boundary requires its own audit,
compatibility proof, and behavior-preserving validation.

## Explicit non-goals

This ADR does not change history ordering, pending-tool reconstruction,
redaction, limits, serialized-size accounting, event allowlisting, tool
identity, message identity, stop reasons, permissions, workspace authority,
sandbox behavior, MCP behavior, ACP capabilities, transport behavior, or
provider behavior. It does not add retry, replay, checkpoint/rollback,
parallel execution, dataflow, UI/ACP feature work, or any new orchestration
surface.

## Validation

Validation covers the existing ACP history, live event, raw stdio, and E2E
paths; canonical-definition and private-alias identity checks; dependency and
import contracts; documentation parity; the complete repository quality
gates; and the final pull-request merge-ref CI. The acceptance bar is a
structural projection-boundary extraction with unchanged observable ACP
behavior.
