# ADR 0144: Automatic Ultracode result adoption integration

- Status: Accepted
- Date: 2026-08-29
- Scope: Neuro Code v1 bounded local vertical slice

## Context

ADR 0141 defines the explicit `ULTRACODE` application entry. It chooses one
bounded local branch: the existing ordinary `MAIN_MAX` path or the existing
`BOUNDED_SWARM` composition. ADR 0143 defines the internal Result Adoption
core, but deliberately leaves the composition seam unwired.

This ADR closes only that seam. It does not redesign Ultracode, Agent Swarm,
Task DAG, Leader, Writable Subagent, Worktree, Checkpoint, Permission, or
Sandbox behavior. It also does not introduce a general merge or copy-back
engine.

## Decision

Only an explicit user turn whose reasoning effort is `ULTRACODE` can enter the
automatic integration. Existing ordinary efforts, including `max`, retain the
ordinary `ConversationRunner` path.

### `MAIN_MAX`

`MAIN_MAX` invokes the existing parent conversation runner and performs zero
Result Adoption construction, reads, plans, target writes, or provider calls.
The normal parent finalization contract remains unchanged.

### `BOUNDED_SWARM`

The bounded branch reuses the existing Planner, Task DAG, Leader, Writable
Subagent, Worktree, Checkpoint, and Agent Swarm services. After the Swarm has
durably produced one canonical terminal `AgentSwarmResult`, the application:

1. Derives one deterministic adoption identity from the durable Ultracode
   execution identity and exact Swarm run identity.
2. Invokes the typed internal Result Adoption service with the exact
   `AgentSwarmResult` and the actual parent `ConversationBinding` mutation
   authority.
3. Requires Result Adoption to reach `COMPLETED` before parent success can be
   published.
4. Persists Ultracode `FINALIZING` with the bounded final response.
5. Commits the parent external turn through the existing exactly-once
   conversation contract.
6. Persists Ultracode `COMPLETED` only after the parent commit succeeds.

The Swarm result is passed as typed durable evidence. Response text, model
instructions, Leader text, worker text, `git diff`, or a model-provided file
list cannot substitute for it. Adoption is an internal application action,
not a model tool call and not a second provider turn.

### Deterministic identity

The adoption ID is:

```text
adopt- + SHA256(execution_id + NUL + swarm_run_id)[:48]
```

It contains no model, Planner, Leader, Worker, timestamp, random UUID, or
latest-row lookup input. Re-entry uses the same exact identity and exact
Swarm result.

### Adoption non-success

`CONFLICT`, `FAILED`, and `INDETERMINATE` are parent-visible bounded outcomes.
The response contains the adoption ID, terminal state, applied/unresolved/
conflict counts, and whether partial parent mutation may have occurred.
The integration never falls back to `MAIN_MAX`, reruns the provider or Swarm,
asks a model to merge, overwrites a conflicting image, or silently claims
success.

### Process-death recovery

The integration preserves the following fresh-process boundaries:

- A: a lower Swarm is `COMPLETED` while Ultracode is still
  `BOUNDED_SWARM_RUNNING`; recovery reads the exact result and resumes the
  same adoption without replaying Planner, Leader, Worker, Swarm, or provider
  work.
- B: adoption is `COMPLETED` while the parent turn is not committed; recovery
  reuses the terminal adoption and commits the parent once, with no adoption
  writes or new plan.
- C: adoption is `INDETERMINATE`; recovery exposes that bounded state without
  overwrite, retry, or Swarm/provider replay.
- D: adoption is `CONFLICT` before mutation; recovery exposes the conflict
  without mutation, a new adoption, or rerun.

Parent commit crash recovery remains owned by the existing conversation
finalization contract. A committed parent turn is reused by exact identity.

### Permissions and progress

Adoption uses the active parent binding's existing workspace mutation,
permission/scoped approval, workspace/instruction, sandbox, and exact-file
pipeline. A fresh process does not reconstruct process-local permission
grants; it re-evaluates the current binding and fails closed when approval is
not available.

The existing `ULTRACODE_DELEGATION_PROGRESS` projection exposes safe stages
such as `swarm_completed`, `adoption_preparing`, `adoption_applying`,
`adoption_completed`, `adoption_conflict`, `adoption_failed`, and
`adoption_indeterminate`, together with bounded identities and counts. It
does not expose raw workspace bytes, patches, secrets, plans, or transcripts.

`SessionTurnService` remains long-lived and dynamically routes ordinary
`max` turns and explicit `ULTRACODE` turns. No service recreation or global
mode mutation is required.

## Consequences

Automatic Ultracode delegation now has one application-owned success path that
can safely update the actual parent workspace from an exact completed Swarm
result. Parent success ordering is explicit, adoption identity is restart
stable, and non-success states remain visible and bounded. Preserved worker
Worktrees, leases, Checkpoints, DAG rows, and Swarm resources are not cleaned
up by this slice.

## Non-goals

This ADR does not add semantic merge or conflict repair, generic retry,
rollback, cleanup, commit or push behavior, remote/cloud execution, persistent
permission grants, public ACP/TUI adoption controls, recursive orchestration,
or a general merge/copy-back engine. It does not change `MAIN_MAX` or the
existing Result Adoption algorithm.

## Validation

Validation includes focused Ultracode, Result Adoption, Agent Swarm, Task DAG,
Writable, permission, crash/conversation recovery, and dynamic TUI tests;
real temporary-Git production-shaped A/B/C/D fresh-process recovery; schema
29 checks; and the repository's complete quality gates.
