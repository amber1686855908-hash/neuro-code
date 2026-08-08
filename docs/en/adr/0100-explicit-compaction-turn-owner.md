# ADR 0100: Explicit compaction turn owner

- Status: accepted
- Date: 2026-08-08
- Scope: `TurnEventRecorder`

## Context

Stage5DS defined a typed projection between the explicit compaction gate and
turn finalization, but did not define who consumes it. Passing a successful
compaction item without the ordinary turn outcome, or consuming a propagation
failure as a completion, would make event and execution-record projections
inconsistent.

## Decision

`TurnEventRecorder.finalize_turn_from_compaction_projection()` is an opt-in
turn-owner seam:

- a successful projection must receive the caller's ordinary turn outcome and
  is finalized through `finalize_turn_with_compaction()`;
- a timeout projection uses its bounded `BUDGET_LIMITED` outcome and the
  ordinary finalization path, without inventing a compaction row;
- propagation-only and no-op projections fail closed before an in-memory
  completion event is appended;
- all ordinary completion calls continue to use the existing method and
  behavior.

The method does not call a Provider, generate a summary, acquire a session
lock, or trigger compaction. The explicit application caller remains
responsible for the safe-boundary request, stale-source guard, cancellation,
and exception propagation. The normal Agent loop and automatic compaction do
not call this seam.

## Consequences

There is one typed handoff from compaction to the existing event/storage owner.
The SQLite transaction still covers only the final event/items/record/item
commit; Provider generation and the prior independent compaction save remain
outside it. Future integration must call this method only from a caller that
already owns the turn lifecycle and session concurrency boundary.
