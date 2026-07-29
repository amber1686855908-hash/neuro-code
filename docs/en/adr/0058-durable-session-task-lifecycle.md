# ADR 0058 — Durable session-task lifecycle for plan execution

[简体中文](../../zh-CN/adr/0058-durable-session-task-lifecycle.md) · **English**

## Status

Accepted

## Context

ADR 0057 makes a user-confirmed plan-to-execution handoff durable, but an event
alone cannot answer whether that work is still running, completed, failed, or
was cancelled. The existing `BackgroundTaskManager` owns live shell process
trees and intentionally discards their records on a binding switch or process
exit. Reusing it for plan execution would conflate process ownership with
conversation state, weaken its cleanup invariant, and incorrectly imply that a
plan turn owns a child process.

Future subagents need an observable lifecycle, but exposing a scheduler or
worker protocol before there is a user-visible contract would make an
unimplemented feature look available. This slice must establish the durable
boundary without starting independent work or broadening any authority.

## Decision

- The domain owns immutable `SessionTask` values. Their opaque IDs are bounded
  and control-character-free; their canonical kinds are `plan_execution` and
  reserved `subagent`; their states are `running`, `completed`, `failed`, and
  `cancelled`. A task has exactly one start time and a terminal task has exactly
  one finish time after that start time. It can transition from `running` to a
  terminal state once only.
- `SessionStore` owns creation, update, and bounded listing of task metadata.
  SQLite schema v7 stores it in a foreign-keyed `session_tasks` table. Tasks
  cascade on session deletion, are neither copied by a fork nor included in
  visible-content search, JSON export, or imported historical Rust sessions.
- A valid `/execute-plan`/`/run-plan` handoff creates one `plan_execution`
  task. `AgentRuntime` emits task-started and execution-requested events before
  the canonical user message. It records task completion, failure, or
  cancellation before the matching turn-terminal event. No task is created for
  an ordinary prompt or for a rejected handoff without a saved plan.
- The TUI `/tasks` command locally combines current binding-scoped background
  snapshots with durable session-task metadata. It shows no prompt, command,
  model output, raw tool output, or credential. It cannot cancel either kind of
  task. Existing `kill_task` remains the only model-facing process cancellation
  operation and still passes through permission and approval policy.
- This is a record of a turn, not a task scheduler. It does not wake a model,
  run a subagent, retry a task, assign work, provide plan comments as part of
  its lifecycle, or create a new permission/workspace/sandbox bypass.
  `subagent` is reserved only so that a
  future vertical slice can use the same lifecycle value without a schema
  redesign.

## Consequences

Users can inspect a bounded, durable account of the execution handoffs they
explicitly made, including a cancelled or failed turn, after resuming a session.
The interface makes the distinction between that conversation metadata and an
owned live shell process explicit.

Bounded current-plan feedback is separately defined by
[ADR 0059](0059-bounded-current-plan-comments.md). The immutable plan revision
attached to a plan-execution task is subsequently defined by
[ADR 0060](0060-plan-execution-revision-snapshots.md). Subagent execution,
task comments, task graphs, assignment, retries, and task cancellation semantics
still require their own user-visible contracts and application services. The
reserved kind must not be treated as implemented until that slice exists.
