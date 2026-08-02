# ADR 0063: Bounded Explicit Plan Task Scheduling

- Status: accepted
- Date: 2026-08-02

## Context

Plan execution is already an explicit, permission-gated handoff. A user may
need to prepare several reviewed plan revisions before starting one, but the
application must not silently turn planning into autonomous scheduling or
subagent execution.

## Decision

The application exposes two local, explicit TUI commands:

- `/schedule-plan` (alias `/queue-plan`) stores one immutable snapshot of the
  current saved plan without contacting a provider.
- `/run-task TASK_ID` starts exactly one queued plan task after the user names
  its opaque ID.

Each session may contain at most four queued `PLAN_EXECUTION` tasks. The limit
is checked in the conversation for immediate feedback and again by the SQLite
store inside the task-creation transaction for cross-instance safety. A task
is claimed with `SessionStore.start_session_task`, which atomically changes
`queued` to `running`; only a queued task can be claimed. Existing
`/execute-plan` and `/run-plan` continue to create and run an immediate
`running` task, so their behavior is unchanged.

The existing `session_tasks.status` text column already accepts canonical
values, so this lifecycle extension does not require a schema-version bump.
Older databases continue to load because the new `queued` value is interpreted
by the domain model rather than by a migration.

Queued tasks never auto-start, retry, wake a model, execute tools outside the
normal plan handoff, or spawn subagents. They remain session-owned durable
metadata and immutable plan snapshots. Fork, export, and import behavior keeps
the existing session-task policy: task records are not copied or serialized
into snapshots.

## Consequences

The scheduler is deliberately a bounded persistence and explicit-claim seam,
not a background scheduler. Runtime budgets, provider contracts, permission
checks, tool execution, cancellation, and Finalizer behavior are unchanged.
Future subagent orchestration or automatic scheduling requires a separate ADR
and a new explicit lifecycle contract.
