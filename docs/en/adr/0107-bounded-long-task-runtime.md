# ADR 0107: Bounded long-task Runtime guidance, compaction, and segments

## Status

Accepted.

## Context

The ordinary Agent budget and batch repository tools are now coherent, but a
long turn still needs three connected capabilities: advance notice before a
budget is exhausted, safe automatic context reduction, and observable
continuation points that do not reset the global safety limit.

The repository already owns the required compaction planner, no-tool summary
generator, persistence service, Runtime gate, and durable compaction item. A
second compaction implementation or a second execution budget would create
conflicting owners.

## Decision

Add a bounded `ExecutionBudgetUsage` projection derived only from the existing
`ExecutionBudget` and live supervisor counters. Its 70%, 85%, and 95% pressure
levels drive request-only `SyntheticReason.RUNTIME_BUDGET` guidance and the
safe `EXECUTION_BUDGET_UPDATED` event. TUI renders the typed event; it does not
calculate or change budgets.

In production `FINALIZE_TERMINAL` bindings with a persisted session, an
injected compaction gate, and an explicit provider context capacity,
`AgentLoopRunner` assesses automatic compaction only at
`BEFORE_MODEL_REQUEST` and `AFTER_TOOL_BATCH`. It does not compact before the
first completed ordinary model step, during a model request/tool batch, after
cancellation, or when capacity is unknown. The summary request remains one
bounded `ModelToolPolicy.DISABLED` call and does not consume ordinary
model/tool counters.

The canonical transcript remains unchanged. The newest compatible
`DurableCompactionItem` is a request projection that replaces only its
fingerprinted middle range and preserves later appended items. Candidate
boundaries never split an assistant tool call from its tool result. A provider
origin change fails the summary request rather than storing a summary under
the wrong context window. Reassessing an already compacted identical range
does not regenerate it; if that projection is still beyond the hard threshold,
the turn enters controlled `BUDGET_LIMITED/CONTEXT_WINDOW_BUDGET`
finalization.

`ExecutionSegmentPolicy` derives observation thresholds from, but never
replaces or resets, the global `ExecutionBudget`. At a completed tool-batch
boundary, confirmed progress and remaining global budget may emit one bounded
`EXECUTION_SEGMENT_CHECKPOINTED` event and inject one request-only
`SyntheticReason.RUNTIME_CHECKPOINT` message. The event contains counters,
progress categories, and plan-step counts only. It is an auditable in-turn
continuation marker, not a workspace checkpoint or process-crash resume
record.

## Transaction and failure boundaries

Provider summary generation and `save_compaction_item()` remain separate from
the later turn-finalization transaction. A saved compaction can therefore
exist even if the turn subsequently fails; stale-source validation makes its
future projection fail closed. Provider errors, storage errors, and
`CancelledError` propagate through existing turn-failure handling. The
existing bounded compaction timeout maps to controlled wall-time finalization.

## Consequences

Long evidence-producing turns can see remaining budget, compact safely, and
cross bounded observation segments without weakening the one global hard
limit. Synthetic guidance, summaries, raw evidence, fingerprints, and tool
arguments are not added to canonical conversation history or budget events.
Tool calls remain sequential, and explicit isolated read-only subagents keep
their existing caller-driven lifecycle; no automatic delegation is added.
