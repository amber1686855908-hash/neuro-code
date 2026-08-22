# ADR 0129: Application-owned managed Git worktree capability

- Status: accepted for the first local lifecycle slice
- Date: 2026-08-22
- Scope: local Git worktree creation, ownership, inspection, reconciliation, and safe removal

## Context

Neuro Code already has canonical filesystem identity, workspace target
resolution, per-binding sandbox construction, repository instruction
discovery, and workspace-scoped LSP ownership. It did not yet have a typed
identity for a Git repository or a durable owner for linked worktrees. A raw
path is insufficient: a main checkout and a linked worktree have different
workspace roots but share Git common metadata, and a `.git` entry may be a
file rather than a directory.

The source checkout may also be dirty. Creating a worker worktree must not
stash, commit, clean, copy, or otherwise rewrite those changes.

## Decision

Add one application-owned capability with this boundary:

```text
ApplicationComposition
        |
WorktreeApplicationService
        |
typed GitWorktreePort + ManagedWorktreeStore
        |
LocalGitWorktreeAdapter + SQLite worktrees.db
        |
isolated managed worktree
```

The service is explicit and is not exposed as an arbitrary Git command tool.
`ApplicationComposition.create_worktree_service()` wires the service to the
configured state directory; callers must initialize it before use. The same
configured state directory deterministically owns both the managed root and
`worktrees.db`, so separate `ApplicationComposition` instances reopen the
same ownership history. Session cleanup does not own or delete this database.

### Domain and ownership

The domain exposes immutable `WorktreeId`, `WorktreeRepositoryIdentity`,
`WorktreeCreateRequest`, `WorktreeSnapshot`, `WorktreeHandle`, lifecycle
states, and `WorktreeWorkspaceBinding`. A snapshot records the canonical Git
common directory, source worktree, Git directory, repository HEAD observed at
creation, exact base commit, managed path, branch mode, ownership, state, and
creation metadata. Raw Git porcelain dictionaries never cross the adapter
boundary.

The managed root is `<state_dir>/worktrees/<repository-id>/<worktree-id>`.
The service rejects overlap with the source checkout, option-like/invalid
branch names, existing IDs, existing target paths, and existing branches. The
default managed branch namespace is `neuro/worktree/<id>`.

### Git contract

The adapter submits an argv-safe `SandboxedProcessRequest` to the canonical
`LocalProcessSandbox` port; it does not create subprocesses directly or use a
shell. Terminal prompting is disabled, Git commands are local-only, and
stdout/stderr, time, cancellation, and child termination are bounded. It uses
`git rev-parse` for repository and immutable base-commit identity,
`git check-ref-format` for branch validation, and
`git worktree list --porcelain -z` for NUL-safe typed parsing. It never calls
fetch, pull, push, clone, or prune. Git 2.30 or newer is required because
revision resolution uses `rev-parse --end-of-options`; older versions fail
closed.

### Creation

Creation first resolves `base_revision^{commit}` to an immutable commit SHA and
persists `CREATING`. Git then creates either an exact detached worktree or a
new managed branch at that SHA. The service verifies path, repository common
directory, HEAD, and branch identity before persisting `READY`. A dirty source
checkout is not read as a patch and is not changed.

### Removal

Removal requires a durable managed record, matching repository identity,
canonical path, expected HEAD, expected branch/ref, and an actual Git record.
`git worktree remove` is used without `--force`. Dirty and locked worktrees
return typed failures and remain owned. Unknown, moved, missing, or mismatched
paths are never removed with `rm -rf`. Managed branches are retained after
worktree removal; branch deletion is a separate future capability.

### Persistence and reconciliation

`worktrees.db` has its own versioned schema and is not mixed with session turn
recovery. Its schema version is checked on reopen and unsupported versions fail
closed; schema upgrades remain an explicit future migration. SQLite and Git
metadata are not treated as one transaction:

| Durable state | Actual Git state | Classification | Action |
| --- | --- | --- | --- |
| `CREATING` | exact worktree exists | ready | verify and promote to `READY` |
| `CREATING` | absent | failed | retain record as `FAILED` |
| `CREATING` | unrelated path exists | orphaned | retain and fail closed |
| `READY` | exact worktree exists | ready | refresh bounded status |
| `READY` | missing or mismatched | orphaned | retain ownership record; no deletion |
| `REMOVING` | missing after removal intent | removed | promote to `REMOVED` |
| `REMOVING` | exact worktree remains | ready | reconcile failed removal |
| any active state | repository missing or common-dir mismatch | orphaned | no filesystem cleanup |

Reconciliation is explicit and is also used by managed list/inspect calls.
Process death between durable intent, Git action, and finalization is
therefore recoverable without claiming cross-system ACID semantics.

### Workspace, sandbox, and LSP seam

`WorktreeWorkspaceBinding` derives one canonical primary root and no inherited
additional roots from a ready immutable handle. The same binding can be passed
to the existing filesystem target resolver, sandbox factory, and future
workspace-scoped LSP manager. This slice does not create writable subagents,
does not share source document caches, and does not implement integration.

## Invariants

| Invariant | Status |
| --- | --- |
| Neuro Code removes only provably owned worktrees | PROVEN by service guards and removal tests |
| Source dirty state is preserved | PROVEN by real Git integration test |
| Repository/path/base identity is immutable and checked | PROVEN for creation/removal paths |
| Git execution is argv-safe, bounded, and local-only | PROVEN by adapter implementation and parser tests |
| SQLite intent and Git state reconcile after process death | PROVEN by the real child `os._exit()` test |
| Dirty, locked, mismatched, and unmanaged worktrees are never force-removed | PROVEN for dirty/locked/path-reuse cases; mismatch is fail-closed |
| Worktree is an independent workspace root | PROVEN by canonical filesystem binding integration |
| No implicit network Git operation | PROVEN by the local command allowlist |

## Not implemented

Checkpoint/rollback, patch or commit integration, merge/cherry-pick/rebase,
conflict resolution, automatic branch deletion, dirty-state copying, model
facing Git/worktree tools, writable subagents, relay/DAG/leader/swarm, and
automatic Ultracode delegation remain outside this ADR.

## Validation

The focused real-Git suite covers porcelain parsing, typed domain validation,
SQLite reopen round trips, detached and managed-branch creation, dirty source
preservation, branch collision, locked/dirty removal refusal, canonical
workspace binding, path reuse, remove failure, timeout/output bounds, and
process-death reconciliation for both creation and removal. Full local
repository validation remains required before publication.
