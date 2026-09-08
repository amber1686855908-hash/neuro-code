# ADR 0156: Ultracode parent-workspace freshness projection

- Status: Accepted
- Date: 2026-09-08
- Scope: VF-4b result-adoption workspace projection

## Context

The worker Worktree and the parent checkout are different verification
boundaries. Result Adoption already records the durable target lifecycle, but
the parent verification layer needs one bounded fact saying whether a logical
adoption reached a desired parent image. That fact must survive controller
recovery and target-level diagnostic state changes without adding a second
verification tracker or a new database column.

## Decision

`ResultAdoptionRecord.parent_workspace_changed` is the canonical projection.
It is true when at least one target durably reached `APPLIED`. A target that
was applied and then changed during final verification is persisted as
`INDETERMINATE` with the exact canonical
`post_apply_concurrent_modification` error kind; that state retains the
historical applied fact. An `INDETERMINATE` target reached from `APPLYING`
before any desired image was observed does not set the projection. No-target
adoptions, pre-apply conflicts, and failures before any target reaches
`APPLIED` therefore remain false; a partial terminal outcome remains true if
another target reached `APPLIED`.

`APPLIED` means that the desired image was observed and durably acknowledged;
it does not prove which process performed the write. The projection is a
parent-workspace fact, not a causal write receipt. One multi-target adoption
is one logical mutation boundary. A future caller may advance the existing
`VerificationTracker` at most once per stable `adoption_id`, including when a
completed record is observed again during recovery.

The projection does not inspect worker response text, commands, summaries,
filesystem prose, or worker `VerificationReport` data. Worker verification
evidence is not imported into the parent tracker.

## Recovery and compatibility

The property is derived from the existing durable target rows, so fresh
controllers produce the same value without writes or a second adoption. A
desired image observed after a crash becomes `APPLIED`; an uncertain
pre-apply image becomes `INDETERMINATE` without asserting a parent change; a
post-apply final-verification race retains the applied fact. Schema 30 is
unchanged, and legacy adoption rows remain readable.

`MAIN_MAX` verification snapshot semantics are unchanged. `BOUNDED_SWARM`
structured verification remains fail-closed, and no parent verification,
worker evidence import, or public UI/protocol behavior is added in VF-4b.
Those concerns remain future VF-4c work.

## Validation

Tests cover completed, no-target, conflict, crash recovery, desired-image
recovery, pre-apply indeterminate recovery, post-apply final-verification
races, and one-mutation-per-logical-adoption projection behavior.

[简体中文](../../zh-CN/adr/0156-ultracode-parent-workspace-freshness.md) · **English**
