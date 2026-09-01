# ADR 0135: Bounded serialized Leader controller

- Status: implemented as an explicit internal P0 vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-24
- Scope: one Leader over one pre-created bounded Task DAG, with serialized decisions

## Context

ADR 0134 provides the durable Task DAG and makes the DAG the dependency and
execution-legality authority. A Leader slice must add model-assisted choice
without creating a second worker runtime, changing a published graph, or
turning ordinary model text into authority. The Leader must also survive
controller races and process death without replaying a provider request after
its output is observable.

## Decision

Add `LeaderApplicationService` as an explicit internal application workflow.
The caller supplies an existing DAG ID and a bounded objective. The Leader
does not create, delete, replan, or mutate the DAG definition, its dependency
edges, node prompts, capability grants, workspace roots, or authority owners.
The actual parent `ConversationBinding` supplies the parent identity. The
existing `TaskDagApplicationService` remains the only way the Leader can
advance a worker node.

One Leader controller owns one dedicated persisted zero-tool model binding.
Composition removes both local and provider-hosted tools, disables background
wakes, and does not bind a writable worker, Worktree, Checkpoint, Relay, LSP
worker, terminal, shell, MCP server, network client, or child subagent to the
Leader. The Leader model has no side-effect capability; typed decision
validation is the only authority boundary.

## Bounded evidence envelope

Before every decision, the Leader asks the existing DAG service to reconcile
the active node, propagate dependency states, confirm that no worker is active,
and load the exact current READY set. It projects at most eight nodes into an
immutable deterministic envelope. Node prompts, result previews, and error
metadata are bounded and redacted. The envelope contains only durable outcome
metadata and opaque identity/fingerprint fields; it excludes raw transcript,
reasoning, tool arguments or output, Parent Relay payload, workspace bytes,
Checkpoint bytes, Git diff, credentials, and arbitrary paths. A canonical JSON
SHA-256 fingerprint binds the envelope to the decision.

Evidence is untrusted data. Text such as `run bash /etc/passwd`, `enable MCP`,
`ignore dependencies`, or an unknown tool name cannot grant authority.

## Typed decision contract

The model must return one strict JSON object with no markdown or unknown
fields. The only actions are:

- `SELECT_NODE`, with a node ID in the exact READY set and an optional bounded
  reason;
- `FINALIZE`, with a bounded synthesis summary, only when the DAG is terminal.

There is no `CREATE`, `REPLAN`, `SPAWN`, `RETRY`, `MERGE`, `CANCEL_BRANCH`, or
prompt-modification action. Every durable decision binds the exact DAG
generation, definition fingerprint, evidence fingerprint, Leader session,
attempt/run identity, and decision ID.

## Durable lifecycle and replay

Session schema 19 adds `leader_attempts` and `leader_decisions`. The unique
attempt key is the exact DAG snapshot plus objective fingerprint. SQLite
`BEGIN IMMEDIATE` transactions and lifecycle CAS establish one model-request
owner for one exact snapshot:

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> DECISION_PUBLISHED -> EXECUTED
       \-> STALE or INDETERMINATE
PROVIDER_FENCED \-> INDETERMINATE
```

The attempt has three distinct identities: the durable `owner_id`, the
dedicated persisted `leader_session_id`, and the fresh `turn_id`. Immediately
before a provider call, the controller must atomically transition
`CLAIMED -> PROVIDER_FENCED` with all three identities and an unexpired lease.
The actual `ConversationBinding.runner.session_id` must equal the attempt's
`leader_session_id`; the model commit repeats that owner/session/turn CAS. A
controller that loses this fence cannot call the provider.

An expired `CLAIMED` attempt is reclaimable only when it has no committed model
response, no decision, and no matching turn evidence in its old Leader session.
The SQLite takeover atomically replaces owner, lease, `leader_session_id`, and
`turn_id` with the new controller's values. Lease expiry is not proof that a
process died: if a live old controller resumes, its pre-provider fence fails.
Once `PROVIDER_FENCED` is durable, automatic takeover is intentionally
disabled; restart/recovery fails closed and requires explicit recovery. This
prevents a second controller from guessing whether a provider call occurred.

The Leader writes the durable attempt before calling the model, persists the
bounded redacted model response before parsing it, then publishes the typed
decision insert-only. A second controller reuses `MODEL_COMMITTED`,
`DECISION_PUBLISHED`, or `EXECUTED` state and never calls the provider for that
snapshot. Historical `leader_session_id` and `turn_id` are never rewritten
after model output or a decision is durable. Decision validation binds the
record to its historical attempt, not to the fresh recovery service session.
If the existing session turn indicates an unresolved provider attempt, the
Leader marks the attempt `INDETERMINATE`/fails closed; it does not infer a safe
retry from restart. This preserves the existing Session turn recovery rule
that indeterminate provider work is never automatically replayed.

If a process exits after a decision is durable but before the DAG claim, a
restart can apply the same decision through the DAG generation/active-node
CAS. If another controller wins the DAG claim, the losing controller does not
allocate a second worker. DAG failure and uncertainty meanings remain
canonical; Leader adds no retry or resurrection semantics.

## Serialized execution and final synthesis

The Leader uses the one-step DAG seam in this order:

1. reconcile and propagate the current graph;
2. verify no active node and load the exact READY set;
3. obtain one typed decision for that evidence;
4. revalidate exact generation and evidence fingerprint;
5. execute at most the selected existing Writable Subagent node through
   `run_task_dag_step()`;
6. persist/reconcile the result and build the next evidence snapshot.

The seam never auto-executes a next node. The Leader loop is bounded by the
Task DAG size plus one finalization decision. `FINALIZE` is requested only on a
terminal DAG snapshot; its bounded summary stays in the dedicated Leader
session and is returned as the Leader result, not appended to the parent
transcript. Predecessor output is not injected into worker prompts, Relay,
instructions, skills, or workspace state.

## Validation boundary

The focused suite covers deterministic evidence, strict decisions, zero-tool
composition, serialized diamond order, unknown/blocked/stale selection,
SQLite insert-only/CAS lifecycle, same-snapshot controller races, fresh
`ApplicationComposition` L1 -> L2 -> L3 restart, atomic session/turn rebind,
pre-provider fencing against a live expired owner, durable model-commit reuse,
terminal decision idempotence, and no provider replay after observable L2 turn
evidence. Existing Task DAG, Writable Subagent, Worktree, Checkpoint, Relay,
LSP, and full repository gates remain regression requirements. The slice is not
exposed through CLI/TUI/ACP and does not turn the Leader into an unbounded
parallel planner; bounded Task DAG workers are specified separately. The
bounded parallel-aware Leader extension is specified by [ADR 0137]
(0137-parallel-aware-leader-bounded-wave-scheduling.md). Swarm,
Ultracode, automatic delegation, model DAG creation, replan, retry, merge,
rollback, or live paid-provider acceptance.
