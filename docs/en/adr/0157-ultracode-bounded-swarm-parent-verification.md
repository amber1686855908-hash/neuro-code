# ADR 0157: Structured BOUNDED_SWARM parent verification

- Status: Accepted
- Date: 2026-09-08
- Scope: VF-4c structured UltraCode parent verification

## Context

VF-4a froze verification requirements for `MAIN_MAX`, while VF-4b exposed a
durable `parent_workspace_changed` fact from Result Adoption. The structured
`BOUNDED_SWARM` path still stopped after adopting worker output and could not
send the real parent workspace through the normal verification and final
response boundaries. The lower Swarm result is orchestration evidence, not a
verified answer for the parent user turn.

## Decision

Structured `BOUNDED_SWARM` uses the existing application-owned
`NormalTurnRequirementsPolicy`, `VerificationTracker`, `AgentRuntime`, and
VF-2 final-response contract. A fresh absent request is resolved exactly once;
explicit non-empty and empty snapshots are preserved. The effective snapshot
is stored in the existing schema-30 Ultracode projection and the parent
`TurnInput`. A persisted structured execution must reuse that exact snapshot;
a legacy NULL snapshot remains legacy, and a conflicting mode or snapshot
identity fails closed. No schema-31 migration is added.

The durable Ultracode row is the orchestration identity. Unlike the legacy
path, a structured execution does not pre-create a parent attempt or commit
the lower Swarm response. It runs the canonical bounded Swarm, adopts its
durable result through Result Adoption, and then starts the real parent
`AgentRuntime` with the original prompt, parent turn ID, execution ID, and
exact requirements snapshot. If adoption reports `parent_workspace_changed`,
its stable `adoption_id` is passed as a seed; the parent tracker records one
workspace mutation before the first model step. The tracker remains the sole
generation owner, and worker requirements or worker verification evidence are
not propagated to the parent.

When adoption completes, the parent runtime owns all subsequent tools,
verification, finalization, and the committed response. A conflict or
indeterminate adoption is an orchestration failure, not verification `FAIL`;
it is completed only through the parent-owned deterministic, bounded,
truth-safe fallback. The lower Swarm response is never used as parent verified
truth. Only the parent completion path updates the Ultracode final response
and result fingerprint.

## Recovery and compatibility

Recovery uses exact durable Swarm, adoption, parent-attempt, TurnInput, and
committed-response identities. It resumes only a safely retryable parent
attempt, replays a committed parent without Provider, verification, or
Finalizer execution, and never creates a second turn or assistant item. A
crash before the parent attempt leaves the durable orchestration state
recoverable; an observable parent output or unresolved non-retryable attempt
fails closed. The existing `FINALIZING` state remains the pending parent
completion boundary.

Legacy `BOUNDED_SWARM` executions with a NULL verification snapshot retain the
existing external-result behavior. `MAIN_MAX`, the Swarm/worker/Planner/
Leader/DAG/Result Adoption owners, permission and sandbox boundaries, and
CLI/TUI/ACP projections remain unchanged. No worker verification import,
requirement inference, test discovery, TestRunner, framework detection, NLP
inference, or public verification UI is added.

With this slice, the planned Verification Foundation sequence is complete.
Future work is product capability or stabilization work, not another
verification-foundation integration boundary.

## Validation

Focused unit, recovery, and production-shaped composition tests cover fresh
structured requests, explicit snapshot modes, adoption success and failure,
parent mutation seeding, exact retry/recovery, committed parent replay, legacy
BOUNDED_SWARM compatibility, and unchanged MAIN_MAX behavior.
