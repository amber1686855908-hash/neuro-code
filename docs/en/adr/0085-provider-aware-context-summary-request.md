# ADR 0085: Provider-Aware Context Window and Summary Request

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DE

## Context

Stage5DD established a provider-neutral compaction assessment, but a later
summary service needs to know which provider/model window the plan belongs to.
The existing provider port intentionally does not expose a new context-window
request parameter, while configured provider profiles and selected-provider
events already carry bounded local capacity metadata.

## Decision

Keep provider-aware compaction metadata in the application memory seam. The
immutable `ProviderContextWindow` identifies only provider and model labels,
optional context affinity, and a positive token capacity. A
`CompactionContextUsage` may bind to that window, and the planner clamps the
bounded summary-token budget to the known capacity.

An actionable plan can be projected to a `ContextSummaryRequest`. The request
contains counts, a half-open candidate range, a target token count, a bounded
summary budget, and the provider window; it never contains source items,
prompts, tool output, credentials, or provider payloads. Unknown capacity,
non-actionable plans, and empty candidate ranges cannot produce a summary
request.

## Boundaries

This slice does not change `ModelProvider`, provider payloads, `ModelContext`,
`AgentRuntime`, `Finalizer`, session persistence, or interface behavior. It does
not tokenize text, build a redacted prompt, call a model, or persist a summary.
Provider-specific tokenization, summary generation, durable compaction items,
and resume reconstruction require a later vertical slice.

## Verification

Tests cover provider-window identity validation, usage/capacity binding, small
capacity budget clamping, actionable request projection, unknown-capacity and
empty-candidate rejection, and bounded summary budgets. Architecture and
import-contract tests keep all public types owned by the canonical memory
module.
