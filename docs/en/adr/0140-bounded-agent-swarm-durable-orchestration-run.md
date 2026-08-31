# ADR 0140: Bounded Agent Swarm / Durable Orchestration Run

- Status: implemented as an explicit internal P0 vertical slice; final validation is recorded by PR #67 CI, and live/paid provider validation remains out of scope
- Date: 2026-08-27
- Scope: one bounded Planner → Leader → Task DAG → Writable worker orchestration run with at most one existing DAG Replan successor
- Depends on: ADR 0131, ADR 0132, ADR 0133, ADR 0134, ADR 0135, ADR 0136, ADR 0137, ADR 0138, and ADR 0139

## Context

The repository now has separately proven Planner, parallel-aware Leader, Task
DAG, predecessor-result Relay, Writable Subagent, worker-scoped LSP,
Worktree, Checkpoint, and bounded Replan capabilities.  They intentionally
remain separate authority owners.  A bounded multi-agent user capability needs
one durable orchestration identity without becoming a second implementation of
any of those services or a general-purpose scheduler.

## Decision

Add one internal `BoundedAgentSwarmApplicationService` composed at
`ApplicationComposition`.  It owns only the parent-bound Swarm run identity,
its lifecycle summary, and exact Planner/DAG lineage.  The authority chain is:

```text
actual parent ConversationBinding
        -> durable Swarm run
        -> existing zero-tool model Planner
        -> existing TaskDagApplicationService
        -> existing parallel-aware Leader
        -> existing Writable workers, relays, Worktrees, Checkpoints, and LSP
        -> existing bounded DAG Replan, at most once
        -> existing Leader final synthesis projection
```

The composition root creates each lower-layer service through its existing
factory.  The Swarm does not create tools, sessions, worktrees, checkpoints,
LSP managers, relays, or worker runtimes directly.  At this ADR's acceptance,
it was not connected to CLI, TUI, ACP, Ultracode, automatic delegation, or a
public orchestration protocol. Subsequent ADRs 0141 and 0144 add one bounded
explicit Ultracode composition seam; they do not make the Swarm public or give
it direct tool or workspace authority.

## Durable identity and lifecycle

Schema 27 adds the insert-once `orchestration_swarm_runs` projection.  One row
binds the bounded run ID, actual parent session, redacted objective
fingerprint, deterministic Planner ID, owner PID/token/lease, generation,
Planner session/turn and proposal fingerprints, root/current DAG identity and
generation, optional Replan revision/successor, and the bounded terminal
response/result fingerprint.  Parent, Planner, root/current DAG, and successor
references use foreign-key `RESTRICT` so recovery history cannot be deleted
behind a run.

The Swarm lifecycle is:

```text
CLAIMED -> PLANNING -> PLANNED -> EXECUTING
                         |            |
                         |            +-> REPLANNING -> EXECUTING (once)
                         |            +-> FINALIZING -> COMPLETED
                         |            +-> FAILED / INDETERMINATE
                         +-> INDETERMINATE
```

`FAILED` is reserved for a failed successor DAG after the single Replan has
already been consumed.  Provider, storage, ownership, cancellation, or
uncertain lower-layer boundaries become `INDETERMINATE`; they never become a
retryable failed source.  State updates use generation CAS and the same live
owner/process-liveness rules as the existing durable controllers.  A live or
unproven owner is not stolen.  A dead owner can be taken over once with a new
generation and exact identity.

The terminal response is the existing Leader final response, redacted and
bounded to 16 KiB.  Its fingerprint covers the Swarm ID, current DAG ID and
generation, immutable DAG definition fingerprint, and response.  A fresh
controller returns the stored result after `COMPLETED`; it does not create a
Planner, Leader, worker, or provider call.

## Normal parallel path

The production-shaped acceptance path uses a model-generated bounded graph

```text
    A
   / \
  B   C
   \ /
    D
```

with `max_parallel=2`.  The existing Leader publishes `SELECT_NODE(A)`, then
`SELECT_NODES(B,C)`, then `SELECT_NODE(D)`, and finally `FINALIZE`.  B and C
are separate Writable executions with separate managed Worktrees, baseline
Checkpoints, child sessions, Parent Relays, predecessor-result Relay entries,
and worker-scoped LSP managers.  D is not claimed until both declared
predecessors are complete, and receives only the existing deterministic
predecessor-result projection.  The parent checkout is never used as a shared
writable workspace and remains unchanged.

