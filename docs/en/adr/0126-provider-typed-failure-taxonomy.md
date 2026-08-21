# ADR 0126 — Provider typed failure taxonomy

[简体中文](../../zh-CN/adr/0126-provider-typed-failure-taxonomy.md) · **English**

## Status

Accepted for the current pre-alpha runtime.

## Context

The provider boundary previously projected most HTTP, transport, and protocol
failures as a generic `ProviderError`. Resilience then guessed retryability from
message fragments, health exposed only the Python exception class, and failover
could not distinguish a bad request from a transient upstream outage. That made
permanent request errors capable of contaminating a transient circuit and made
the public failure event less useful without exposing raw provider payloads.

The five model HTTP adapters are OpenAI-compatible Chat, OpenAI Responses,
Anthropic Messages, Gemini Generate Content, and Gemini Interactions. Provider
catalog discovery has its own `ProviderCatalogError` contract and remains a
separate read-only discovery boundary.

## Decision

- `ProviderFailure` is an immutable, bounded, redacted fact object. It contains
  `kind`, safe detail, optional HTTP status, bounded `Retry-After`, provider and
  model identity when known, and an optional lifecycle phase. It never contains
  retry, circuit, or failover decisions, request bodies, headers, raw causes, or
  credentials. Exception chaining remains available through `__cause__`.
- The runtime taxonomy is `authentication`, `authorization`, `rate_limit`,
  `invalid_request`, `model_not_found`, `context_overflow`, `server`,
  `timeout`, `network`, `protocol`, and `unknown`. Configuration remains the
  existing `ConfigurationError` hierarchy rather than becoming a provider kind.
  `asyncio.CancelledError` is propagated unchanged.
- HTTP status and structured provider error fields are classified at the
  adapter boundary. Transport exception types distinguish timeout from network
  failures, and malformed stream/protocol payloads are classified as protocol
  failures. `Retry-After` is parsed only when valid and is bounded before it
  reaches the local scheduler.
- `ProviderFailurePolicy` owns three independent decisions. Authentication,
  authorization, model-not-found, and context-overflow failures do not retry or
  count toward a transient circuit but may isolate a candidate before output.
  Rate limits retry and may fail over without counting as unhealthy. Server,
  timeout, and network failures retry, count toward the circuit, and may fail
  over. Invalid requests do none of those actions; protocol failures do not
  retry or count but may fail over; unknown failures conservatively do not retry,
  count toward the circuit, and may fail over. Configuration skips a candidate
  without poisoning the circuit.
- Once any model event has been observed, retry and failover are both disabled.
  The partial stream also does not add a new circuit failure because replaying it
  would not be safe and it does not represent a clean request attempt.
- `ProviderHealth.last_failure_kind` is the stable typed observation. The
  existing `last_error_type` remains as a compatibility field. Attempt-failure
  events retain their original fields and add optional typed kind/status fields.

## Consequences

Provider resilience no longer depends on error-message wording. A provider
adapter can evolve its user-facing detail without changing retry or routing
behavior. Health and event projections remain bounded and redacted, while
operators can distinguish request, quota, transport, server, and protocol
incidents.

The taxonomy is process-local and does not claim persistent health scoring,
automatic model substitution after visible output, provider-specific billing
semantics, or a live-provider benchmark. Catalog discovery and hosted web-search
sidecar errors retain their existing separate contracts.

## Invariants

1. A permanent request/configuration failure cannot open the transient provider
   circuit.
2. No retry or failover occurs after observable model output.
3. Retry is selected from typed facts and canonical policy, never from a new
   error-message substring heuristic.
4. Public failure details are bounded and redacted; raw causes and credentials
   do not enter health, events, UI, or logs.
