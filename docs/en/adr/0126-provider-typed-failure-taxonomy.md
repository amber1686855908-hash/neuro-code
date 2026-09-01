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
  model identity when known, an optional lifecycle phase, and an evidence
  origin (`provider`, `transport`, `local`, or `unknown`). It never contains
  retry, circuit, or failover decisions, request bodies, headers, raw causes, or
  credentials. Exception chaining remains available through `__cause__`.
- The runtime taxonomy is `authentication`, `authorization`, `rate_limit`,
  `invalid_request`, `model_not_found`, `context_overflow`, `server`,
  `timeout`, `network`, `protocol`, and `unknown`. Configuration remains the
  existing `ConfigurationError` hierarchy rather than becoming a provider kind.
  `asyncio.CancelledError` is propagated unchanged.
- HTTP status and structured provider error fields are classified at the
  adapter boundary. The shared HTTP classifier is deliberately conservative:
  it does not parse human messages, does not turn a generic 404 into
  `model_not_found`, does not turn an unstructured 429 into a retryable
  `rate_limit`, and treats generic 413 as `invalid_request`. Each adapter then
  owns exact documented envelope fields for its protocol. Transport exception
  types distinguish timeout from network failures, and malformed
  stream/protocol payloads are classified as provider protocol facts.
  `Retry-After` is parsed only when valid and is bounded before it reaches the
  local scheduler.
- `ProviderFailurePolicy` owns three independent decisions. Authentication,
  authorization, model-not-found, and context-overflow failures do not retry or
  count toward a transient circuit but may isolate a candidate before output.
  Rate limits retry and may fail over without counting as unhealthy when the
  provider envelope is unambiguous. Server,
  timeout, and network failures retry, count toward the circuit, and may fail
  over. Invalid requests do none of those actions; protocol failures do not
  retry or count but may fail over; provider/transport unknown failures do not
  retry or count but may fail over, while local unknown failures stop at the
  current candidate. Configuration skips a candidate without poisoning the
  circuit.
- Once any model event has been observed, retry and failover are both disabled.
  The partial stream also does not add a new circuit failure because replaying it
  would not be safe and it does not represent a clean request attempt.
- `ProviderHealth.last_failure_kind` is the stable typed observation. The
  existing `last_error_type` remains as a compatibility field. Attempt-failure
  events retain their original fields and add optional typed kind/status fields.

## Conformance evidence

The implementation uses offline fixtures derived from the official protocol
error envelopes. This is a bounded conformance slice, not a claim of complete
provider compatibility.

| Protocol | Exact structured evidence used | Canonical fact and policy boundary |
|---|---|---|
| OpenAI-compatible Chat / OpenAI Responses | OpenAI documents authentication, temporary rate limits, credit balance, organization/project spend, usage limits, server errors, and `response.failed` `server_error` fields | Explicit billing/spend/usage codes map to `authorization`; explicit transient rate codes map to `rate_limit`; unknown 429 remains `unknown` |
| Anthropic Messages | `error.type` values such as `authentication_error`, `billing_error`, `permission_error`, `invalid_request_error`, `request_too_large`, `rate_limit_error`, `api_error`, `timeout_error`, and `overloaded_error` | `billing_error` maps to `authorization`; `rate_limit_error` maps to `rate_limit` and preserves the documented `Retry-After` response hint; `not_found_error` remains a generic resource/endpoint 404 |
| Gemini Generate Content | `error.status` / ErrorInfo reasons such as `API_KEY_INVALID`, `INVALID_ARGUMENT`, `FAILED_PRECONDITION`, `PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`, `INTERNAL`, `UNAVAILABLE`, and `DEADLINE_EXCEEDED` | `RESOURCE_EXHAUSTED` is the documented 429 rate-limit fact for RPM/TPM/RPD/spend dimensions and maps to `rate_limit`; the bounded policy retries without counting it toward the circuit |
| Gemini Interactions | `error.code` values including `authentication`, `permission_denied`, `model_not_found`, `not_found`, `rate_limit_exceeded`, `quota_exceeded`, `api_error`, `service_unavailable`, and `deadline_exceeded` | Explicit rate, quota, and model codes receive separate facts; future codes remain `unknown` |

Primary references: [OpenAI error codes](https://developers.openai.com/api/docs/guides/error-codes),
[OpenAI rate limits](https://developers.openai.com/api/docs/guides/rate-limits),
[OpenAI Responses streaming](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal/delta),
[Anthropic API errors](https://platform.claude.com/docs/en/api/errors),
[Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits),
[Gemini Generate Content API errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors),
[Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits),
and [Gemini API errors](https://ai.google.dev/gemini-api/docs/api-errors).

## Circuit and configuration semantics

`consecutive_failures` is the number of consecutive pre-output failures that
are eligible to count toward the transient circuit, starting after the last
successful request or any circuit-ineligible failure. A `SERVER, SERVER,
INVALID_REQUEST, SERVER` sequence therefore ends at one, as does
`SERVER, SERVER, RATE_LIMIT, SERVER`; neither sequence opens a threshold-three
circuit. A partial stream never adds a circuit failure.

`ConfigurationError` remains separate. Its current pre-output behavior is
candidate-specific: a provider dialect or tool configuration can be skipped
in a failover chain, but it is not retried and never counts toward health. A
global configuration error is expected to fail before candidate execution and
is not reinterpreted as provider health evidence.

## Consequences

Provider resilience no longer depends on error-message wording. A provider
adapter can evolve its user-facing detail without changing retry or routing
behavior. Health and event projections remain bounded and redacted, while
operators can distinguish request, ambiguous quota, transport, local runtime,
server, and protocol incidents. Offline fixtures prove the listed envelopes;
live/paid calls and a real-provider benchmark remain unrun.

The taxonomy is process-local and does not claim persistent health scoring,
automatic model substitution after visible output, provider-specific billing
semantics, or a live-provider benchmark. Catalog discovery and hosted web-search
sidecar errors retain their existing separate contracts.

## Invariants

1. A permanent request/configuration or circuit-ineligible unknown failure
   cannot open the transient provider circuit.
2. No retry or failover occurs after observable model output.
3. Retry is selected from typed facts and canonical policy, never from a new
   error-message substring heuristic.
4. Public failure details are bounded and redacted; raw causes and credentials
   do not enter health, events, UI, or logs.
