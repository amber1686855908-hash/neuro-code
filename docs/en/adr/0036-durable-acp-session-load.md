# ADR 0036: Durable ACP session load

[简体中文](../../zh-CN/adr/0036-durable-acp-session-load.md) · **English**

- Status: accepted
- Date: 2026-07-19

## Context

ADR 0035 introduced stable connection-local ACP IDs while the internal SQLite
session ID remains lazy. Standard `session/load` needs that client-visible ID to
survive process restarts, and it requires the agent to replay conversation
history before the load request succeeds. The load response does not replace
the requested session ID.

Treating an internal database ID as every ACP ID would break the existing
separation. Keeping the mapping only in memory would make an ID returned by
`session/new` unusable after disconnect. Replaying raw persisted objects would
also expose system prompts, private reasoning, provider-native context, tool
arguments, or unbounded tool output.

## Decision

- Advance the SQLite session schema to v5 and add a namespaced
  `session_aliases` table. One ACP external ID maps to one internal session and
  one internal session has at most one alias in the `acp-v1` namespace. Foreign
  keys remove aliases only when history is explicitly deleted by another
  interface.
- Persist the alias when the runtime emits `SESSION_STARTED`, before model or
  tool work. Keep a shielded post-run/cancellation fallback for runners that do
  not emit that event. `session/close` never removes the alias or history.
- Accept a legacy internal session ID as an initial load reference when no
  alias exists, then bind that value as its durable ACP alias. Do not create a
  second alias for a session that already has one.
- Advertise `loadSession: true` and implement standard `session/load`. The
  requested ID remains the active ACP ID. Load applies the same absolute
  workspace and empty `additionalDirectories` rules as `session/new`. ADR 0038
  subsequently permits bounded ephemeral stdio `mcpServers` on both methods.
- Put resume selection in `ApplicationComposition`: revalidate filesystem
  workspace identity and the fixed sandbox, reconstruct a configured saved
  provider/model when available, and reject unavailable native-context
  affinity rather than silently changing its origin.
- Reconstruct the conversation and background-task scope before publishing the
  active session. Concurrent load/new publication is reservation-based, and
  disconnect cancels both published sessions and in-progress creation.
- Replay only visible user text, assistant text, tool call name/kind/allowlisted
  path, and bounded redacted tool results. System messages, reasoning,
  preserved provider context, image references, `_meta`, raw input/output, and
  arbitrary arguments are omitted.
- Bound replay to 2,000 stored items, 4,096 updates, 64 KiB per visible message
  chunk, 32 KiB per tool result, and 2 MiB serialized updates. Validate the
  complete replay before sending its first update.
- Use fresh UUID message IDs for replayed messages. Tool calls are replayed as
  `pending` followed by `completed`; unresolved historical calls receive a
  terminal `failed` update.

## Consequences

- An ACP ID returned by `session/new` can be loaded by a later `neuro-code acp`
  process without exposing the internal database ID.
- Load remains fail-closed across workspace, sandbox, provider affinity,
  malformed IDs, duplicate active sessions, excessive history, and client
  replay failure.
- Standard SDK clients receive history and can continue the same persisted
  conversation; official-SDK subprocess tests cover close, restart, load,
  replay, continuation, and history retention.
- SQLite v1-v4 databases migrate forward without rewriting session content.
  JSON session export remains schema version 4 because the alias is
  interface-local metadata rather than exported conversation content.
- The ACP surface remains partial. Subsequent slices implement
  workspace-scoped `session/list`, `session/resume`, `session/delete`,
  `session/fork`, bounded and profile-gated additional directories, ephemeral
  stdio/Streamable HTTP/legacy SSE MCP declarations, client filesystem/terminal
  calls, WebSocket transport, and private bounded extensions. Binary multimedia
  history replay, ACP-transport MCP server declarations, persistent MCP
  configuration, interactive client-terminal input/resize/PTY framing, and
  complete conformance remain outside the supported boundary.
