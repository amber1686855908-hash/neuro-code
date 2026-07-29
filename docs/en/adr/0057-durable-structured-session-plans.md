# ADR 0057 — Durable structured session plans

[简体中文](../../zh-CN/adr/0057-durable-structured-session-plans.md) · **English**

## Status

Accepted

## Context

`plan` was already a safe interaction mode: it allowed exploration while the
permission policy rejected unmatched side effects. That policy alone does not
give a user a durable, inspectable account of the work the model intends to do.
A provider-specific plan protocol, a writable plan file, or a private UI-only
cache would either weaken the existing application boundary or make resumed and
forked sessions disagree.

Reference agents have richer approval and task systems, but their private
protocols are not a compatible API for this project. This slice must provide a
small useful capability without claiming to implement plan approval, comments,
or subagents.

## Decision

- The domain owns immutable `SessionPlan` and `PlanStep` values. A replacement
  has one to twelve steps, an optional bounded purpose, and only `pending`,
  `in_progress`, or `completed` status. The domain rejects control characters,
  unknown fields, malformed JSON shapes, and oversized values.
- The provider-neutral, non-side-effecting `update_plan` tool accepts one whole
  replacement. It does not write a plan file, edit the workspace, execute a
  command, or grant any permission.
- `AgentRuntime` recognizes only the tool's canonical metadata, persists it
  through the `SessionStore` port before reporting success, emits
  `PLAN_UPDATED`, and adds the current rendering to later model requests. A
  runtime that has no durable session store fails the tool call rather than
  claiming the plan was saved.
- SQLite schema v6 stores the bounded JSON value separately from ordered
  session items. Resume restores it before the next prompt and a durable fork
  copies it. It is deliberately outside FTS visible-content indexing, JSON
  export, and imported Rust-session data.
- The Textual UI adds `/plan DESCRIPTION`, which selects the already-safe plan
  interaction mode and submits the description, plus `/view-plan` and
  `/show-plan` to render the saved plan locally in the selected interface
  language. The UI never parses provider output to infer plan state. Once a
  plan exists, `/execute-plan` (alias `/run-plan`) is an explicit user-only
  handoff: it changes only to `accept-edits`, delegates one execution turn to
  the active conversation, and never selects `auto` or bypasses permissions.
- The runtime rejects a handoff without a saved plan. For a valid handoff it
  appends `PLAN_EXECUTION_REQUESTED` with the bounded plan payload before the
  canonical user message. The event is stored through the ordinary session
  event port, while the execution prompt continues to receive current plan
  guidance. Command/network approval, workspace containment, explicit rules,
  and sandbox enforcement are unchanged.

## Consequences

Users can return to or fork a session without losing its current high-level
work plan, and every provider uses the same tool shape. A plan update remains
auditable through the ordinary runtime event sequence while model guidance does
not pollute canonical message history.

The feature now provides a small reviewed plan-to-execute transition without
introducing automatic execution. Per-step feedback is subsequently defined by
[ADR 0059](0059-bounded-current-plan-comments.md); assigned workers, a shared
task graph, plan files, ACP plan methods, and subagent orchestration still
require separate user-visible contracts and lifecycle ownership rather than an
expansion of this metadata field.
