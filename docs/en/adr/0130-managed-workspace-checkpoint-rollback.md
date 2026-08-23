# ADR 0130: Managed workspace checkpoint and rollback

- Status: accepted for the first internal vertical slice
- Date: 2026-08-23
- Scope: durable source-state checkpoints and exact rollback of one ready, Neuro Code-owned managed worktree

## Context

Neuro Code has three different meanings of “checkpoint”:

1. `EXECUTION_SEGMENT_CHECKPOINTED` is a bounded long-task progress/audit marker.
2. `session_turn_attempts` records request/output/tool durability for crash and
   `INDETERMINATE` recovery.
3. A workspace checkpoint is an application-owned snapshot of one managed
   worktree’s source projection.

The third capability must not reuse either of the first two stores or their
semantics. A workspace rollback is destructive, must never infer authority from
a raw path, and must not rewrite the source checkout or Git history.

## Decision

Add an explicit internal application capability:

```text
WorktreeHandle
    |
WorkspaceCheckpointApplicationService
    |-- ManagedWorktreeStore       (ownership proof)
    |-- WorkspaceStatePort          (Git index + bounded source projection)
    |-- checkpoints.db              (immutable targets + rollback attempts)
    `-- state_dir/checkpoints/      (atomic content-addressed artifacts)
