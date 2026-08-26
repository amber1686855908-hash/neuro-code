# ADR 0138: Bounded Model-Generated DAG Planning

- Status: implemented as an explicit internal P0 vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-26
- Scope: one explicit parent objective to one immutable bounded Task DAG
- Depends on: ADR 0134, ADR 0135, ADR 0136, and ADR 0137

## Context

The static Task DAG and parallel-aware Leader slices already provide canonical
graph validation, durable capacity, bounded waves, and isolated Writable
workers. They intentionally require a caller-supplied graph. This slice adds
one provider-backed planning step before graph publication. It proves model
generated planning only; it is not a replan or graph revision mechanism.

The planning boundary must not move execution authority into the model. A
planner exists before a worker lease, Worktree, Checkpoint, or child runtime
exists, so the worker-specific Parent Context Relay is not the planning input
store. The existing Leader lifecycle is also not reused as a table because
its attempt is foreign-keyed to an already-published `task_dags` row.

## Decision

Keep the authority chain:

```text
explicit parent objective
        -> zero-tool Planner proposal
        -> TaskDagApplicationService validation/publication
        -> parallel-aware Leader selection
        -> Writable worker execution
```

The Planner owns only an immutable proposal. TaskDagApplicationService remains
the sole owner of node, edge, dependency, acyclic, prompt, parallelism, and
immutable graph validation/publication. The Leader remains the owner of READY
wave selection. Writable remains the owner of worker binding, capability
intersection, Worktree, Checkpoint, child session, tools, and worker-scoped
LSP.

### Zero-tool Planner binding

Composition creates a dedicated persisted planner session and one-step
`ConversationBinding`. Local tools, provider-hosted tools, filesystem, Bash,
terminal, network, MCP, LSP, Worktree, Checkpoint, worker, and background
capabilities are absent. The planner binding is not exposed through a public
CLI, TUI, or ACP orchestration command.

### Planning input envelope

The request contains one caller-provided `planning_id` and objective. The
parent identity is taken from the actual parent binding's runner session ID;
caller-provided identity is not authoritative. The Planner may receive a
separate immutable `PlanningContextEnvelope` containing only genuine USER and
visible ASSISTANT plain-text messages. It excludes system and tool roles,
synthetic items, hidden reasoning, tool calls/results, media, arbitrary
workspace bytes, and authority-bearing structures. Configured sensitive values
are redacted before inclusion.

The envelope preserves source order and uses the existing bounded context
limits: at most 10 items, 4 KiB per item, 24 KiB projected content, and 32 KiB
rendered content. Its canonical JSON and SHA-256 fingerprint are deterministic.
The envelope is evidence only and cannot grant tools, roots, sandbox policy,
provider access, workers, or filesystem authority.

### Strict proposal contract

The provider must return one strict JSON object with only these top-level
fields:

```json
{
  "nodes": [
    {"id": "research", "prompt": "bounded task", "depends_on": []}
  ],
  "max_parallel": 1,
  "reason": "bounded decomposition"
}
```

Each node must contain exactly `id`, `prompt`, and `depends_on`. Node
declaration order is canonical. Dependency IDs must be unique and appear in
the same declaration order; the parser does not sort away graph semantics.
Unknown dependencies, self-dependencies, cycles, duplicate node IDs, edge
overflow, and other graph-definition rules are delegated to the canonical
Task DAG validator after strict parsing. The frozen limits remain: at most 8
nodes, 16 edges, 4 dependencies per node, 8 KiB node prompts, and
`max_parallel` from 1 through 4. Proposal fields cannot request capabilities,
roots, sandbox settings, providers, tools, retries, merges, shell commands,
dynamic expressions, or worker behavior. Node prompts are data, not authority.

Canonical sorted-key JSON gives equivalent JSON spellings one proposal
fingerprint. Semantic differences in declaration order, dependencies, prompts,
or `max_parallel` remain different fingerprints.

### Durable identity and publication

Schema 25 adds two dedicated projections:

- `orchestration_planning_attempts` stores the exact planning ID, actual parent
  session, objective/context fingerprints, planner session and turn, owner and
  lease, lifecycle, preallocated intended DAG ID, model response, proposal
  fingerprint, and published DAG ID.
