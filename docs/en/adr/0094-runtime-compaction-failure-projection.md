# ADR 0094: Runtime compaction failure projection

- Status: accepted
- Date: 2026-08-08
- Scope: application memory contract for a future Runtime integration

## Context

Stage5DM enforces a finite wall-clock limit for an explicitly enabled
compaction request, while preserving Provider failures, storage failures, and
`asyncio.CancelledError`. The gate is intentionally not part of the ordinary
`AgentRuntime` loop. A future integration therefore needs a stable way to
consume a known failure without making the gate write an execution record or
silently converting cancellation and infrastructure failures.

## Decision

`neuro_code.application.memory.compaction_runtime` now exposes a typed
`classify_context_compaction_failure()` projection. It retains no exception
message, prompt, context, Provider payload, or storage detail.

- `ContextCompactionTimeoutError` is the only controlled-terminal projection.
  A future Runtime may map it to a recoverable
  `BUDGET_LIMITED` outcome with `WALL_TIME_BUDGET`; the outcome is not marked
  `finalized` because compaction is not a final answer.
- The timeout projection's record policy is `TURN_FINALIZATION`. This means
  only the caller that owns the turn-finalization transaction may persist the
  corresponding `SessionExecutionRecord` together with that turn's terminal
  event. The compaction gate never writes an execution record.
- `asyncio.CancelledError`, `ProviderError`, and `SessionError` are propagation
  projections with no terminal outcome and no execution-record request.
  Their original exceptions remain the caller's responsibility.
- Unknown exceptions are not classified. They must not be guessed into a
  budget, cancellation, or storage status.

The projection is a policy contract only. It does not catch errors, alter
`AgentRuntime`, emit events, trigger automatic compaction, or claim a
Provider/SQLite transaction. A future Runtime must explicitly decide whether
to consume the timeout projection at a safe boundary and must use its existing
turn-finalization transaction for any execution record.

## Consequences

Timeout behavior can be tested independently of the main loop while ordinary
Provider, storage, and cancellation behavior remains unchanged. A future
integration cannot accidentally persist a standalone compaction failure or
hide an unknown exception behind a fabricated terminal outcome.
