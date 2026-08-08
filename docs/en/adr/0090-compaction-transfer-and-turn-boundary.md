# ADR 0090: Compaction transfer and turn-finalization boundary

- Status: accepted
- Date: 2026-08-08
- Scope: session export/import, fork, and compaction persistence

## Context

`DurableCompactionItem` is an optimization record used to validate and rebuild
a bounded context projection. It is not canonical conversation history. The
existing session export format is public interchange data, while
`SessionStore.finalize_turn()` atomically commits a completion event, ordered
session items, the search projection, and an optional execution record.

## Decision

Compaction rows are intentionally excluded from `SessionExport` and therefore
from JSON/Markdown export and snapshot import. Export/import preserves the
canonical session items; an imported session starts with no compaction rows and
can rebuild or create new rows only through an explicit later operation. This
keeps summaries, opaque source fingerprints, provider affinity metadata, and
compaction implementation details out of the interface contract.

Forking copies the existing canonical session projection according to the
session fork contract, but does not copy compaction rows. A child may diverge
from the parent context, and a parent's source fingerprint or provider window
must not be treated as valid child state.

Compaction persistence remains a separate short storage transaction.
`finalize_turn()` does not implicitly save, delete, or roll back compaction
rows. Its atomicity claim is limited to its own completion event, session items,
search projection, and optional execution record. A future runtime integration
that needs compaction and turn finalization in one transaction must introduce a
new explicit storage contract and tests; callers must not infer cross-operation
atomicity from sequential method calls.

Deletion continues to rely on the session foreign key cascade. Existing
sessions without compaction rows remain fully compatible.

## Consequences

- Export schema version remains unchanged.
- Resume from an imported or forked session uses canonical items without stale
  parent compaction metadata.
- No digest, summary, prompt, tool payload, or provider-private metadata is
  rendered by the current export paths.
- The boundary is behavior-preserving and does not enable automatic Runtime
  compaction or add events.
