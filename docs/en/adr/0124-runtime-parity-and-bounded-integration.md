# ADR 0124 — Runtime parity and bounded integration

[简体中文](../../zh-CN/adr/0124-runtime-parity-and-bounded-integration.md) · **English**

## Status

Accepted for the current pre-alpha runtime.

## Context

The existing design deliberately landed narrow vertical slices: a safe tool
pipeline, session-owned MCP tools, partial ACP stdio, provider-neutral
compaction, and explicit read-only subagents. That boundary made the system
safe, but left several integration gaps visible to clients: model requests had
no compact reconstruction evidence, independent read-only tool calls were
always serialized, MCP capability lists were tool-only, provider failures had
no bounded retry/circuit policy, and user permission rules were not durable.

The implementation must preserve the existing ownership rules. A scheduler
must not execute tools outside the permission/workspace/sandbox pipeline; MCP
must remain session-owned; untrusted protocol content must stay bounded and
redacted; and provider retries must never repeat a request after observable
model output.

## Decision

The following bounded integrations are production capabilities:

- Every model step emits a non-secret request snapshot containing context/tool
  fingerprints and shape counts. The full reconstruction payload remains
  in-memory only.
- Read-only tool calls may run in bounded parallel groups. Side-effecting and
  interaction-control tools are exclusive. Canonical terminal results are
  emitted for completed, failed, rejected, cancelled, and not-started calls;
  pipeline hooks observe the same redacted boundary.
- ACP-owned MCP sessions enumerate resources, resource templates, and prompts;
  they support bounded reads, refresh, sampling, and elicitation through a
  private namespaced extension. Tool names are replaced atomically on refresh.
- ACP accepts bounded audio and embedded binary prompt content. It preserves
  the content in the model context, while providers without native media
  support receive safe placeholders. A WebSocket newline-JSON bridge reuses
  the stdio ACP router and the same session, permission, and workspace gates.
- Explicit subagent scheduling is available behind a scope-aware application
  service. It bounds parallelism, retries, timeouts, depth, recursive spawn,
  tool names, and write capability, and closes every fresh child runtime.
- Provider adapters are wrapped with pre-output retry and cooldown circuit
  protection. TUI model discovery uses a bounded atomic cache that stores only
  model identifiers and falls back to recent data for classified network
  failures; credentials are never persisted.
- Permission rules support path/operation matching and a bounded atomic JSON
  store. Loading remains explicit at the CLI/composition boundary; local deny,
  workspace, sandbox, approval, and redaction checks remain authoritative.
- Context compaction is wired at runtime safe points when the session and
  provider capacity are known, and is available explicitly through CLI, TUI,
  ACP, and session-facing application calls. Durable compaction rows remain
  provider-neutral and separate from provider generation; the existing SQLite
  turn-finalization transaction remains the owner of whole-turn atomicity.

## Compatibility boundary

This decision does not claim stateful Responses `previous_response_id` chains,
automatic Ultracode delegation, model-generated titles, arbitrary plugin or
hook execution, ACP MCP transport, interactive client PTY input/resize/framing,
or native audio/video provider semantics. Binary multimedia is accepted and
bounded at the ACP/domain boundary, but binary history replay and provider
native handling remain explicit future capabilities.

## Consequences

The runtime now has shared evidence and result contracts for CLI, TUI, ACP,
replay, and diagnostics without duplicating prompt or credential stores. The
cost is that parallel read-only execution needs private transcript projections,
MCP metadata is exposed only through a private extension, and some integrations
remain intentionally partial until their external protocol contracts are
stable and tested.
