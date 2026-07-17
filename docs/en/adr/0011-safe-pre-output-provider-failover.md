# ADR 0011: Safe pre-output provider failover

[简体中文](../../zh-CN/adr/0011-safe-pre-output-provider-failover.md) · **English**

Status: accepted

## Context

Named profiles make it possible to route one run across direct providers,
local gateways, or CC Switch profiles. A provider can be unavailable because a
credential is missing, an endpoint is down, or a request fails before a stream
starts. Trying an alternative can improve availability, but replaying a model
step after output or a hosted tool has started can duplicate visible text,
side effects, or charges. Imported sessions may also contain opaque native
context that is valid only for their original profile affinity.

## Decision

- `[routing] fallbacks` is an ordered list of existing, unique profiles and may
  not contain the configured default profile.
- Build candidate adapters lazily. A missing fallback credential therefore
  does not block a healthy primary profile.
- Buffer only the first event from each candidate. Configuration and provider
  failures before that event advance to the next candidate. The first event of
  any kind is the commit point; a later error propagates without failover.
- Attempt each candidate at most once. After a candidate succeeds, begin later
  model steps from it and never return to an earlier candidate during that run.
- Emit `provider_attempt_failed` for each pre-output failure and
  `provider_selected` when a candidate first succeeds. Bound and normalize
  surfaced error text, and aggregate failures if no candidate succeeds.
- `--no-failover` bypasses the chain and constructs only the selected profile.
- A newly created or ordinary-message-only session may adopt the successful
  fallback's provider, model, and affinity. A session containing preserved
  opaque context keeps its stored origin; the selection event records that the
  origin was not updated.
- Do not retry the same candidate, infer provider health across processes, or
  implement a circuit breaker in this slice.

## Consequences

Startup and pre-stream failures can recover without making CC Switch a runtime
dependency. Failover remains observable and deterministic, and once any model
activity is visible the runtime favors side-effect safety over availability.
The first-event rule is deliberately stricter than checking only text: a
reasoning delta, local tool call, hosted-tool lifecycle event, usage report, or
completion also commits the candidate.

Selection is process-local. Persistent health scoring, backoff, retry budgets,
and policies for safely translating opaque context between providers require
separate decisions and tests.
