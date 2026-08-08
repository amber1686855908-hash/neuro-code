# ADR 0084: Deterministic Context Compaction Assessment Contract

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DD

## Context

Neuro Code already stores ordered conversation items and can report either
provider usage or a bounded local estimate. It does not yet have a provider-
aware context window contract, a durable summary item, or a runtime compaction
loop. Adding those concerns directly to `AgentRuntime` would mix assessment,
summarization, persistence, and provider replay before their boundaries are
defined.

## Decision

Add `neuro_code.application.memory.compaction` as a deterministic assessment
boundary. `ContextCompactionPlanner` accepts an immutable usage snapshot and an
ordered item sequence, then returns a `ContextCompactionPlan` containing only
typed counts, token thresholds, and a half-open candidate index range.

The default policy marks 80% of known capacity as a soft threshold and 95% as a
hard threshold. The protected prefix and a configurable recent suffix are never
included in the candidate range. Unknown capacity produces `UNAVAILABLE` and
does not propose compaction. Estimated usage is retained as metadata; it does
not become exact provider usage.

## Boundaries

This slice never summarizes or mutates `SessionItem` values, never stores prompt
text or tool output in a plan, and never writes SQLite or session items. It does
not change `ModelProvider`, `ModelContext`, `AgentRuntime`, `Finalizer`, TUI,
CLI, ACP, or provider payloads. A later slice must define provider-specific
summary generation, preservation of system/project instructions and unresolved
tool state, provider-affinity rules, durable compaction items, and the runtime
transaction boundary before automatic compaction is enabled.

## Verification

Unit tests cover unknown capacity, soft and hard threshold decisions, protected
and recent retention, insufficient candidate ranges, immutable inputs, bounded
plan representations, and invalid usage/policy/plan invariants. Architecture
and import-contract tests require the memory module to remain the sole canonical
owner of these types.
