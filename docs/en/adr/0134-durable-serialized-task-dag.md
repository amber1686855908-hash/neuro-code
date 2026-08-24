# ADR 0134: Durable serialized Task DAG

- Status: implemented as an explicit internal vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-24
- Scope: one bounded, caller-defined DAG whose nodes reuse the existing Writable Subagent pipeline

## Context

ADRs 0129-0133 establish the managed Git worktree, workspace checkpoint,
writable child, worker-scoped read-only LSP, and bounded Parent Context Relay
contracts. They deliberately do not provide orchestration. A first DAG slice
must add durable dependency control without creating a second worker runtime,
transferring authority, or implying parallel or autonomous delegation.

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

## Durable publication and serial claim

Schema 18 adds `task_dags` and `task_dag_nodes`. Definitions are insert-only;
an existing DAG ID is accepted only when its immutable definition fingerprint
matches. Graph and node lifecycle mutations use generation CAS.

Before a worker starts, the service atomically changes one `READY` node to
`RUNNING`, persists its exact parent task ID, and sets the graph's
`active_node_id`. The same transaction updates the graph generation. Finishing
the active node atomically writes its terminal projection, clears
`active_node_id`, and advances the graph generation. The active-node claim is a
cross-process serial gate: two schedulers cannot claim different ready nodes at
the same time. No timestamp, latest-row, prompt, or lease guessing is used.

The node projection records only bounded lifecycle and workspace identities:
parent task, child session, lease, Worktree, baseline Checkpoint, Relay,
fingerprints, changed-file count, and a bounded response preview. A completed
node requires exact lease and Relay evidence; missing or inconsistent success
correlation is `INDETERMINATE`.

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

Reconciliation first runs the existing Writable reconciliation, then looks up
the exact parent session/task and exact lease for the active node. A completed,
failed, or cancelled `SessionTask` maps to the same DAG node meaning. Missing
task/lease evidence, an orphaned or uncertain lease, or an invalid correlation
maps to `INDETERMINATE`. No worker is automatically rerun and no workspace is
deleted, rolled back, merged, copied back, or cleaned up.

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
Ultracode, automatic delegation, parallel execution, dataflow/result relay,
predecessor transcript sharing, shared worktrees, merge/integration, commit,
rollback, cleanup, retries, automatic crash reruns, CLI/TUI/ACP exposure, or a
new public orchestration protocol.

## Validation boundary

Acceptance requires domain bound/cycle tests, schema 17-to-19 migration with
populated Parent Relay preservation, insert-only and stale-generation tests,
cross-process claim and two-scheduler race evidence, deterministic serialized
diamond failure propagation, exact worker correlation, completed/failed/
cancelled/uncertain recovery, real `multiprocessing.spawn` process death after
worker completion and during active ownership, no-rerun allocation counts,
real Parent Relay-before-model behavior, real Writable/LSP regression, separate
managed Worktrees, unchanged parent dirty state, full local gates, and the
stacked PR merge-ref platform matrix.
