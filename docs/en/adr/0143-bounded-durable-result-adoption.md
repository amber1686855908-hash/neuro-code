# ADR 0143 — Bounded durable result adoption core

[简体中文](../../zh-CN/adr/0143-bounded-durable-result-adoption.md) · **English**

## Status

Accepted for the bounded internal vertical slice.

## Context

A completed writable worker produces evidence about its own managed worktree.
That result is not, by itself, permission to mutate the parent checkout. The
parent may contain unrelated dirty files, another controller may be recovering
the same adoption, or a worker may have changed after its result was preserved.
Treating worker text, a response summary, `git diff`, or a latest-row lookup as
the adoption authority would make those cases ambiguous and could overwrite
parent work.

The existing Task DAG, Swarm, Writable Subagent, Worktree, Checkpoint, scoped
permission, sandbox, and filesystem boundaries already own their respective
capabilities. This slice adds one explicit application service that consumes
their durable projections and forwards exact parent mutations through the
existing runtime write boundary. It is not an Ultracode feature and does not
provide a general merge engine.

## Decision

`ResultAdoptionApplicationService` is an internal, application-composed
capability. Its caller supplies only an adoption identity and a completed
Swarm identity. The service generates an immutable `ResultAdoptionPlan`; the
caller, provider, worker, response text, and relay cannot fabricate its target
paths, images, authority, or parent identity.

The plan binds:

- the active parent `ConversationBinding` session, canonical workspace root,
  repository identity, and current committed HEAD;
- the exact completed Swarm and Task DAG generation/definition fingerprint;
- the declaration-ordered completed source nodes and their child session,
  lease, managed Worktree, READY baseline Checkpoint, base commit, final
  workspace fingerprint, capability fingerprint, and grant fingerprint; and
- the ordered exact target set, operation, baseline image, desired image,
  pre-image fingerprint, desired fingerprint, and plan fingerprint.

Only a completed Swarm with a completed writable DAG is eligible. Every source
node must have a preserved terminal lease, a READY baseline Checkpoint, a
managed READY Worktree, and matching durable identities. The live preserved
worker projection is inspected before planning; its canonical fingerprint must
equal both the node and lease final fingerprints. The parent's identity is
read from the active binding and its actual projection, never from model or
worker paths.

## Three-way and path policy

For each changed regular file the plan records one of these operations:

- `CREATE`: baseline absent, desired present, and parent absent;
- `UPDATE`: baseline present, desired differs, and parent equals baseline; or
- `DELETE`: baseline present, desired absent, and parent equals baseline.

Any parent same-path difference is a durable `CONFLICT` before `APPLYING` and
causes zero parent writes. Any overlap between eligible workers' changed
relative paths is rejected before a plan is published. Unrelated parent dirty
paths are preserved. Symlinks, link-like traversal, special files, mode-only
changes, protected Neuro state, checkpoint/worktree storage, credentials and
outside-root paths fail closed in this slice.

The conservative first-slice bounds are at most 8 source workers, 64 target
files, 32 MiB of target images in total, 8 MiB per file image, and 4 KiB per
relative path. The adoption ownership lease is at most five minutes. These
bounds are application constants and are checked again by immutable domain
values when durable state is loaded.

## Parent mutation authority

The service never calls `shutil`, raw file replacement, Git checkout/apply/
cherry-pick, a shell, or a public model tool. It creates one typed
`WorkspaceMutationRequest` per target and calls the mutation port captured from
the active parent binding. The runtime path remains:

```text
canonical filesystem target
  -> PermissionManager / scoped approval
  -> workspace boundary and instruction checks
  -> sandbox/profile check
  -> exact regular-file executor
```

`CREATE` and `UPDATE` may use the existing `WORKSPACE_EDITS` candidate only
when the ordinary canonical target rules produce it. `DELETE` never inherits
that broad workspace candidate; it remains exact-action-or-deny. Explicit
`DENY`, a foreign parent session/root, a model-supplied scope, or a worker
capability cannot authorize adoption. Approval memory remains process-local
and is not persisted or recreated after restart.

## Durable lifecycle and recovery

Session Store schema 29 adds insert-once `result_adoptions` and per-target
`result_adoption_targets` rows. Parent and target transitions use owner
liveness, `BEGIN IMMEDIATE`, immutable identity checks, and generation CAS.
The plan lifecycle is:

```text
CLAIMED -> VERIFIED -> APPLYING -> VERIFYING -> COMPLETED
    \-> CONFLICT / FAILED / INDETERMINATE
```

Each target records `NOT_STARTED`, `APPLYING`, `RETRYABLE`, `APPLIED`,
`CONFLICT`, `FAILED`, or `INDETERMINATE`. Before the target's observable
mutation, its operation, path, expected pre-image, desired image, and
fingerprints are already durable. Recovery observes the actual parent image:

- expected pre-image: retry only through the permission/write boundary;
- desired image: mark `APPLIED` and never rewrite it;
- neither image: mark `CONFLICT` before mutation or `INDETERMINATE` after an
  attempted effect, and never overwrite the third image.

Filesystem mutation is not multi-file atomic. A partial application is
recovered forward one target at a time; if a later target is externally
modified, the adoption becomes `INDETERMINATE` and does not roll back earlier
work. Repeating a completed adoption with the exact identity performs zero
writes and returns the same durable result. A live owner is not stolen; a dead
owner can be taken over only through the durable owner/CAS rules.

Worker Worktrees, READY Checkpoints, leases, DAG rows, and Swarm resources are
never removed, rolled back, merged, committed, copied back by another path, or
cleaned up by this service.

## Consequences and non-goals

The core can safely apply a bounded set of exact regular-file results to the
actual parent checkout while preserving unrelated dirty work and durable
worker evidence. It intentionally does not add automatic Ultracode
integration, model merge, conflict resolution UI, TUI/ACP entrypoints,
checkpoint rollback, cleanup, commit/push, remote execution, recursive Swarm,
or a general writable merge/copy-back engine. Worker result completion remains
distinct from parent mutation completion.

## Validation

Focused tests cover three-way create/update/delete identity, parent/stale/
overlap conflicts, target-level recovery, duplicate adoption, permission
boundaries, schema 28-to-29 migration, and spawned-process A/B/C/D recovery.
A production-shaped composition uses real SQLite, managed Worktrees, READY
Checkpoints, parallel Task DAG workers, and the canonical parent mutation port
in a temporary Git repository; it verifies parent A/C changes, B and unrelated
dirty U preservation, unchanged HEAD, preserved child evidence, third-party
bytes preserved on `INDETERMINATE`, and completed re-entry with zero new
filesystem or durable resource rows. Full repository lock, documentation
parity, lint, formatting, mypy, coverage, and build gates remain required
before this slice can be rated proven.
