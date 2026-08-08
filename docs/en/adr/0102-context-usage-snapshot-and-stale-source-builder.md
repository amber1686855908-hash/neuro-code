# ADR 0102: Context usage snapshot and stale-source request builder

- Status: accepted
- Date: 2026-08-08
- Scope: `neuro_code.application.memory.compaction_runtime`

## Context

The explicit compaction owner had a safe session-lock boundary, but callers
still had to assemble `CompactionContextUsage` and calculate the source
fingerprint themselves. That duplicated the current model-usage convention and
made it easy to forget the stale-source guard when a plan was actionable.

## Decision

Add two side-effect-free application helpers:

- `build_context_usage_snapshot()` accepts an immutable `ModelContext`, an
  optional `ProviderContextWindow`, and optional provider-reported input and
  output usage. When both values are present it follows the existing
  `CONTEXT_USAGE_UPDATED` convention and records input plus output. Missing
  output keeps the input value but marks the snapshot estimated; missing input
  uses the bounded domain context estimator. Unknown provider capacity remains
  `capacity_tokens=None` and is never guessed from a provider object.
- `build_explicit_context_compaction_runtime_request()` performs only the
  deterministic trigger assessment. For an actionable plan it requires the
  caller-owned session/compaction identity and timestamp, computes the
  fingerprint from that exact context and candidate range, and returns a
  request carrying the guard. For a non-actionable plan it does not fabricate a
  digest or persistence metadata.

Neither helper calls a Provider or storage adapter, mutates context, starts a
turn, or enables automatic compaction. The existing gate and persistence
service still re-check the fingerprint at execution time, and the conversation
turn lock remains the concurrency boundary.

## Consequences

Application callers can use one typed entry point for exact-or-estimated usage
and stale-source construction while preserving the distinction between
reported and estimated values. A provider context window remains explicit
configuration; this stage does not change the `ModelProvider` protocol or
claim that all providers expose a discoverable context capacity.