The Swarm does not widen a worker's scheduler or authority.  Existing
capability intersection, sandbox, filesystem, Worktree, Checkpoint, Parent
Relay, result Relay, and LSP boundaries remain authoritative.  No raw
transcript, hidden reasoning, provider request, tool argument, environment,
credential, checkpoint blob, workspace content, or authority instruction is
stored in the Swarm projection or passed as Swarm context.

## Replan path

When the current source DAG is `FAILED`, quiescent, fully terminal, and has no
indeterminate node, the Swarm enters `REPLANNING` and invokes the existing ADR
0139 service with one deterministic revision ID.  The source definition and
runtime projection are checked before and after the call and remain immutable.
The existing Replan service enforces `MAX_DAG_REPLAN_DEPTH=1`, publishes one
new immutable successor identity, and preserves its own no-provider-replay
contract.  The Swarm verifies exact source, evidence, proposal, revision,
successor, parent, and depth lineage before returning to `EXECUTING`.

After a successor fails, the Swarm becomes terminal `FAILED`.  It never
replans an `INDETERMINATE` or cancelled DAG, resurrects source nodes, retries
a provider or worker, or creates a second successor.

## Crash recovery and controller races

The durable Swarm row is inserted before the Planner is invoked.  Recovery
always delegates child recovery to the existing Planner, Leader, Task DAG,
Writable, Relay, Worktree, Checkpoint, LSP, and Replan contracts.  The Swarm
itself only reconciles the durable phase and exact identity.  The focused
composition tests cover the meaningful boundaries: identity before provider
execution, Planner/DAG publication, a live parallel wave, completed DAG before
finalization, failed source before Replan, successor execution, terminal
result reuse, and a spawned controller that dies after the initial durable
claim.  Fresh finalization recovery also proves that no lower factory is
called when the terminal result is already durably pending.

The representative fresh-process recovery matrix in
`tests/test_agent_swarm_process_recovery.py` uses `multiprocessing` spawn and
explicit durable state markers at four Swarm handoffs.  It proves: a completed
Planner attempt/proposal/DAG recovered before the Swarm `PLANNED` transition;
a terminal lower Leader/DAG result recovered before `FINALIZING`; a completed
Replan successor recovered before the Swarm current-DAG switch while the
failed source remains immutable; and a durably persisted `FINALIZING` result
recovered before `COMPLETED`.  Each L1 process exits with `os._exit`, and a
fresh `ApplicationComposition` L2 verifies exact run, Planner, DAG, Replan,
result, provider-call, and managed-resource identities without replay.  This
is a representative bounded matrix, not a claim about every arbitrary kill
timing or live/paid provider behavior.

Two controllers claiming one Swarm ID use SQLite `BEGIN IMMEDIATE`, insert-once
identity, process-liveness ownership, and generation CAS.  Exactly one
controller can own the active row.  The loser performs no provider call, DAG
publication, worker allocation, or terminal-result mutation.  Observable
provider-turn uncertainty is fail-closed and is never replayed.

## Cancellation and bounds

Cancellation while a Swarm phase is owned records `INDETERMINATE` through a
shielded durable transition.  If a lower component has uncertain side effects,
the Swarm cannot reinterpret that uncertainty as a safe Replan.  Existing
Task DAG node, parallelism, relay, worker, and Replan limits are reused.  The
Swarm adds no unbounded queue, recursive graph, recursive Replan, generic
retry, or hidden orchestration step counter.

## Non-goals

At this ADR's acceptance, it did not add automatic Ultracode delegation,
user-facing Ultracode behavior, recursive Swarms, unbounded agents, generic
retry, shared writable worktrees, merge, cherry-pick, copy-back, patch adoption,
public CLI/TUI/ACP orchestration, remote/cloud execution, marketplace
integration, or a new Checkpoint/Rollback implementation. Checkpoint and
Rollback remain existing capabilities and authority owners. Subsequent ADRs
0141 and 0144 add only the bounded explicit Ultracode composition described
above.

## Verification

The slice adds deterministic event/barrier synchronization to the frozen
Planner race preflight, production-shaped normal and Replan paths, durable
Swarm domain/store tests, SQLite migration/FK/tamper/CAS tests, the
four-boundary fresh-process recovery matrix, an active-controller race, and a
composition-level authoritative `INDETERMINATE` no-Replan test.  Full
repository quality gates and PR #67 merge-ref CI are the release evidence;
this ADR does not claim arbitrary crash-point coverage, live-provider, or
public-interface support.
