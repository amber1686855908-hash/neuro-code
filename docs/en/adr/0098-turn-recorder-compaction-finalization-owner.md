# ADR 0098: Turn recorder owns opt-in compaction finalization

- Status: accepted
- Date: 2026-08-08
- Scope: `TurnEventRecorder`

## Context

ADR 0097 introduced an atomic storage operation that can commit a completed
turn together with one durable compaction item. A storage method alone does
not define which application component may use it. Calling the storage port
from a summary generator or from an interface would bypass turn event
ordering, session ownership, and the existing completion path.

## Decision

`TurnEventRecorder.finalize_turn_completion()` accepts an optional, already
validated `DurableCompactionItem`. When present, the recorder requires a
persisted session and calls `SessionStore.finalize_turn_with_compaction()`;
otherwise it keeps calling the ordinary `finalize_turn()` method.

The recorder owns only the final storage commit. It does not generate a
summary, build a context, invoke a Provider, alter the normal Agent loop, or
consume a compaction failure projection. Invalid compaction input fails before
the completion event is appended to the recorder's in-memory event list.

This is an opt-in seam for a future turn owner. No current model step or
automatic threshold invokes it, and background auto-wake completion keeps its
existing execution-record policy.

## Consequences

There is one application-level owner for the event-plus-compaction commit, and
the existing event delivery order is preserved: persistence completes before
the `TURN_COMPLETED` event is delivered. Provider generation and cancellation
remain outside the SQLite transaction and retain their existing failure
semantics. A future Runtime integration must explicitly decide how a
`ContextCompactionRuntimeFailureProjection` becomes a completion outcome
before calling this method.
