# ADR 0082: Fail-Closed ACP Subagent Lifecycle Projection

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DB

## Context

The private ACP subagent lifecycle extension delegates to an injected
application lifecycle owner.  A protocol boundary must not trust a faulty
owner or test double to return an action for a different parent/task, and it
must not place an unsafe external alias on the wire.

## Decision

Before projecting a lifecycle result, the ACP adapter requires the returned
parent session ID, parent task ID, and action to match the request that was
validated and dispatched.  A mismatch is rejected as
`subagent_lifecycle_invalid_result`; it is never converted into a successful
resume, fork, or delete response.

The ACP interface serializer accepts a non-delete session alias only when it
is non-empty, control-character-free, NUL-free, and within the bounded UTF-8
size limit.  Invalid output fails closed before serialization.  Delete keeps
its identifier-free `{action, deleted}` projection.

## Boundaries

This is a response-boundary hardening slice.  It does not change lifecycle
ownership, SQLite transactions, alias allocation retries, child execution,
model calls, tool replay, scheduling, recursion, parallelism, or write
capabilities.

## Consequences

Injected application seams and future implementations cannot accidentally
cross the parent relationship or emit unbounded/unsafe ACP identifiers.  The
wire contract remains the same for valid results, while malformed results
fail closed with a stable internal error.
