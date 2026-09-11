# ADR 0158: Turn workspace checkpoint and undo

- Status: Accepted
- Date: 2026-09-11
- Scope: B1 latest-only normal-turn workspace undo

## Context

The existing checkpoint engine safely captures and restores the Git-visible
projection of a Neuro Code-owned managed worktree, but a normal user run uses
the source checkout. A raw path cannot be used as checkpoint authority and the
source checkout must not be inserted into the managed-worktree ownership
store. B1 needs one latest-only, durable undo target for eligible normal-turn
mutations without changing Git history or turn recovery.

## Decision

Bootstrap issues a typed `SourceWorkspaceCheckpointGrant` only after proving
the canonical repository identity, source path, current HEAD, and branch or
detached state through the existing Git port. The checkpoint application
service re-proves that grant before capture and rollback. Managed worktree
handles retain their existing ownership proof.

`TurnWorkspaceCheckpointCoordinator` creates one checkpoint after permission
approval and immediately before the first bounded primary-workspace mutation.
Eligible later mutations in that turn reuse it. Read-only and denied
operations create none. An unbounded, ignored, unsupported, or unprovable
mutation first appends a durable `UNAVAILABLE` association; if that write
fails, the mutation is rejected. The association is a bounded
`WORKSPACE_UNDO_STATE` session event with latest-only `AVAILABLE`,
`UNAVAILABLE`, and `ROLLED_BACK` states. No checkpoint stack or user-facing
checkpoint ID is added.

The protected image is the existing projection: tracked and non-ignored
untracked file content, staged/index bytes, binary files, supported symlinks,
and supported modes. Ignored files, external side effects, nested repositories,
submodules, special files, empty directories, and arbitrary external-process
changes are out of scope. Undo is an idle-only user action exposed by TUI
`/undo` and CLI `sessions undo <SESSION_ID>`; it does not invoke the model or
terminate live mutators. A successful rollback is verified by the existing
fingerprint state machine and produces one verification mutation handoff for a
future normal turn. It does not alter historical turn recovery or create a
second verification generation owner.

## Recovery and compatibility

After restart, an `AVAILABLE` association is eligible only after the exact
source grant, Git identity, HEAD, and projection safety checks pass.
`UNAVAILABLE` and `ROLLED_BACK` remain terminal. A rollback guard is durable
before restore begins, so process death or association-write failure cannot
cause a second destructive rollback. `MODEL_OUTPUT_STARTED`, turn recovery,
and committed assistant history remain separate facts. The existing managed
checkpoint API, provider/tool contracts, SQLite session schema, and ACP
protocol are unchanged; no model-visible undo tool is added.

## Validation

Focused tests cover typed source authority, dirty/index/untracked/binary/
symlink checkpoint behavior, latest-only association and invalidation,
concurrent first-mutation preparation, persistence failure, restart, rollback
guarding, CLI/TUI projections, live-mutator refusal, and existing managed
checkpoint/recovery regressions.
