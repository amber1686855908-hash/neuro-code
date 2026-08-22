# ADR 0127 — Durable turn crash and INDETERMINATE recovery

[简体中文](../../zh-CN/adr/0127-durable-turn-crash-recovery.md) · **English**

## Status

Accepted for the current pre-alpha runtime.

## Context

Before this decision, a session had durable `USER_MESSAGE`, model-step, request
snapshot, tool, and terminal events, but no durable identity for one complete
`AgentRuntime.run()`. A provider request could start after a snapshot without a
write-ahead request fact, and a tool could start without a recovery-owned
started fact. Normal failure recording also updated the task, failure event,
and retained context through separate storage calls. After a process exit,
event absence could not distinguish an unstarted turn from a turn that had
already produced model output or a side effect.

Execution segment checkpoints already document progress guidance and bounded
audit metadata. They are not process-crash recovery and are not workspace
rollback points. This ADR adds a separate durability layer without changing
that contract.

## Decision

- Every persisted turn receives a unique opaque `turn_id`. A turn attempt is
  accepted in `session_turn_attempts` before the first provider request or tool
  body. Background wake turns also receive an attempt; their input is marked
  non-reconstructable because child-task state may change independently.
  For plan execution, acceptance and task ownership are one SQLite transaction:
  a new `RUNNING` task is inserted with the exact `attempt.task_id`, or the
  exact `QUEUED` task is validated and transitioned to `RUNNING` with the same
  identity. Recovery never infers ownership from the latest task, a fingerprint,
  or an event.
- `session_turn_attempts` is the small canonical recovery index. The ordered
  `events` table remains append-only audit evidence. A recovery fact and its
  event are written in the same SQLite transaction, so classification is
  derived from sticky facts rather than from event absence or the last UI
  message.
- The write-ahead boundaries are explicit. `MODEL_REQUEST_STARTED` is
  persisted before the provider stream is entered. The first observable text,
  reasoning, backend-tool, tool-call, or completion event persists
  `MODEL_OUTPUT_STARTED` before that model event is handled. `TOOL_STARTED` is
  persisted before the tool body is executed, including whether the tool is
  side-effecting.
- The existing `TURN_COMPLETED` transaction remains the only commit point. It
  atomically writes the completion event, final session items, title/search
  projection, optional execution record, task terminalization, and committed
  attempt resolution. Failure and cancellation use a corresponding atomic
  terminalization transaction.
- Explicit abandon of a linked `RUNNING` plan task is also one SQLite
  transaction: it validates the session, task identity, task kind, and current
  status, then writes `RUNNING → CANCELLED`, `SESSION_TASK_CANCELLED`,
  `TURN_ABANDONED`, and the attempt `ABANDONED` resolution in deterministic
  task-event-before-turn-event order. Ordinary user attempts without a task
  keep the existing abandon path.
- A recovery scanner ignores ordinary `FAILED` and `CANCELLED` terminal
  attempts. Restart never writes `ABANDONED`; only the explicit recovery
  operation does. `INDETERMINATE` is never automatically replayed.
- The exact retry input is the bounded, turn-owned `TurnInput` projection:
  prompt, ordered content parts, source, and plan identity flags. It is
  fingerprinted and stored only when reconstructable and at most 256 KiB.
  Provider request bodies, headers, credentials, system context, tool
  arguments, and unbounded outputs are not recovery storage.
- CLI, TUI, and the ACP private
  `neuro-code/session/recovery` extension share the application recovery
  service. Their default inspect view is limited to unresolved attempts;
  committed/abandoned history remains available through an explicit audit
  view. They expose bounded evidence and explicit `abandon`; `retry` is
  available only for a pre-output non-plan user turn with exact input. Plan
  execution retry is unsupported, even when its safety classification is
  `SAFELY_RETRYABLE`. A retry abandons the old attempt and starts a new turn
  identity; it never resumes the old attempt in place.

## Recovery classification

| Durable facts | Classification | Automatic replay |
|---|---|---:|
| Atomic completion and attempt resolution `committed` | `COMMITTED` | No |
| Open non-plan user attempt, exact input, no output, no tool start, and no fact conflict; a request may have been marked started | `SAFELY_RETRYABLE` | No; explicit retry only |
| Open plan attempt with exact task ownership, exact input, no output, no tool start, and no fact conflict | `SAFELY_RETRYABLE` | No; retry unavailable, explicit abandon only |
| Observable model output, any tool start, side-effecting tool start, missing exact input, background wake, or conflicting facts | `INDETERMINATE` | Never |
| Explicit durable `TURN_ABANDONED` resolution | `ABANDONED` | No |

The current retry policy is intentionally conservative. A provider-specific
capability that can produce an external effect before its first observable
event must remain `INDETERMINATE`; no such hidden effect is inferred to be
safe from the presence of `MODEL_REQUEST_STARTED` alone.

## Transaction and migration model

Schema version 14 adds the foreign-keyed `session_turn_attempts` table and an
index by session/resolution. Migration 13 → 14 creates the table without
rewriting existing sessions; legacy sessions therefore have no interrupted
attempt and resume unchanged. The row stores bounded identity, input
fingerprint/reconstructability, sticky request/output/tool facts, latest
stage, and terminal resolution. It does not duplicate the full model request.

The storage port owns ordinary `start_turn_attempt`, atomic plan acceptance
with task ownership, recovery-fact append, completion, failure/cancellation,
and explicit abandonment. SQLite write-lock and transaction boundaries are the
durability boundary. A failed index/search or recovery transition rolls the
entire owning transaction back, including the attempt resolution, event, task
state, and session items.

## Consequences

Normal resume is blocked while an unresolved attempt exists. A user must first
inspect it and either explicitly retry a safe non-plan user turn or abandon it.
An indeterminate attempt can be audited and abandoned, but this phase does not
implement mid-turn continuation, tool compensation, workspace rollback,
background-child reconciliation, plan execution retry, or automatic replay.

The durable input projection can contain user-provided text or stable media
references, so it follows the existing session retention boundary. Interface
projections expose only bounded metadata and never render that input or secret
provider fields.

## Evidence

Focused tests cover pre-output safe classification, request/output/tool
write-ahead facts, atomic plan acceptance and abandon rollback, explicit plan
task ownership, normal failure filtering, migration, and real child-process
exit/reopen. A process exit after a durable output marker reopens as
`INDETERMINATE`; a process exit after atomic plan acceptance reopens with the
exact `RUNNING` task owner; a process exit after the atomic commit reopens as
`COMMITTED`.
