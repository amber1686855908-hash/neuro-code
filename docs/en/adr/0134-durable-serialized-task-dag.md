# ADR 0134: Durable bounded-parallel Task DAG

- Status: PROVEN within current vertical-slice scope
- Date: 2026-08-24
- Scope: one bounded, caller-defined DAG whose nodes reuse the existing Writable Subagent pipeline; `max_parallel` is 1..4

## Context

ADRs 0129-0133 establish the managed Git worktree, workspace checkpoint,
writable child, worker-scoped read-only LSP, and bounded Parent Context Relay
contracts. They deliberately do not provide orchestration. A first DAG slice
must add durable dependency control without creating a second worker runtime,
transferring authority, or implying autonomous delegation. This slice now
allows bounded concurrency only between independently owned DAG workers.

## Decision

Add a typed internal `TaskDag` domain contract and an explicit application
service. The caller supplies the complete node definitions; the parent session
is always taken from the actual `ConversationBinding`. The first slice accepts
at most eight nodes, sixteen dependency edges, four dependencies per node, and
only `WRITABLE_SUBAGENT` nodes. Node IDs, prompts, definitions, and diagnostic
metadata are bounded and safe. Unknown references, duplicate edges,
self-dependencies, duplicate node IDs, and cycles are rejected before
publication.

Topological order and ready-node selection are deterministic by declaration
ordinal and node ID. Dependencies are control-only. A predecessor does not
forward its prompt, transcript, reasoning, tool output, response, or workspace
contents to a successor.

`max_parallel` is an application-internal immutable DAG definition field. It
defaults to one and is bounded by the shared `MAX_SUBAGENT_PARALLELISM` limit
of four. The durable running set is derived from node rows whose state is
`RUNNING`; the legacy `active_node_id` column is retained only as a
serialized-Leader compatibility projection and is never a capacity or
scheduling authority. Each claimed node also records the process owner
PID/token. A live owner prevents another controller from mistaking the short
pre-`SessionTask`/lease allocation window for a crash; a dead owner enters the
existing per-node fail-closed recovery classification.

## Existing owners remain authoritative

Every executable node reuses the existing `SessionTask` and
`WritableSubagentApplicationService`. The DAG adds only an internal execution
identity containing the DAG ID, node ID, and a generated parent task ID. The
node stores that exact task ID before invoking the worker. The existing
Writable service continues to own capability intersection, managed Worktree,
baseline Checkpoint, child session, SubagentLink, Parent Relay, model/runtime,
workspace preservation, and worker-scoped LSP.

The DAG service does not use `SubagentScheduler.run_many()`, does not create a
second writable implementation, and does not expose Bash, terminal, network,
MCP, Git, checkpoint, rollback, or recursive-subagent authority.

## Durable publication and bounded claim

Schema 18 adds `task_dags` and `task_dag_nodes`. Definitions are insert-only;
an existing DAG ID is accepted only when its immutable definition fingerprint
matches. Graph and node lifecycle mutations use generation CAS.

Before a worker starts, the service deterministically selects the first ready
slice by ordinal and node ID. For each candidate, one `BEGIN IMMEDIATE`
transaction reloads the DAG, counts canonical `RUNNING` node rows, checks the
immutable `max_parallel` capacity, verifies exact node generation/state, and
atomically changes `READY` to `RUNNING` while persisting the parent task and
process owner identity. The same transaction advances the graph generation.
SQLite therefore owns the cross-process capacity race; no process-local lock,
semaphore, timestamp, latest-row, prompt, or lease guessing is authoritative.

Finishing a node atomically writes its terminal projection and derives the
legacy `active_node_id` projection only when exactly one node remains
`RUNNING`; it advances the graph generation without requiring a scalar active
node. The canonical active execution model is the durable `RUNNING` node set,
so multiple controllers cannot create more than `max_parallel` workers.

The application uses a structured `TaskGroup` for one claimed batch. A
`TaskDagWritableWorkerFactory` supplies a fresh Writable application service
for each claimed node; the frozen per-worker Writable lock remains intact.

The node projection records only bounded lifecycle and workspace identities:
parent task, child session, lease, Worktree, baseline Checkpoint, Relay,
fingerprints, changed-file count, and a bounded response preview. A completed
node requires exact lease and Relay evidence; missing or inconsistent success
correlation is `INDETERMINATE`.

