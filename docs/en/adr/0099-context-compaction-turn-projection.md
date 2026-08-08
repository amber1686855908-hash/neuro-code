# ADR 0099: Typed context-compaction turn projection

- Status: accepted
- Date: 2026-08-08
- Scope: `application.memory.compaction_runtime`

## Context

Stage5DR gave `TurnEventRecorder` an opt-in path that can atomically commit a
validated durable compaction item with a completed turn. The runtime gate also
already classifies timeout, cancellation, Provider, and storage failures. A
caller still needs one small, typed boundary that transfers only the safe value
it owns, without making the gate emit events or silently consuming failures.

## Decision

`ContextCompactionTurnProjection` and its two projection functions are the
explicit transfer boundary:

- `project_context_compaction_result()` transfers only the already persisted
  `DurableCompactionItem` from a successful trigger result;
- `project_context_compaction_failure()` transfers only the bounded
  `ContextCompactionRuntimeFailureProjection` and its optional typed outcome;
- timeout is ready for a caller-owned terminal completion with a recoverable
  `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome;
- cancellation, Provider, and storage failures remain propagation-only;
- unknown exceptions remain unclassified and are not guessed into a result.

The projection stores no exception, prompt, raw summary, tool data, or
workspace data in its representation. It does not persist, emit events, call a
Provider, or invoke `TurnEventRecorder`. A future turn owner must still decide
the completion event data and call the recorder explicitly. The normal Agent
loop and automatic compaction remain disabled.

## Consequences

The success path can pass the item to
`TurnEventRecorder.finalize_turn_completion(..., compaction_item=item)` without
rebuilding or re-saving it. A timeout can be consumed only by a caller that
owns the turn-finalization transaction. Propagated failures cannot be
mistakenly reported as an empty or successful turn. Provider generation and
the independent compaction save remain outside the SQLite transaction; the
projection does not expand that atomicity claim.
