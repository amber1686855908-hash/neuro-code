# ADR 0092: Explicit Runtime compaction safe boundary

- Status: accepted
- Date: 2026-08-08
- Scope: application memory boundary for future Runtime integration

## Context

Stage5DK provides a default-disabled explicit compaction trigger, but it does
not identify when a Runtime may safely call it. Calling while a model request
or tool batch is active could produce a context snapshot that does not match
the persisted conversation. Calling after cancellation would also turn a
cancelled turn into additional Provider work.

The existing summary generator already performs exactly one strict no-tool
Provider request. The ordinary turn budget and cancellation lifecycle must not
be silently reused for that operation.

## Decision

Add `neuro_code.application.memory.compaction_runtime` with:

- `ContextCompactionSafePoint.BEFORE_MODEL_REQUEST` and
  `ContextCompactionSafePoint.AFTER_TOOL_BATCH` as the only modeled safe points;
- a typed `ContextCompactionRuntimeBoundary` that reports the model step and
  whether a model request, tool batch, or cancellation is active;
- `ContextCompactionBoundaryDecision`, which distinguishes disabled,
  non-actionable, unsafe, cancelled, and allowed requests;
- `ContextCompactionRuntimeBudget`, which fixes the current compaction contract
  at one model request, zero tool calls, and no inheritance from the ordinary
  turn budget;
- a stateless `ContextCompactionRuntimeGate` that assesses first and delegates
  to `ContextCompactionTriggerService` only for an explicit actionable request
  at a safe boundary.

Unsafe or cancelled requests return a bounded no-op result and never contact a
Provider or storage adapter. Provider, cancellation, and storage failures from
an allowed request continue to propagate from the existing trigger service.

## Consequences

This is an application contract and test seam only. It does not change
`AgentRuntime`, add an event, enable automatic threshold triggering, implement
a wall-clock timeout, or claim atomicity across Provider generation and SQLite
persistence. A future Runtime integration must call this gate only after it
has a real snapshot of the safe boundary and must add an enforced timeout
contract before accepting one.
