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
after cancellation. Pristine rewind before the first token and full draft
restoration are broader interaction policies. The TUI now provides a bounded
presentation-only queue for explicit follow-up prompts submitted before the
first non-empty model token.

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
- TUI prompts opt into a pristine-rewind policy. If cancellation occurs before
  any non-empty model text/reasoning, completion, or tool activity, the runtime
  saves the pre-turn item prefix instead of the cancelled user message and marks
  `pristine_rewound: true` on `TURN_FAILED`. The submitted user event remains in
  the append-only audit stream; no model context or tool result is replayed.
  Once output or tool activity exists, the message is retained and normal
  cancellation recovery applies.
- Before the first non-empty model token, the TUI accepts at most four explicit
  follow-up prompts into a local queue. A successful turn starts them in order;
  cancellation or failure restores the first queued prompt to the input. The
  queued prompts are not sent to the runtime or persisted until their turn
  starts, and the queue never accepts input after the first token.
- Local adapters retain responsibility for bounded cleanup. In particular,
  Bash cancellation terminates its owned process tree. When background
  management is enabled, cancellation during the foreground wait also kills
  the same manager-owned task and discards its terminal record. Cancelling a
  client stream cannot guarantee cancellation of work already executing inside
  a provider-hosted tool.

## Consequences

Cancellation now leaves provider-valid local tool-call/result ordering and a
conversation that can immediately run another prompt. Audit consumers can
distinguish cancellation through `cancelled: true` on `tool_failed` and
`turn_failed` data. Headless runtime callers receive the original
`CancelledError`; cancellation is never reported as success.

This is still partial M3 behavior. The TUI now restores a safely rewound prompt
to its draft and keeps the existing bounded pre-token interjection queue.
Provider-hosted remote cancellation guarantees and cross-platform PTY smoke
coverage remain future slices. Model-completion auto-wake is available as an
explicitly enabled, session-scoped, bounded TUI policy with persisted global
defaults, per-provider overrides, cooldown, budget, duplicate suppression, and
restart-aware wake-ledger recovery. Enabled Bash calls automatically promote a
still-running foreground command after its wait budget without restarting it;
the task remains owned by the conversation scope.

## Validation

Neuro Code validates cancellation and recovery behavior through its own runtime,
conversation, and interactive-interface tests.
