# ADR 0089: Explicit context-compaction persistence service

- Status: accepted
- Date: 2026-08-08
- Scope: application memory and SessionStore boundary

## Decision

Add `ContextCompactionApplicationService` as an explicit application use case.
It builds a redacted `ContextSummaryInput` from an immutable `ModelContext`,
checks the caller-provided source fingerprint before making a model request,
uses the existing `ProviderContextSummaryGenerator`, converts the bounded
result with `build_durable_compaction_item`, and saves through the canonical
`SessionStore.save_compaction_item` port.

The request carries an opaque expected source fingerprint and a caller-chosen
compaction ID. A changed source count or fingerprint fails before the Provider
is called. The storage adapter owns duplicate-ID semantics: identical records
remain idempotent and conflicting records fail closed.

Generation and persistence are intentionally separate operations. A Provider
request is not part of the SQLite transaction that saves the resulting item;
the service reports success only after the storage port returns and propagates
Provider, cancellation, and storage failures without retry or fallback.

## Boundaries

- No `AgentRuntime` integration or automatic compaction.
- No new event, session item, export/import record, or UI projection.
- No raw context, prompt, tool arguments, credentials, or source digest is
  sent to the Provider or exposed through the result representation.
- The durable item remains provider-neutral and can be rebuilt on resume only
  after its existing source and affinity checks pass.

## Rationale

The service is a narrow application orchestration boundary rather than a new
storage implementation. It makes stale-source validation and the separate
model/write transaction boundary explicit before any future Runtime trigger
is considered.
