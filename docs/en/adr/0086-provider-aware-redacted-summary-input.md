# ADR 0086: Provider-Aware Redacted Summary Input

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DF

## Context

Stage5DE defined a provider-bound `ContextSummaryRequest`, but it still carried
no source projection. The next seam must make the input safe to hand to a
future summarizer without copying raw conversation payloads into the request.
Messages may contain tool arguments, reasoning, images, or credentials, and
preserved provider state may contain opaque backend payloads.

## Decision

Keep `ContextSummaryInputBuilder` in the canonical application memory module.
It accepts an immutable `ModelContext` and a `ContextSummaryRequest`, then
projects only the selected candidate range. Each `ContextSummaryItem` records
the source index, source kind, role when applicable, and a bounded text
projection. Tool arguments, preserved payloads, and reasoning content are
replaced by fixed markers rather than serialized.

The builder applies explicit and shape-based redaction before control-character
sanitization and UTF-8 byte truncation. A provider-independent token estimator
is injected at this boundary; the resulting input is bounded by
`capacity_tokens - max_summary_tokens`, and at most 128 source items and 4 KiB
per item are retained. The result stores only counts and redacted/truncated
flags; item text is excluded from its representation.

## Boundaries

This slice does not call a Provider, choose a tokenizer, construct a model
prompt, mutate `ModelContext`, persist a compaction item, or integrate with
`AgentRuntime`. The token estimator is an explicit local contract, not a claim
of provider-exact token accounting. Provider-backed summary generation,
durable compaction items, and resume reconstruction remain later capabilities.

## Verification

Tests cover secret redaction, tool/reasoning/preserved-state projection,
control and byte limits, token-budget truncation and omission, estimator
validation, context immutability, representation safety, and typed input
invariants. Architecture and import-contract tests keep the input types owned
by the canonical memory module.
