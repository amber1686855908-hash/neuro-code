# ADR 0091: Explicit and default-disabled context-compaction trigger

- Status: accepted
- Date: 2026-08-08
- Scope: application memory and future Runtime integration boundary

## Context

The repository now has separate contracts for compaction assessment, bounded
redacted summary input, one-request Provider generation, durable item
persistence, and resume reconstruction. Those contracts still deliberately do
not call from `AgentRuntime`. A future Runtime needs a narrow boundary that can
assess a turn without side effects and can request persistence only after it
has reached a safe model-turn boundary.

## Decision

Add `neuro_code.application.memory.compaction_trigger` with:

- `ContextCompactionTriggerMode.DISABLED`, the default, which only returns the
  deterministic assessment and never calls a Provider or storage adapter;
- `ContextCompactionTriggerMode.EXPLICIT`, which may delegate one actionable
  `RECOMMENDED` or `REQUIRED` plan to the existing
  `ContextCompactionApplicationService`;
- immutable request, assessment, and result values that expose only bounded
  plan metadata; source context, identifiers, and stale-source fingerprints
  are hidden from representations;
- a stateless `ContextCompactionTriggerService` that requires a session ID,
  compaction ID, timezone-aware creation time, and caller-owned expected source
  fingerprint before an actionable persistence request.

The trigger recomputes the plan from the supplied immutable `ModelContext`.
Unknown capacity, a non-actionable plan, and disabled mode are no-ops. A stale
source fails in the existing persistence service before the Provider request.
Provider errors, cancellation, and storage errors propagate; there is no
fallback, retry, event, or partial success result.

Compaction is a separate application operation. It does not increment
`AgentRunResult.steps`, reuse a normal turn's model/tool budget, emit an event,
or retain attempt state. A future Runtime integration must define its safe
boundary and transaction semantics explicitly rather than inferring them from
this service.

## Consequences

- Runtime callers can preflight every turn without enabling automatic
  compaction.
- Explicit callers get one reusable, stale-source-guarded path to the existing
  summary and storage contracts.
- Generation and persistence remain separate operations; this ADR does not
  claim whole-turn SQLite atomicity.
- No Provider, SessionStore schema, session item, export/import, event, CLI,
  TUI, or ACP behavior changes in this stage.
