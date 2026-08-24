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

Session schema 20 adds `task_dag_dependency_relays`. A row binds the exact DAG
definition, target node definition and generation, direct dependency IDs,
entry fingerprints, source/content fingerprints, byte count, and integrity
fingerprint. The target-generation uniqueness key makes an exact duplicate
publication idempotent. A duplicate with different content or identity is
rejected; direct database tampering fails integrity validation on reload.

The store uses the existing SQLite transaction boundary and reloads the
published row before commit. A concurrent scheduler cannot publish a
conflicting relay for the same target generation. If target generation,
predecessor state, lease, Parent Relay, workspace/checkpoint evidence, or any
identity is missing, stale, uncertain, or mismatched, the application marks
the target `INDETERMINATE` and does not construct a worker/provider request.
If a relay was durably published and the worker crashes before model
execution, recovery reuses the exact publication; it does not regenerate a
different result or replay a predecessor.

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
uncertain evidence fail-closed behavior, and injection through the existing
Writable Subagent composition path. Existing Task DAG, Leader, Writable
Subagent, Parent Relay, Worktree, Checkpoint, worker-scoped LSP, crash
recovery, and full repository gates remain required. The slice is not a claim
of parallel/dataflow scheduling or live paid-provider acceptance.
