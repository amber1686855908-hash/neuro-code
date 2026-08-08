# ADR 0080: Explicit CLI Subagent Lifecycle Actions

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5CZ

## Context

Stage5CY added explicit TUI controls for a linked read-only child session. The
same application lifecycle owner should be reusable from a headless interface,
without making the CLI responsible for storage, model execution, or child
context reconstruction.

## Decision

Stage5CZ adds the `neuro subagents ACTION TASK_ID --parent-session SESSION_ID`
command. `ACTION` is one of `resume`, `fork`, or `delete`. The command opens
the existing composition boundary, validates the parent session workspace,
and delegates to `SubagentRelationshipLifecycleService`.

- `resume` returns a bounded child-session selection result. It does not start
  a model turn or replay tools.
- `fork` delegates the existing session lifecycle fork and reports the new
  session ID; it does not open or relink the fork automatically.
- `delete` deletes only the linked child session after application-level
  relationship and terminal-task checks.

Plain output contains only a short lifecycle message. `--json` uses the typed
CLI serializer and emits parent/task/child identifiers, the canonical action,
and an optional forked-session identifier. Prompts, transcript items, events,
tool arguments, credentials, provider state, and child context are never
serialized.

## Boundaries

The command is explicit and bounded. It does not schedule children, run a
model, create recursive or parallel children, add write-capable tools, or
claim cross-process atomicity beyond the existing session lifecycle owner.
Resume remains a selection operation; a later user command is required to
start a child turn.

## Consequences

CLI, TUI, and future inbound adapters can share one application lifecycle
contract. The CLI remains an adapter and does not read SQLite directly. No
schema, provider, runtime-kernel, finalizer, or normal-agent behavior changes.
