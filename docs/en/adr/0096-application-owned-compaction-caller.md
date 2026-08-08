# ADR 0096: Application-owned explicit compaction caller

- Status: accepted
- Date: 2026-08-08
- Scope: `ApplicationComposition` and `AgentConversation`

## Context

Stage5DO exposed a narrow `AgentRuntime.trigger_context_compaction()` seam, but
left production gate construction and concurrency ownership to a future caller.
Compaction must not race a normal turn, and the operation must not pretend to
rebuild or persist a context that it does not own.

## Decision

`ApplicationComposition.create_binding()` now constructs one compaction gate per
binding from the existing provider, session store, configured redaction values,
and the existing compaction persistence/trigger services. The gate is injected
into `AgentRuntime`, but no caller invokes it automatically and no threshold is
checked by the normal Agent loop.

`AgentConversation.trigger_context_compaction()` is the explicit application
caller. It:

- serializes the request under the conversation's existing `_turn_lock`, so a
  normal turn and an explicit compaction cannot overlap for one conversation;
- accepts the complete immutable `ContextCompactionRuntimeRequest` as the
  caller-owned context snapshot rather than rebuilding or mutating context;
- requires an existing session and matching `session_id` for `EXPLICIT`
  requests; disabled assessment remains available without a persisted session;
- delegates to the injected Runtime seam and preserves Provider, cancellation,
  timeout, stale-source, and storage error semantics;
- does not append session items, reload the transcript, emit events, or claim
  transactionality with a normal turn.

The request's source fingerprint remains the stale-snapshot guard. Compaction
persistence remains a separate short storage operation; this decision does not
claim atomicity with `SessionStore.finalize_turn()`.

## Consequences

Production composition now has a real, explicit, testable gate while ordinary
turn behavior remains unchanged and compaction is still opt-in. Future work may
add a user-facing explicit command or a turn-finalization integration, but it
must preserve the session lock and define any cross-operation transaction
contract separately.
