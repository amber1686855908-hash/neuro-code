# ADR 0141 — Automatic Ultracode delegation and orchestration entry

[简体中文](../../zh-CN/adr/0141-automatic-ultracode-delegation.md) · **English**

## Status

Accepted for the first bounded vertical slice.

## Context

`max` is Neuro Code's deepest ordinary single-agent reasoning/review policy.
The existing `ultracode` value previously projected to that policy only; it
did not own a workflow. The repository now has a frozen bounded Agent Swarm,
but its composition service is intentionally an internal application seam and
must not be duplicated or exposed as an unrestricted orchestrator.

The first Ultracode slice therefore needs to add one explicit application
entry while preserving ordinary effort behavior, provider neutrality, exact
turn recovery, and the authority boundaries of the existing Conversation,
Planner, Leader, Task DAG, Writable, Worktree, Checkpoint, Relay, and LSP
services.

## Decision

Only a user turn whose requested effort is `ULTRACODE` enters the Ultracode
delegation service. Ordinary `low`, `medium`, `high`, `xhigh`, and `max` turns
remain on the existing normal ConversationRunner path. The entry uses a small
deterministic local policy and makes exactly one typed decision:

- `MAIN_MAX`: run the existing parent single-agent runtime with ordinary
  `max` semantics;
- `BOUNDED_SWARM`: invoke the existing
  `ApplicationComposition.create_agent_swarm_service()` once.

The policy has no model classifier call. It cannot select tools, workers,
capabilities, roots, sandbox, network, MCP, DAG definitions, retry, merge,
checkpoint behavior, or provider credentials. It is an application strategy,
not a provider-native reasoning level.

The current policy is deliberately only a bounded deterministic heuristic: it
matches a fixed set of parallel, decomposition, cross-file, and research
markers in the user prompt. It is not semantic task-complexity classification
and it does not claim model-level routing intelligence. The interactive TUI
binds the delegate as a dormant application entry regardless of the initial
effort; `SessionTurnService` checks the controller's current effort at each
user turn, so runtime `max` ↔ `ultracode` switching does not rebuild the
service or leave a stale entry seam.

## Durable identity and lifecycle

Session schema 28 adds the insert-once
`orchestration_ultracode_executions` projection. Its immutable identity binds:

- the actual parent session and exact parent turn;
- input and context fingerprints;
- provider, model, and context-affinity provenance;
- one `MAIN_MAX` or `BOUNDED_SWARM` decision; and
- one downstream identity: the exact parent turn execution for `MAIN_MAX`, or
  a deterministic `swarm_run_id` for `BOUNDED_SWARM`.

The state machine is:

```text
DECIDED -> MAIN_MAX_RUNNING -> COMPLETED
DECIDED -> BOUNDED_SWARM_RUNNING -> FINALIZING -> COMPLETED
                                      \-> INDETERMINATE
                         any owned branch \-> INDETERMINATE
```

SQLite `BEGIN IMMEDIATE`, process-liveness ownership, immutable identity
checks, and generation CAS make the decision and its owner recoverable. A
fresh process reuses the exact durable decision; it never classifies the same
prompt again and never creates a replacement downstream identity.

## Branch and result boundaries

`MAIN_MAX` calls the existing parent `ConversationRunner` with the exact
`turn_id` and `ultracode_execution_id`. The provider projection may be native
`max` where an explicit adapter supports it, or may omit that field as before;
the provider never receives a fabricated `ultracode` value.

`BOUNDED_SWARM` calls the existing bounded Swarm with the one durable
`swarm_run_id`. The router does not create a second Planner, Leader, Task DAG,
Writable worker, Worktree, Checkpoint, LSP manager, or relay. Lower-layer
progress is not copied into the parent transcript; only bounded delegation
progress and the final Swarm response are parent-visible.

Both branches use the existing external-turn finalization contract to append
one parent-visible assistant result. Recovery matches a committed result only
by exact `(session_id, turn_id, ultracode_execution_id)` event evidence. An
exact committed result is idempotent and cannot append a second assistant
message.

## No double execution and recovery

The router never starts both branches and never falls back from a failed or
indeterminate branch to the other branch. A parent attempt with observable
output is reused and never replayed. If a lower Swarm has already published its
exact durable identity, recovery may continue that same Swarm; an open parent
attempt without that lower identity fails closed. A lower terminal result or a
parent result observed before Ultracode bookkeeping is finalized is promoted
through `FINALIZING` and committed idempotently.

The raw durable-state fresh-process matrix uses
`multiprocessing.get_context("spawn")` and `os._exit` for these boundaries:

- A: durable decision before the downstream branch starts;
- B: observable `MAIN_MAX` output before Ultracode completion;
- C: an exact durable Swarm run before the Ultracode branch link advances;
- D: a completed Swarm result before the parent assistant commit; and
- E: a committed parent result before Ultracode terminal bookkeeping.

Each case proves no decision replay, no branch switch, no second provider
execution, no duplicate Swarm identity, and one parent-visible assistant
result. This matrix exercises the durable lifecycle seams directly; it is not
itself a full `ApplicationComposition` process-death proof. Two separate
production-composition acceptances cover the MAIN_MAX and BOUNDED_SWARM
boundaries through fresh compositions and the real downstream paths.

## Security, compatibility, and non-goals

The actual parent `ConversationBinding` remains the capability ceiling. The
entry has no filesystem, Bash, LSP, MCP, network, Worktree, Checkpoint, or
Writable authority. Routing text is evidence only and cannot mutate any of
those boundaries. CLI and TUI may enter this internal service; ACP does not
gain an effort surface in this slice.

This slice does not add automatic Swarm for ordinary efforts, recursive
Ultracode or Swarm, generic retry, result adoption, merge/copy-back,
cherry-pick, patch adoption, public Swarm dashboards, remote/cloud execution,
marketplace integration, or a new Checkpoint/Rollback implementation.

## Validation

Focused and production-shaped tests cover both branches, provider-wire
neutrality, insert-once/generation-fenced persistence, schema 27-to-28
migration, exact replay, cancellation/failure no-fallback behavior, the raw
fresh-process A–E matrix, dynamic effort switching through one long-lived turn
service, and two representative full-composition process-death acceptances:
one MAIN_MAX result handoff and one completed Swarm handoff. The repository's
full lint, type, documentation, coverage, build, and regression gates remain
required before rating this slice proven.