- `orchestration_plan_proposals` stores one insert-only exact parsed proposal
  bound to the attempt, parent, intended DAG, objective/context fingerprints,
  and canonical proposal JSON.

`ApplicationComposition.create_model_planning_service()` creates a new persisted
Planner session for every fresh service. The service's `planning_session_id` is
the identity of the current recovery controller; it is intentionally distinct
from the historical `planner_session_id` and `planner_turn_id` stored on the
attempt. A fresh controller may therefore use L2 after a crash under L1 while
the committed attempt provenance remains L1/T1 and is never rewritten merely
because recovery uses a new composition.

The lifecycle is:

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> PROPOSAL_PUBLISHED
         -> DAG_PUBLISHED -> COMPLETED
```

`STALE` classifies invalid observable model output. `INDETERMINATE` classifies
an unresolved provider or storage boundary. The attempt preallocates its
intended DAG ID before the provider request, and the same ID is used when the
canonical Task DAG service publishes the graph. Proposal publication is
insert-only: an exact duplicate is idempotent, while a conflicting proposal
or tampered canonical record fails closed.

### Replay and crash semantics

The existing observable-turn invariant applies: after provider output or turn
evidence is observable, the Planner never automatically calls the provider
again. A fresh controller may continue only from durable exact identity.

The accepted recovery boundaries are:

1. Before provider output is observable, the existing liveness/fence policy
   may permit a safe claim takeover when no turn evidence exists.
2. After model output is committed, recovery parses and publishes the same
   durable response without provider replay.
3. After the proposal is durable, recovery uses the same proposal and intended
   DAG ID without changing any definition field.
4. After Task DAG insertion, insert-only exact identity returns the existing
   graph; recovery verifies its definition/fingerprint and completes the
   planning attempt without a second graph. If a generic session turn has
   already recorded request/output evidence while the Planner-specific model
   commit is missing, recovery remains fail-closed and does not replay the
   provider.

A live or unproven owner cannot be stolen. Concurrent controllers use durable
owner/CAS checks, so one exact planning identity cannot produce duplicate
provider calls, divergent proposals, different intended DAG IDs, or two
Task DAG publications. Fresh spawned compositions cover the committed
output, proposal publication, DAG insertion, and provider-turn-evidence
crash windows; an independent-process controller race also proves the losing
controller does not mutate the winner's provenance.

### Explicit non-goals

This ADR does not add retry, DAG revision, mutation after publication, replan,
node resurrection, recursive planning, automatic delegation, task-complexity
routing, Swarm, Ultracode, distributed scheduling, unlimited agents,
merge/copy-back, rollback orchestration, cleanup orchestration, public
CLI/TUI/ACP orchestration APIs, or a user-visible workflow editor. It does not
add worker capabilities or a second generic runtime.

## Verification

Focused tests cover strict parsing, unknown/malformed/duplicate and invalid
graphs, frozen bounds, canonical fingerprints, zero-tool composition, actual
parent identity, bounded context projection and redaction, insert-only and
tamper behavior, exact intended DAG identity, concurrent ownership, fresh
composition L1-to-L2 crash recovery at each durable publication boundary,
provider-turn evidence fail-closed recovery, provider no-replay, and schema
24-to-25 preservation.

A production-shaped acceptance uses a real `ApplicationComposition`, scripted
provider, real planner, real TaskDagApplicationService, real parallel-aware
Leader, and real Writable workers. It verifies the A -> B/C -> D graph,
`max_parallel=2`, one planner call, zero Planner/Leader tools, B/C overlap, D
ordering, distinct managed Worktrees, and an unchanged dirty parent checkout.
Separate fresh OS-process acceptance proves L1 != L2, preserves historical
L1/T1 provenance, reuses the exact response/proposal/intended DAG, and keeps
provider invocation count at one through output, proposal, and DAG crash
windows. A provider-turn-evidence crash is classified as explicit recovery
required/`INDETERMINATE`, never as an automatic retry.

The next independent slice is Bounded DAG Revision / Replan. It is not part
of this implementation.
