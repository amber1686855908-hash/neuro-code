# ADR 0087: Durable Context-Compaction Items and Resume Projection

- Status: accepted for the Stage5DG vertical slice
- Date: 2026-08-08
- Scope: application memory, domain conversation values, and SQLite session storage

## Context

Stages 5DD–5DF defined deterministic compaction assessment, provider-aware
summary requests, and bounded redacted summary input. Those stages deliberately
did not call a provider, mutate `ModelContext`, or persist a summary. A later
runtime integration needs a durable boundary that can reject stale summaries
without exposing the source conversation.

## Decision

`DurableCompactionItem` is a small domain value owned by
`neuro_code.domain.conversation.compaction`. It stores only bounded provider
identity, context capacity, source counts and half-open candidate indexes, an
opaque SHA-256 source fingerprint, summary token metadata, a timezone-aware
creation time, and an already-redacted summary. The summary is excluded from
the value's `repr`; prompts, tool arguments/results, credentials, complete
source context, and supervisor state are never persisted.

`build_durable_compaction_item()` in
`neuro_code.application.memory.compaction` performs the final redaction,
control-character sanitization, UTF-8 bound, and summary-token bound before
constructing the domain value. The fingerprint is used only for stale-source
validation and is never sent to a model or interface.

`CompactionResumeRebuilder` is an explicit, side-effect-free projection. It
requires matching source counts, provider origin, non-overlapping ranges, and
the current source fingerprint. It replaces each validated range with an
in-memory `SyntheticReason.COMPACTION_SUMMARY` user message while preserving
the context origin and reasoning effort. It never runs a model, replays tools,
writes storage, or changes the input context.

SQLite schema v13 adds `session_compaction_items`, foreign-keyed to `sessions`
with a per-session source-range uniqueness constraint. Records are inserted or
idempotently re-saved by `compaction_id`, loaded in deterministic order, removed
by session cascade, and intentionally not copied by fork or included in
import/export. Schema migration is forward-only from v12 and remains inside
the existing serialized initialization transaction.

The storage methods are part of the existing `SessionStore` port. This keeps
SQLite behind the application port and does not make interfaces read the
database directly.

## Consequences

- Existing sessions without compaction rows remain valid and rebuild unchanged.
- No provider request, automatic compaction, runtime event, or main-loop
  behavior changes in this slice.
- A later runtime integration must persist the canonical context replacement
  and summary record with an explicit transaction boundary; this ADR does not
  claim whole-turn atomicity.
- Sequential compactions against a changed source require the later integration
  to persist a new canonical source snapshot; the rebuilder rejects mismatched
  source counts or overlapping/stale records rather than guessing.
