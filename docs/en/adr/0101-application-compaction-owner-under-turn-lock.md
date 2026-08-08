# ADR 0101: Application compaction owner under the turn lock

- Status: accepted
- Date: 2026-08-08
- Scope: `AgentConversation` and `ConversationRunner`

## Context

Stages 5DS and 5DT defined a typed handoff from the explicit compaction gate
to turn finalization, but the application caller still had to coordinate the
gate and the finalization owner itself. Calling those operations separately
could let a normal turn start between them, or let a no-op projection reach a
callback that expected a terminal value.

## Decision

`AgentConversation.run_context_compaction_with_owner()` is an explicit,
opt-in application seam:

- request validation and the gate call run under the existing conversation
  `_turn_lock`;
- the caller supplies the complete immutable context snapshot, boundary,
  stale-source metadata, and session identity;
- a successful gate result is converted to
  `ContextCompactionTurnProjection` before the owner callback runs;
- a bounded timeout is converted to the existing recoverable
  `BUDGET_LIMITED/WALL_TIME_BUDGET` projection;
- no-op projections fail closed before the owner is called;
- cancellation, Provider, storage, and unknown failures preserve the
  original exception and do not invoke the owner;
- the owner callback remains responsible for `TurnEventRecorder` and any
  finalization transaction; the conversation method does not emit events,
  mutate transcript items, or persist a turn itself.

The generic `ConversationRunner` protocol exposes the same typed callback
shape for application consumers. The normal Agent loop, automatic threshold
triggering, and user-facing UI remain unchanged.

## Consequences

The gate and its owner now have one explicit concurrency boundary, and a
future caller can prove that a successful item or controlled timeout is the
only value handed to turn finalization. This does not make Provider summary
generation and SQLite persistence one transaction: the existing compaction
service still persists its item before the owner callback, while the owner may
choose the separate atomic turn-plus-item storage contract for finalization.
