# ADR 0060 — Immutable revisions for plan-execution tasks

[简体中文](../../zh-CN/adr/0060-plan-execution-revision-snapshots.md) · **English**

## Status

Accepted

## Context

ADR 0058 records the lifecycle of a user-confirmed plan execution, while ADR
0059 lets a user give feedback on the current plan revision. Once the model
replaces the current plan, the durable task record alone cannot identify which
revision was handed to the execution turn. The historical event stream contains
the request, but it is not a small read-only task audit surface.

Copying the mutable current plan into a task view would make history change
retroactively. Treating this as a scheduler, an approval record, or a subagent
result would likewise claim authority that the task lifecycle does not have.

## Decision

- A `SessionTask` may carry an immutable `SessionPlan` snapshot only when its
  kind is `plan_execution`. The same bounded domain validation applies. Legacy
  task records remain valid with no snapshot, and a `subagent` task cannot
  carry one.
- Before an execution turn starts, `AgentRuntime` requires a saved plan and
  stores that exact revision on the new task. Terminal lifecycle transitions
  preserve the snapshot; a later `update_plan` cannot change history.
- SQLite schema v9 adds a bounded `plan_snapshot_json` field to
  `session_tasks`. The v8 migration supplies an empty value for existing tasks.
  Snapshots are excluded from FTS, JSON export, and Rust-session import, and
  session tasks (including their snapshots) are never copied by a fork.
- `/tasks` stays read-only. For a snapshot-bearing execution task, it displays
  only the first 12 characters of the revision fingerprint and the completed
  step count. It does not render plan text, prompts, commands, tool output, or
  credentials.
- The snapshot creates no approval, execution, retry, scheduling, workspace,
  sandbox, or subagent authority. ACP receives no new task method in this
  slice.

## Consequences

Users can distinguish the planned revision that an old execution task used even
after the active plan has evolved, without exposing a raw transcript in task
lists. Older database records remain readable and simply have no revision
summary.

The later user-initiated, current-session read-only display of one stored
snapshot is defined by [ADR 0061](0061-read-only-plan-execution-inspection.md).
Associating task results with individual steps, approval workflows, task graphs,
scheduling, and subagent orchestration remain separate capabilities.