```

`ApplicationComposition.create_workspace_checkpoint_service()` constructs the
capability, but no model-facing tool, automatic policy, TUI command, LSP
binding, writable subagent, worker coordinator, Relay, DAG, or integration
operation is enabled by this ADR.

### Target authority

`CheckpointCreateRequest` receives a `WorktreeHandle` or `WorktreeId-derived
handle`; it never receives a raw filesystem path. Capture and rollback require
all of the following:

- the durable worktree record is `MANAGED`, `managed=true`, and `READY`;
- the handle, canonical path, repository common directory, source worktree,
  Git directory, branch/detached identity, and Git worktree record match;
- the managed worktree still exists and is not externally locked;
- rollback sees the exact checkpoint HEAD; a moved HEAD returns typed
  `HEAD_MISMATCH` and never rewinds a branch or detached history.

Rollback takes the existing managed worktree only. A source checkout, external
worktree, `ORPHANED` record, `REMOVED` record, reused path, replaced repository,
or mismatched branch is rejected without filesystem mutation.

### Captured projection

The snapshot is a source-controlled workspace projection, not an arbitrary
filesystem image. It contains:

- repository/worktree identity, HEAD, and branch/detached state;
- the exact per-worktree Git index bytes;
- every tracked index path, including absent tracked paths, staged and
  unstaged content, tracked deletions, binary bytes, executable mode, and
  tracked symlink targets;
- every non-ignored untracked regular file or symlink.

Ignored files are deliberately absent from the projection and are never
deleted or restored. Empty directories are not rollback authority. Unmerged
index stages, intent-to-add, sparse/split indexes, submodules, nested
repositories, special files, link-like parents, and unsupported platform
reparse forms fail closed with `UNSUPPORTED_WORKSPACE_STATE`.

The implementation chooses content-addressed files in the state directory plus
a canonical metadata manifest. The raw index is stored separately; regular
file contents are stored as SHA-256 named blobs and symlink targets are stored
as bounded link data without following them. Artifact paths are generated only
from opaque checkpoint IDs and contain relative paths only. No SQLite BLOB is
used for source contents, and no normal Git stash lifecycle is used.

### Fingerprint and integrity

The deterministic fingerprint hashes repository identity, worktree identity,
canonical target path, HEAD, branch/detached state, index digest, and the
sorted tracked/non-ignored manifest with mode, kind, presence, size, and
content/link digest. Equal projections have equal fingerprints; an in-scope
content, mode, index, deletion, path, or identity change changes the
fingerprint. Modification time alone is not fingerprint input.

Capture persists a `CAPTURING` intent before publication, writes bounded
temporary artifacts, closes and fsyncs files, hashes metadata/index/blobs, and
atomically publishes the final checkpoint directory before a CAS transition
to `READY`. A crash leaves either a recoverable `CAPTURING` record or no final
artifact; a partial directory is never promoted to `READY`. Manifest, index,
blob, size, count, and root-integrity checks run before rollback. Corruption,
truncation, replacement, unexpected artifact files, and malformed database
records fail closed.

Hard bounds cover file count, untracked count, single-file size, total source
bytes, manifest bytes, index bytes, artifact bytes, and capture time. The
failure is typed `CHECKPOINT_TOO_LARGE`; no unbounded content is placed in
logs, errors, or UI projections.

### Rollback and crash recovery

`RollbackAttempt` is separate from immutable `WorkspaceCheckpoint` metadata.
The attempt is persisted as `STARTED` before the destructive worktree phase.
The service acquires a unique Neuro Code Git worktree lock with the attempt
identity; an existing external lock is never unlocked. The lock also makes a
concurrent ordinary worktree removal fail closed through the existing
Worktree capability. Only the exact paths observed by the current projection
and absent from the target are unlinked, deepest first. No `git clean`, broad
recursive delete, reset, checkout, restore, stash, branch-ref rewrite, or
history rewind is used. Symlink leaves are unlinked as leaves and are never
resolved to delete their targets.

Tracked/untracked files and modes are restored through the workspace adapter,
then the saved index is atomically replaced. Success requires the actual
post-operation projection fingerprint to equal the target fingerprint;
process exit code zero is not sufficient. The attempt becomes `COMPLETED`
only after verification and lock release. A partial restore, uncertain lock or
index result, artifact issue after start, or failed final fingerprint is
`INDETERMINATE`, never false success.

On restart, durable `STARTED`/`INDETERMINATE` attempts are inspected. A live
owner remains protected; a dead owner can be claimed with CAS and retried only
when the worktree identity, exact HEAD, and artifact integrity still prove
safe. If the target fingerprint is already present, recovery finalizes the
attempt without rewriting source content. An artifact verification failure for
an active attempt is recorded durably as `INDETERMINATE`; recovery performs no
restore and does not release an existing Neuro-owned protective lock. The lock
is released only by an explicit later resolution. Concurrent stale writers
lose by SQLite generation CAS; two rollback attempts for one worktree have one
deterministic durable winner.

The recovery tests exercise real child-process death after exact-leaf effects
and after index replacement, then use the normal `reconcile()` path to converge
to the target fingerprint. Real process races prove that rollback/rollback has
one destructive owner and that rollback/remove containment is enforced by the
Git worktree lock. An uncertainty discovered after the owned lock is acquired
is never terminalized as a clean `FAILED` result.

### Recovery state matrix

| Case | Durable state and observation | Recovery behavior | Result |
| --- | --- | --- | --- |
| A | `STARTED`, no mutation, owner dead, artifact valid | Claim with CAS, prove identity/HEAD/lock, restore and verify | `COMPLETED` |
| B | `STARTED`, files or leaves partially restored, index old, owner dead, artifact valid | Claim with CAS and continue through the idempotent restore path | `COMPLETED` with exact target fingerprint |
| C | `STARTED`, index restored but workspace leaves remain partial, owner dead, artifact valid | Claim with CAS and continue through the idempotent restore path | `COMPLETED` with exact target fingerprint |
| D | `STARTED` or `INDETERMINATE`, artifact corrupt | Persist `INDETERMINATE`; do not restore, mark the artifact healthy, remove the worktree, or release an owned protective lock | Durable `INDETERMINATE` |
| E | Workspace fingerprint already equals target; `COMPLETED` not persisted | Verify without rewriting source content, then release only the exact owned lock | `COMPLETED` |
| F | External lock is present | Fail closed before starting a destructive attempt; never unlock the external owner | Typed `LOCKED` failure |

## Invariants

| Invariant | Status in this slice |
| --- | --- |
| Only a `READY`, managed, identity-proven worktree is a target | PROVEN by service and adversarial tests |
| Capture does not mutate source checkout or managed worktree | PROVEN by real Git tests |
| `READY` checkpoints are immutable and integrity/bound bounded | PROVEN by insert-only/CAS/artifact tests |
| Rollback restores the complete declared projection and verifies fingerprint | PROVEN for staged/unstaged, deletion, binary, mode, symlink, and untracked cases |
| Ignored files are untouched | PROVEN by before/after ignored-file tests |
| No broad clean or arbitrary recursive deletion | PROVEN by exact-leaf adapter design and tests |
| Crash recovery cannot claim false success | PROVEN by real child-process death before and after destructive effects |
| Active rollback artifact corruption becomes durable `INDETERMINATE` | PROVEN by corrupt-after-lock restart/reconcile tests |
| Destructive uncertainty is not terminalized as clean `FAILED` | PROVEN by owned-lock and final-verification failure tests |
| Concurrent writers cannot regress a durable record | PROVEN by SQLite CAS and active-worktree uniqueness |
| Rollback/rollback has one cross-process destructive owner | PROVEN by multiprocessing race tests |
| Rollback cannot race managed removal uncontrolled | PROVEN by multiprocessing rollback/remove race tests |
| A Neuro-owned rollback lock is released only after `COMPLETED` | PROVEN by lock retention/cleanup tests |
| No new model-facing external execution surface | PROVEN by explicit composition-only wiring |
| Source checkout remains unchanged | PROVEN by dirty/source preservation tests |
| Core lifecycle is platform-aware and fail-closed | Linux real-Git path covered; macOS/Windows exact-head CI is required |

## Not implemented

Automatic checkpoint policies, model-facing checkpoint tools, TUI/ACP exposure,
checkpoint deletion/retention, arbitrary filesystem snapshots, ignored-file
rollback, Git history rewind, branch ref reset, patch/commit/merge/
cherry-pick/rebase integration, conflict resolution, checkpoint merge or
diff UI, writable parallel or recursive subagents, automatic LSP worker
binding, Relay/DAG/Leader/Swarm/Ultracode orchestration, and source-checkout
rollback remain outside this slice. The explicit single-child writable
workspace slice is defined separately by
[ADR 0131](0131-managed-writable-subagent-workspace.md).

## Compatibility and validation

The minimum supported Git version remains 2.40.0 because the inherited filter
preflight requires `git check-attr --source=<tree-ish>`. Git 2.39 and older
fail closed at the existing Worktree capability boundary. The checkpoint
capability must pass focused real-Git tests, full pytest with coverage, docs
parity, Ruff, format, mypy, lock/build checks, and exact-head Linux/macOS/
Windows CI before being rated beyond this vertical slice.
