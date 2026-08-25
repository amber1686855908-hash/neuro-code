# ADR 0136: Bounded Task DAG predecessor-result relay

- Status: implemented as an explicit internal P0 vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-24
- Scope: direct completed-predecessor result projection for one serialized bounded Task DAG

## Context

ADR 0134 intentionally made dependency edges control-only. That prevents a
worker from receiving an implicit transcript, tool history, workspace state, or
authority grant from a predecessor, but it also leaves a dependent worker
without the small amount of completed-result context needed by a bounded DAG
workflow. The missing capability must not become a second parent-context
system, a prompt-to-authority channel, or a parallel orchestration design.

The existing system already has three different owners that must remain
separate:

1. Parent Context Relay carries a bounded snapshot from a parent session into a
   child worker.
2. Task DAG predecessor-result relay carries completed direct-predecessor
   result evidence into a dependent worker.
3. Leader evidence carries bounded DAG state into the zero-tool Leader.

## Decision

Add an application-owned `TaskDagDependencyResultRelay` for a claimed
dependent node. The relay is created only after the target node has an exact
`RUNNING` graph/node generation claim and before the child runtime or provider
request is created. A root node receives no relay. A dependent node receives
exactly its declared direct dependencies, in declaration order; transitive
ancestors are available only through their own direct chain.

The relay is an immutable, insert-only projection. Each entry contains the
predecessor node and generation, exact worker task/session/lease/worktree/
checkpoint/Parent Relay identities, final workspace fingerprint, changed-file
count, and a bounded redacted result preview. The entry is accepted only when
the predecessor is durably `COMPLETED`, its worker evidence is consistent, its
writable lease is `PRESERVED`, and its Parent Relay and workspace/checkpoint
identities match the DAG node projection.

## Bounds and content boundary

The application enforces all of these limits before persistence and before
message rendering:

- at most four predecessor entries;
- at most 4 KiB of UTF-8 result text per entry;
- at most 16 KiB of aggregate source result text;
- at most 24 KiB of rendered relay message;
- bounded opaque IDs and fingerprints; no unbounded error or response text.

Only redacted result text and evidence metadata cross the edge. The relay does
not contain or authorize transcript history, reasoning, tool calls or tool
results, workspace bytes, Git data, checkpoint bytes, arbitrary paths,
capabilities, sandbox roots, network access, LSP authority, or instructions.
The `ContextBuilder` injects one separate
`SyntheticReason.DAG_PREDECESSOR_RESULTS` USER message after the Parent Relay
and before genuine child history. It is not written to child history and is
never parsed as an authority source.

## Durable identity, races, and failure

Session schema 20 adds `task_dag_dependency_relays`; schema 21 adds the
separate `task_dag_recovery_claims` ownership fence. A relay row binds the
exact DAG definition, target node definition and generation, direct dependency
IDs, entry fingerprints, source/content fingerprints, byte count, and
integrity fingerprint. The target-generation uniqueness key makes an exact
duplicate publication idempotent. A duplicate with different content or
identity is rejected; direct database tampering fails integrity validation on
reload.

The recovery claim is not a node-generation lock and does not reuse Leader
attempts. Its immutable execution identity binds the parent session, DAG and
node definition fingerprints, exact node generation, parent task ID, and relay
ID plus source/content/integrity fingerprints. Its owner PID/token is the only
mutable part and changes only by exact version CAS. The unique
`(dag_id, node_id, node_generation)` key is the durable cross-process fence.

The store uses the existing SQLite transaction boundary and reloads the
published row before commit. A concurrent scheduler cannot publish a
conflicting relay for the same target generation. Recovery classifies an
already-claimed active node without starting a worker:

- `ACTIVE_WORKER` means exact non-terminal `SessionTask` and writable lease
  ownership evidence exists; existing Writable reconciliation remains the
  recovery owner.
- `RECOVERY_OWNED` means an exact recovery claim is held by a live or
  unproven owner. Reconciliation is read-only in this state: it does not start
  Writable, steal the claim, fail the node, or mark it `INDETERMINATE`.
- `SAFE_NOT_STARTED` is allowed only when the exact `RUNNING` node and
  `parent_task_id` are durable, the existing relay is loaded by
  `(dag_id, target_node_id, target_generation)` and passes READY, definition,
  direct-dependency, and fingerprint checks, and both the matching
  `SessionTask` and writable lease (and subagent link) are absent, with no
  live recovery owner. The read-only reconciliation path only classifies this
  state. A later DAG step first acquires the exact durable recovery claim and
  only its winner may call Writable. A live loser returns the canonical active
  state without provider, resource, lease, task, or node-terminal side effects.
- `INDETERMINATE` covers an absent or unverifiable relay, partial evidence, a
  link or other worker ownership evidence, stale identity, or any uncertain
  state. It never auto-reruns a possibly-started worker. A lease-only partial
  window is not `INDETERMINATE` while the exact recovery owner is live or
  unproven.

If the recovery owner dies before the first Writable lease insert, a fresh
controller may prove that owner dead and take over the same claim by version
CAS, preserving the same node generation, parent task, and relay identity. If
the lease insert has begun, recovery never automatically reruns the worker;
the existing Writable reconciliation decides whether the evidence is
reconcilable or must remain fail-closed.

This boundary follows the production Writable ordering: repository identity
inspection is read-only; the first durable side-effecting allocation is the
lease insert, followed by `SessionTask`, worktree, checkpoint, child session,
subagent link, Parent Relay, runtime creation, and model execution. Therefore
the exact active-node plus READY-relay plus absent-task-and-lease state proves
that this Writable allocation phase was never entered. The durable recovery
claim closes the cross-process interval between that proof and the first lease
insert. Real `spawn` acceptance covers two controllers racing from the same
pre-claim snapshot, a live-owner partial lease window, and a dead-owner-before-
lease takeover; a crash after ownership evidence remains fail-closed and is
not replayed.

If target generation, predecessor state, lease, Parent Relay, workspace/checkpoint
evidence, or any identity is missing, stale, uncertain, or mismatched, the
application marks the target `INDETERMINATE` and does not construct a
worker/provider request. If a relay was durably published and the worker
crashes before model execution, recovery reuses the exact publication; it does
not regenerate a different result or replay a predecessor.

## Non-goals

This ADR does not add parallel DAG execution, dynamic/model-generated graph
construction, transitive aggregation beyond direct edges, retries, reruns,
merge/copy-back, rollback, cleanup, shared live context, UI/ACP exposure,
Swarm, Ultracode, or a general-purpose inter-agent message bus. `max_parallel`
remains one, and `TaskDagApplicationService` remains the only worker execution
seam.

## Validation boundary

The focused implementation tests cover bounded rendering and redaction,
synthetic-message replacement, schema migration and row integrity, exact
duplicate idempotency, conflicting publication rejection, direct-dependency
selection, declaration ordering, dependency-chain behavior, failed or
uncertain evidence fail-closed behavior, the read-only recovery classification,
durable recovery-claim insert/CAS and schema-20-to-21 migration, real
safe-not-started process death and exact-once continuation, two-controller
cross-process ownership and partial-window behavior, dead-owner-before-lease
takeover, ambiguous post-allocation ownership process death without rerun, and
injection through the existing Writable Subagent composition path. Existing
Task DAG, Leader, Writable
Subagent, Parent Relay, Worktree, Checkpoint, worker-scoped LSP, crash
recovery, and full repository gates remain required. The slice is not a claim
of parallel/dataflow scheduling or live paid-provider acceptance.
