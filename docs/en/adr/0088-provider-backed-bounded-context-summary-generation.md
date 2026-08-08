# ADR 0088: Provider-backed bounded context-summary generation

- Status: accepted for the Stage5DH vertical slice
- Date: 2026-08-08
- Scope: application memory and the existing ModelProvider port

## Context

Stages 5DD–5DG established deterministic compaction assessment, provider-aware
summary requests, redacted input projections, and durable resume records. The
next useful boundary is an explicit way to ask the selected provider for one
summary without changing the AgentRuntime loop or trusting raw session data.

## Decision

`ProviderContextSummaryGenerator` lives in the canonical
`neuro_code.application.memory.compaction` module. It accepts only a validated
`ContextSummaryInput`, constructs a temporary two-message `ModelContext` from
that bounded projection, and makes exactly one request with `tools=()` and
`ModelToolPolicy.DISABLED`.

The prompt contains only fixed instructions, bounded provider labels, counts,
and the already projected item text. It never receives the source context,
tool arguments, reasoning content, preserved provider payloads, credentials,
or source fingerprints. The generator applies redaction and UTF-8/token
bounds again to provider output before returning the in-memory
`ContextSummaryGenerationResult`; the summary is excluded from `repr`.

Text deltas are buffered and `ModelCompleted.response_text` is preferred when
present, preventing duplicate output. Missing completion, an empty result,
multiple completions, or a ModelToolCall is treated as a `ProviderError`.
ProviderError and cancellation propagate unchanged; no tool is executed and
there is no retry loop, persistence write, runtime event, or automatic
compaction.

The generator validates the request's provider/model identity against the
injected provider. An omitted context affinity remains compatible, while an
explicit affinity must match. A later runtime slice may call this generator
only after choosing its own transaction and stale-source policy.

## Consequences

- Summary generation has a testable provider seam without widening the model
  port or changing ordinary requests.
- A generated summary is not durable until a caller explicitly passes it to
  `build_durable_compaction_item()` and the storage port.
- Provider-specific tokenizers, retries, Runtime integration, compaction
  events, UI behavior, export/import, and whole-turn atomicity remain future
  work.
