# ADR 0083: ACP Subagent Alias Reconnect Compatibility

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DC

## Context

The private `_neuro-code/session/subagents` extension returns an external ACP
alias for `resume` and `fork`.  ACP clients may reconnect between lifecycle
requests, and storage may reject a proposed alias because another session
already owns it.  A protocol adapter must preserve the durable alias on
reconnect and must never project an alias that resolves to a different child
session.

## Decision

Lifecycle alias allocation remains bounded to four attempts.  Each successful
allocation is resolved through the ACP alias namespace before it is serialized;
if the alias is unavailable, cannot be resolved, or resolves to another
internal session, the adapter retries with a fresh bounded proposal.  Exhausted
attempts fail closed with `session_alias_allocation_failed`.

The storage-backed `get_or_create` contract remains the source of idempotence:
repeated `resume` requests and a new ACP agent instance after reconnect return
the existing alias for the same child session.  No new alias is created merely
because the client connection changed.

## Boundaries

This compatibility slice does not change the ACP standard capability set,
child execution, lifecycle ownership, SQLite schema, parent transcript,
provider behavior, scheduling, retries of model work, recursion, parallelism,
or write capabilities.  Alias allocation is still a separate bounded storage
operation and is not claimed to be atomic with the lifecycle action.

## Verification

Tests cover reconnect through the SDK private route, collision retry, stable
alias reuse, bounded failure, and fail-closed ownership mismatch.  The wire
projection continues to contain only the external alias and action; internal
session IDs and child content remain excluded.
