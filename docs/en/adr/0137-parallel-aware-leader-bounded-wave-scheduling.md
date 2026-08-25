# ADR 0137: Parallel-aware Leader / bounded wave scheduling

- Status: implemented as an explicit internal P0 vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-26
- Scope: one zero-tool Leader over one already-published bounded Task DAG
- Supersedes: the serialized execution portion of ADR 0135; ADR 0135 remains the historical decision contract

## Context

ADR 0134 already provides immutable static Task DAG definitions, durable
capacity, per-node execution ownership, generation CAS, and independent
Writable worker services. ADR 0135 provides a durable zero-tool Leader, but
its execution seam was serialized even when a DAG declared `max_parallel > 1`.
The next slice must let the Leader select one bounded wave without moving
worker, workspace, capability, or dependency authority into the model.

The slice is deliberately internal. It does not add a public CLI/TUI/ACP
surface, dynamic graph construction, replan, retry, merge, rollback, or a
second orchestration hierarchy.

## Decision

Keep the authority hierarchy:

```text
zero-tool Leader decision -> Task DAG claim/CAS -> Writable worker -> Worktree/session resources
```

The Leader owns only a typed decision. The Task DAG remains the sole owner of
dependency legality, durable `RUNNING` capacity, node generations, execution
owner identity, and worker invocation. The Writable Subagent remains the sole
owner of child binding, capability intersection, worktree, lease, checkpoint,
relay, and worker-scoped LSP resources. The Leader never receives tools or a
worker binding.

### Typed decisions and canonical selection

The strict JSON contract contains exactly one of:

```json
{"action":"SELECT_NODE","node_id":"<ready id>","reason":"<bounded text>"}
{"action":"SELECT_NODES","node_ids":["<ready id>","<ready id>"],"reason":"<bounded text>"}
{"action":"FINALIZE","summary":"<bounded text>"}
```

`SELECT_NODE` remains the serialized compatibility path. For
`SELECT_NODES`, the list is non-empty, unique, bounded by `max_parallel`, and
must be in canonical `(ordinal, node_id)` order. A non-canonical list is
rejected; the Leader does not silently reorder model output. Every selected ID
must be in the exact READY set and the decision is rejected when it exceeds
the durable free capacity. `FINALIZE` is accepted only for a terminal DAG
with no active `RUNNING` node.

The Leader validates the decision against the exact evidence snapshot before
calling the Task DAG wave seam. Unknown IDs, duplicate IDs, terminal-node
selection, stale graph/node generations, capacity overflow, and malformed or
unknown JSON fail closed. Typed invalid output is durably historical and is
never replayed as a provider request.

### Evidence contract

The bounded evidence envelope includes the parent session, DAG ID and
definition fingerprint, graph generation, immutable `max_parallel`, durable
`running_node_ids`, calculated `available_capacity`, canonical READY IDs, and
one bounded node projection per node. Each node includes ordinal, generation,
dependencies, state, bounded redacted outcome metadata, and opaque worker
identity/fingerprint fields.

For deterministic inspection, the payload also exposes state buckets:
`completed_node_ids`, `failed_node_ids`, `cancelled_node_ids`,
`skipped_node_ids`, and `indeterminate_node_ids`. State buckets are derived
from the node projection and are evidence only; they do not grant authority.
The existing byte, node-count, text-bound, and redaction limits remain in
force. Raw transcript, reasoning, tool arguments/results, relay payloads,
workspace bytes, credentials, arbitrary paths, and capability grants remain
excluded.

### Wave execution and capacity

`RunTaskDagWaveRequest` is an internal typed seam carrying the selected IDs,
the expected graph generation, and the expected generation for every selected
node in the same canonical order. The Task DAG service:

1. reconciles the current graph and propagates dependency state;
2. verifies the exact graph generation, selected READY generations, immutable
   parallel bound, and current durable capacity;
3. claims only the selected nodes through the existing SQLite `BEGIN IMMEDIATE`
   capacity check and graph/node generation CAS;
4. creates one independent Writable service per claimed node; and
5. runs the claimed workers in a structured `TaskGroup`.

The durable capacity is always `max_parallel - RUNNING rows`; no process-local
semaphore is authority. A race or recovery may reduce capacity after a
decision is published. In that case the service may claim only the selected
canonical prefix that still fits; it never substitutes an unselected READY
node. The next evidence refresh decides whether remaining selected nodes can
be applied. `max_parallel=1` continues through the serialized one-node path.

### Durability, recovery, and races

Session schema 24 adds the parallel decision projection. Leader attempts and
decisions retain the parent session, selected node IDs, and selected node
generations. The 23-to-24 migration adds the columns, backfills parent
identity from `task_dags`, backfills old `SELECT_NODE` lists, and rebuilds the
decision table when its old `CHECK` constraint does not admit `SELECT_NODES`.
The migration preserves populated schema-23 attempt/decision rows.

The actual node execution owner remains the Task DAG row. A durable
`SELECT_NODES` decision is reusable after a process crash only when each
selected node is still at its recorded READY generation or has durably
advanced from it to `RUNNING` or a terminal node. Recovery never calls the
Leader provider again and never creates an unselected worker. A partial claim
can therefore finish the remaining selected prefix while the already claimed
node remains protected by the existing Task DAG/Writable recovery semantics.

Two controllers still race through the existing durable Leader attempt fence
and Task DAG CAS. One controller may publish and execute the wave; the other
must either reuse the observable durable decision or fail closed. A live stale
provider owner is fenced, and an unresolved provider turn remains
`INDETERMINATE`; no provider replay is inferred from process death.

Failure, cancellation, skipped descendants, and indeterminate nodes retain
the Task DAG's existing semantics. An indeterminate branch does not prevent an
unrelated READY branch from being scheduled, but a terminal `INDETERMINATE`
DAG cannot be finalized as a successful result. No automatic retry, rerun,
merge, rollback, cleanup, or graph mutation is introduced.

## Validation boundary

The slice is accepted only with focused and production-shaped evidence for:

- serialized `max_parallel=1` compatibility;
- a real `A -> (B,C) -> D` wave where B and C overlap and D waits for both;
- evidence for one running node plus three READY nodes with two free slots;
- canonical-order, duplicate, overflow, stale-generation, and running-node
  `FINALIZE` rejection;
- independent worker/resource identity and no unselected substitution;
- durable decision reuse without provider replay;
- partial-claim recovery, spawned-process death, and concurrent controllers;
- failure, cancellation, skipped descendants, and unrelated-branch progress;
- populated schema-23 to schema-24 migration; and
- the complete repository formatting, typing, coverage, build, and docs-parity
  gates.

The slice remains internal and bounded. The final capability rating is
`PROVEN within current vertical-slice scope` only after the exact-head
merge-ref CI is green.
