# ADR 0097: Explicit atomic turn finalization with compaction

- Status: accepted
- Date: 2026-08-08
- Scope: `SessionStore` and SQLite persistence

## Context

Durable context compaction is intentionally an independent operation today.
`save_compaction_item()` owns a short transaction, while `finalize_turn()` owns
the `TURN_COMPLETED` event, session items, search projection, and optional
execution record. Calling those methods one after the other cannot make a
provider request and two storage operations one transaction, and it cannot
protect a turn from a failure between the writes.

The next integration boundary needs a precise storage contract without
changing ordinary turns or silently widening the existing compaction method.

## Decision

`SessionStore` exposes the opt-in method
`finalize_turn_with_compaction(session_id, event, items, record, compaction_item)`.
The SQLite implementation writes these projections in one `BEGIN IMMEDIATE`
transaction:

- the `TURN_COMPLETED` event;
- the append-only session-item prefix and search projection;
- the optional `SessionExecutionRecord`;
- one `DurableCompactionItem`.

The method commits only after every projection succeeds and rolls back the
whole transaction on validation, uniqueness, search-index, or storage failure.
An identical existing compaction ID is idempotent; an owner or payload
conflict is rejected. A duplicate completion event remains an error.

`save_compaction_item()` remains an independent short operation. The new
method does not make provider generation part of SQLite atomicity, does not
change `finalize_turn()`, and is not called by the normal Agent loop or the
current explicit compaction gate. A future caller that owns a turn-finalizing
transaction may opt into this method and must still define its timeout and
cancellation ownership.

## Consequences

The boundary now distinguishes two claims:

1. an isolated compaction write is atomic by itself;
2. an explicit turn-finalization call can atomically commit its event, items,
   execution record, search projection, and compaction row.

Neither claim covers provider/network work. Existing ordinary turns and
default-disabled compaction behavior remain unchanged.
