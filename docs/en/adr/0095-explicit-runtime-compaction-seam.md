# ADR 0095: Explicit Runtime compaction seam

- Status: accepted
- Date: 2026-08-08
- Scope: `AgentRuntime` application facade

## Context

Stage5DN defined how a future Runtime may consume a compaction timeout without
changing cancellation, Provider, or storage errors. The existing gate already
requires a safe-boundary snapshot, but there was no Runtime-facing seam. Adding
automatic threshold checks to `AgentRuntime.run()` would change ordinary turn
behavior before compaction's transaction and event contracts are ready.

## Decision

`AgentRuntime` accepts an optional
`compaction_runtime_gate: ContextCompactionRuntimeGate | None`, defaulting to
`None`, and exposes `trigger_context_compaction()` for an explicitly supplied
`ContextCompactionRuntimeRequest`.

- A missing gate fails closed with `ConfigurationError`; it never falls back to
  a Provider request or ordinary Agent turn.
- The caller must supply the complete immutable request, including the
  trigger context, source guard, safe point, active-operation flags,
  cancellation state, and independent compaction budget.
- The facade only validates the request type and delegates to the injected
  gate. It does not derive thresholds, mutate context, increment turn steps,
  emit events, or write an execution record.
- The gate's timeout, cancellation, Provider, and storage semantics remain
  unchanged. A future turn owner may explicitly consume the Stage5DN timeout
  projection and persist a record only through turn finalization.
- `AgentRuntime.run()` is unchanged; no automatic or default compaction is
  enabled, and ApplicationComposition does not inject a gate yet.

## Consequences

Tests and a future application caller can exercise compaction at a proven safe
boundary without coupling the ordinary Agent loop to threshold or persistence
policy. The seam is intentionally narrow: wiring a production gate, adding
events, or claiming whole-turn atomicity requires a later vertical slice.
