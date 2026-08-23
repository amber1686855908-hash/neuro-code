# ADR 0131: Serialized managed writable-subagent workspace

- Status: implemented as an explicit internal vertical slice; final rating waits for exact-head CI
- Date: 2026-08-23
- Scope: one bounded writable child, one Neuro-owned managed worktree, and one preserved baseline

## Context

The existing `/subagent` workflow, CLI, TUI, and ACP paths are intentionally
read-only. A writable child must not turn a boolean such as `allow_writes` or a
raw path into authority, inherit the parent conversation, or mutate a dirty
source checkout. The first writable capability therefore needs a narrow,
explicit boundary that can be inspected after success, failure, cancellation,
or process death.

## Decision

Add an internal `WritableSubagentApplicationService` that is constructed by
`ApplicationComposition.create_writable_subagent_service()`. It is not wired
into `/subagent`, CLI, TUI, ACP, automatic scheduling, LSP workers, or any
checkpoint/rollback orchestration. Calls are serialized in-process and the
SQLite lease provides the cross-process active-parent and active-worktree
uniqueness boundary.

The service derives a typed `ManagedChildWorkspaceGrant` only after it has:

1. read the parent repository identity and exact committed HEAD;
2. inserted an `ALLOCATING` lease;
3. created a `MANAGED_BRANCH` worktree at that exact SHA outside every parent
   workspace root; and
4. captured a `READY` baseline workspace checkpoint.

The grant binds the grant ID, parent capability fingerprint, parent root and
repository identity, exact base SHA, immutable `WorktreeHandle`, managed
worktree ID, canonical child root, creation time, and baseline checkpoint ID.
The child gets a fresh session and fresh binding whose cwd and sole workspace
root are exactly the managed worktree.

### Child capability

The effective child tool set is the intersection of the explicit parent and
global policy, with only these names in scope:

- read: `read_file`, `read_files`, `list_dir`, `list_tree`, `glob`, `grep`,
  `grep_many`, and `skill`;
- write: `search_replace` and `apply_patch`.

Bash, terminal, background tasks, MCP, network, Git/worktree/checkpoint/
rollback, subagent, and LSP authority are not granted. The parent and global
policy must both expose the two write tools, filesystem write authority, and a
writable sandbox profile. The generic
`SubagentCapabilitySet.is_subset_of()` semantics remain unchanged; the typed
grant is the only boundary that replaces inherited workspace-root authority.

The normal Permission → canonical filesystem target → execution → sandbox
pipeline still runs for every child write. No child tool receives a raw grant
path as a substitute for those checks.

### Parent authority and session lifetime

The composition root accepts only the actual active `ConversationBinding`.
The writable service captures that binding's runner session ID and immutable
`SubagentCapabilitySet`; a request may repeat the session ID only as an
identity check, and a mismatch is rejected before lease, task, worktree, or
checkpoint allocation. A caller-reported capability manifest is not an
authority input. The service also holds the binding while the capability is
live, so the parent session and capability fingerprint used in the lease come
from the same binding that opened the conversation.

Writable leases are durable workspace evidence, not disposable session
metadata. Session-store schema 16 rebuilds the lease table from schema 15 and
preserves populated rows while changing both parent and child session foreign
keys to `RESTRICT`. Before deleting a session, `delete_session()` computes the
full recursive parent/child deletion closure and refuses the operation when
any lease references any session in that closure. This preserves the session,
lease, managed worktree, and baseline checkpoint until a future explicit
workspace-resolution capability is implemented.

### Lifecycle and preservation

The durable lease uses `ALLOCATING`, `WORKTREE_READY`, `BASELINE_READY`,
`ACTIVE`, `PRESERVED`, `ORPHANED`, and `FAILED`. Lease identity is immutable;
transitions are insert-only/CAS with a generation. A child result or failure
never automatically removes the worktree, rolls back the baseline, merges,
commits, copies files back, or deletes the preserved checkpoint. A failure in
proving the final workspace is `ORPHANED`, not a false clean success.

Reconciliation inspects the managed worktree and baseline checkpoint, verifies
identity/state, and classifies dead owners or missing evidence without deleting
uncertain data. It is separate from `session_turn_attempts` and does not infer
workspace recovery from an execution attempt.

Owner liveness is shared by checkpoint and writable reconciliation. POSIX uses
the non-mutating `kill(pid, 0)` probe. Windows opens the process with limited
query/synchronization rights, observes its zero-time wait state, and closes the
handle; only a signalled process or a proven missing PID is classified dead.
Access-denied and unexpected API results remain conservatively alive. No
reconciliation path kills, reclaims, or deletes data as part of this probe.

### Result projection

The caller receives only a bounded, redacted projection containing parent task
and child session IDs, terminal status, response, steps/outcome, worktree ID,
baseline checkpoint ID, exact base SHA, capability/grant fingerprints, final
workspace fingerprint, changed/count metadata, and truncation. It contains no
full diff, transcript, raw tool arguments, credentials, or raw file contents.

## Not implemented

Writable parallel workers, recursive writable subagents, automatic delegation,
CLI/TUI/ACP exposure, automatic checkpoint/rollback policy, rollback after a
child run, merge/commit/patch integration, copy-back, branch deletion, and
checkpoint or worktree cleanup remain explicit future capabilities.

The capability requires Git 2.40.0 or newer through the existing Worktree
filter preflight contract. Git 2.39.5 is unavailable and Git 2.40.0 is
accepted at the boundary; older Git fails closed at initialization.

## Validation boundary

The slice is accepted only when focused writable tests, full local validation,
and the exact-head Linux/macOS/Windows/package CI are green. Before that point
the implementation must be described as partial and not as a completed
product-level writable orchestration capability.
