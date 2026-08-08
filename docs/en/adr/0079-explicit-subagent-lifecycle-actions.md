# ADR 0079: Explicit Subagent Lifecycle Actions

- Status: accepted
- Date: 2026-08-07
- Scope: Stage5CY

## Context

Stage5CX added a read-only TUI relationship view for parent sessions and
their read-only child subagents. The view exposed safe capability labels, but
it did not execute lifecycle operations. A later action must not turn those
labels into UI-owned storage or runtime behavior.

## Decision

Stage5CY adds `SubagentRelationshipLifecycleService` in the application
sessions layer. It accepts one typed action for a parent task relationship:

- `resume` validates the parent-owned link and terminal `SUBAGENT` task, then
  returns the validated child session ID. It does not run a model, replay
  tools, or reuse a finalizer context.
- `fork` delegates the existing `SessionLifecycleController` fork operation
  for the child session and returns only the new session ID. The fork is not
  automatically opened or registered as a new child task.
- `delete` delegates deletion of the linked child session, never the parent
  session.

The service fails closed when the relationship, parent task, or child session
is missing, when the task is not a `SUBAGENT`, or while the task is active.
Identifiers are bounded and control-character free. A self-referential link
is rejected.

The TUI exposes these actions through the explicit command
`/subagents ACTION TASK_ID`. It calls the application controller and renders
only the bounded result. It does not access SQLite, transcripts, tools,
permissions, or provider state. Resume selects the child session through the
existing session-selection boundary; fork reports the new ID without opening
it; delete reports the deleted child ID.

Validation and the delegated mutation are separate calls. This stage makes no
cross-process atomicity claim; existing session lifecycle owners remain
responsible for their established locking and persistence semantics.

## Non-goals

This stage does not add automatic scheduling, recursive spawning, parallel
children, parent-context reuse, write-capable tools, checkpointing, worktrees,
or a new session schema.

## Consequences

Lifecycle controls now have a narrow application seam that can later be reused
by other inbound interfaces without duplicating ownership checks. The TUI
remains an adapter, and lifecycle operations remain explicit and bounded.
