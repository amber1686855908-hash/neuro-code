# ADR 0155: Durable MAIN_MAX verification snapshot

- Status: Accepted
- Date: 2026-09-07
- Scope: VF-4a bounded local Ultracode integration

## Context

VF-3b persists an optional immutable `VerificationRequirementsSnapshot` in
`TurnInput` for ordinary Agent retry and recovery. The explicit Ultracode
entry previously rejected every structured request, which meant that its
`MAIN_MAX` branch could not preserve the same verification contract. The
`BOUNDED_SWARM` branch still has no worker-level verification integration and
must not receive a structured snapshot in this slice.

## Decision

`MAIN_MAX` freezes one effective parent verification snapshot before the
parent `TurnInput` or durable Ultracode execution is created. When the request
contains `None`, `NormalTurnRequirementsPolicy.resolve(None)` is called once.
An explicit non-empty snapshot and an explicit empty snapshot are already
caller-owned decisions and are preserved exactly.

The exact snapshot is carried by both `TurnInput` and the immutable
`UltracodeExecution` identity. A retry or recovery request that supplies a
different snapshot is an identity conflict. A fresh structured
`BOUNDED_SWARM` request is rejected before parent-session creation and
durable branch claim; legacy BOUNDED_SWARM requests continue with `None`.

## Durable representation and migration

Schema 30 adds two nullable columns to
`orchestration_ultracode_executions`:

- `verification_requirements_json`
- `verification_requirements_fingerprint`

Both NULL values mean permanent legacy mode. A structured row must contain a
canonical, bounded snapshot and its matching fingerprint. Partial, malformed,
oversized, or tampered projections fail closed. The schema-29-to-30 migration
adds only these columns and preserves existing rows and legacy behavior; no
new table or database schema is introduced.

## Recovery

The existing normal Agent runtime remains the verification and final-response
authority. If a MAIN_MAX parent turn is already durably committed, recovery
uses a dedicated `replay_committed_turn` projection to publish the exact
committed result. It does not call the Provider, rerun verification or the
Finalizer, create another attempt, or use the BOUNDED external-result commit
boundary to manufacture a structured verified completion.

## Non-goals

This slice does not add BOUNDED_SWARM worker/Leader verification, result
adoption verification, requirement inference, automatic discovery, a test
runner, generic scope algebra, public UI/protocol fields, or a new verification
truth owner. The `VerificationTracker` and `FinalResponseContract` remain the
existing canonical owners.

## Validation

Focused Ultracode tests cover default/explicit/empty snapshots, identity and
fingerprint binding, schema migration, malformed/tampered rows, legacy
recovery, structured MAIN_MAX recovery without duplicate execution, and
fail-closed structured BOUNDED_SWARM requests. Repository quality gates and
the complete test suite remain required for this change.
