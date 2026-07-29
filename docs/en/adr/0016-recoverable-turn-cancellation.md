# ADR 0016 — Recoverable turn cancellation

[简体中文](../../zh-CN/adr/0016-recoverable-turn-cancellation.md) · **English**

## Status

Accepted.

## Context

Cancelling a streaming model request, approval wait, or local tool must stop
owned work without corrupting the durable conversation. A model response may
contain several local tool calls. If cancellation leaves an assistant tool call
without a corresponding tool result, the next provider request can be invalid.
`asyncio.CancelledError` also bypasses ordinary `Exception` recovery, so a
conversation controller can retain stale in-memory items after the runtime has
saved its terminal state.

The historical terminal behavior proves that a pane must accept another prompt
after cancellation. Pristine rewind before the first token, restored drafts,
and queued interjections are broader interaction policies and are not required
for this vertical slice.

## Decision

- Textual owns each turn through one `Worker`. The prompt remains enabled while
  it runs. App-level `Ctrl+C` and local `/cancel` cancel that worker. With no
  active turn, `Ctrl+C` clears a draft or reports that no turn is running.
- The approval modal retains a narrower fail-closed binding: `Ctrl+C` denies
  the pending request instead of cancelling the whole turn.
- The runtime treats `CancelledError` as a terminal failure, not completion. It
  cooperatively cancels the active adapter, records a `tool_failed` event and
  error result for the active local call, records error results for all later
  calls from the same model batch, emits a cancelled `turn_failed`, and saves
  the ordered session items. Normal tool outcomes are appended before their
  terminal audit event so event-sink cancellation cannot orphan the call.
- `AgentConversation` catches cancellation separately and reloads canonical
  items plus provider-origin metadata from `SessionStore` before releasing the
  turn lock. A subsequent prompt therefore continues the same SQLite session.
- Streamed partial text remains presentation-only unless the provider supplied
  a terminal completion. The cancelled user message remains durable. This
  slice does not claim transaction rollback for side effects completed before
  cancellation.
- Local adapters retain responsibility for bounded cleanup. In particular,
  Bash cancellation terminates its owned process tree. Cancelling a client
  stream cannot guarantee cancellation of work already executing inside a
  provider-hosted tool.

## Consequences

Cancellation now leaves provider-valid local tool-call/result ordering and a
conversation that can immediately run another prompt. Audit consumers can
distinguish cancellation through `cancelled: true` on `tool_failed` and
`turn_failed` data. Headless runtime callers receive the original
`CancelledError`; cancellation is never reported as success.

This is still partial M3 behavior. Pristine pre-token rewind, draft restoration,
buffered interjection handling, provider-hosted remote cancellation guarantees,
and cross-platform PTY smoke coverage remain future slices.

## Validation

Neuro Code validates cancellation and recovery behavior through its own runtime,
conversation, and interactive-interface tests.