ADR 0136 adds the bounded predecessor-result relay without changing this
execution authority. Session schema 24 retains the schema-20 relay, the
schema-21 recovery fence, bounded DAG capacity, per-node execution-owner
fields, and the parallel-aware Leader decision projection. After a dependent
node's exact `RUNNING` generation is claimed and before its child runtime
starts, the DAG service publishes an insert-only schema-20 projection
containing only completed direct predecessors, in declaration order. The
projection is redacted and bounded to
4 KiB per result, 16 KiB of source result text, and 24 KiB rendered context;
it is bound to predecessor worker/lease/workspace/checkpoint/Parent Relay
identity and cannot carry authority. The relay is a separate context channel,
not a change to the dependency state machine.

## Dependency and failure semantics

The node lifecycle is:

```text
PENDING -> READY -> RUNNING -> COMPLETED
                              -> FAILED
                              -> CANCELLED
                              -> INDETERMINATE
PENDING/READY -> SKIPPED
```

All dependencies completed means `READY`. A failed, cancelled, skipped, or
indeterminate dependency makes the dependent `SKIPPED` with a bounded reason;
an independent branch remains eligible. The graph becomes `COMPLETED` only
when every node completed, `CANCELLED` when cancellation is explicit, and
otherwise `FAILED` after all reachable nodes are terminal. A graph with
missing reachable progress is `INDETERMINATE`.

## Crash and recovery

Reconciliation first looks up the exact parent session/task and exact lease for
the active node. For a dependent node with no task and no lease, it then
read-loads the exact predecessor-result relay and recovery claim. A live or
unproven claim owner is classified as `RECOVERY_OWNED`; reconciliation does not
start Writable or mutate the DAG. Only a later execution step may insert the
exact durable recovery claim, and only its winner may begin Writable. A
running node whose persisted execution owner PID is live is likewise observed
without allocating, failing, or replaying it; this closes the pre-evidence
window shared by independent controllers. A dead execution owner continues
through the per-node crash classification below. A
completed, failed, or cancelled `SessionTask` maps to the same DAG node
meaning. Missing task/lease evidence without an exact safe recovery boundary,
an orphaned or uncertain lease, or an invalid correlation maps to
`INDETERMINATE`. No worker is automatically rerun and no workspace is
deleted, rolled back, merged, copied back, or cleaned up.

The recovery claim binds the parent session, DAG/node definition fingerprints,
exact node generation, parent task, and relay ID plus source/content/integrity
fingerprints. Its unique execution key is independent of node generation
updates. A live owner is never stolen. If the owner is proven dead before the
first Writable lease insert, a fresh controller takes over the same claim by
version CAS; after lease ownership begins, existing Writable reconciliation
remains fail-closed and no automatic rerun is permitted.

Cancellation durably finishes the active node and marks remaining pending or
ready nodes cancelled before re-raising cancellation. A process that exits
between worker completion and DAG-node finish is therefore reconciled from the
existing durable worker evidence rather than replayed.

The real process-death acceptance covers two distinct boundaries. If the
Writable `SessionTask` is already `COMPLETED` and its lease is already
`PRESERVED`, but the process exits before the DAG terminal CAS, restart
reconciles the exact node to `COMPLETED` and releases `active_node_id` without
creating another worker. If the worker owner exits while the exact
`SessionTask` is still non-terminal, Writable reconciliation marks the lease
`ORPHANED`; DAG reconciliation records `INDETERMINATE`, preserves the child
session/worktree/checkpoint/relay identities, and does not rerun the worker.
These guarantees are bounded to the tested real `spawn`/`os._exit` seams.

## Not implemented

This ADR does not add model-generated DAG decomposition or define the Leader
controller; the bounded Leader is specified separately by [ADR 0135](0135-bounded-serialized-leader-controller.md).
It does not add Swarm,
Ultracode, automatic delegation, unbounded or dynamic dataflow
scheduling, predecessor transcript sharing, shared worktrees,
merge/integration, commit, rollback, cleanup, retries, automatic crash reruns,
CLI/TUI/ACP exposure, or a new public orchestration protocol. The bounded
direct predecessor-result relay is specified separately by [ADR 0136](0136-bounded-task-dag-predecessor-result-relay.md).

## Validation boundary

Acceptance requires domain bound/cycle tests, schema 17-to-23 migration with
populated Parent Relay preservation, insert-only and stale-generation tests,
durable recovery-claim CAS and schema-20-to-21 migration, cross-process claim
and two-scheduler race evidence, real live-owner partial-window and
dead-owner-before-lease takeover evidence, bounded parallel fan-out/fan-in
overlap, deterministic diamond failure propagation, exact worker correlation,
completed/failed/
cancelled/uncertain recovery, real `multiprocessing.spawn` process death after
worker completion and during active ownership, no-rerun allocation counts,
real Parent Relay-before-model behavior, real Writable/LSP regression, separate
managed Worktrees, unchanged parent dirty state, full local gates, and the
stacked PR merge-ref platform matrix.
