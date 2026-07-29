# ADR 0061 — Read-only inspection of a plan-execution snapshot

[简体中文](../../zh-CN/adr/0061-read-only-plan-execution-inspection.md) · **English**

## Status

Accepted

## Context

ADR 0060 gives each plan-execution task an immutable snapshot so a later plan
replacement cannot rewrite its history. The compact `/tasks` list intentionally
shows only a fingerprint prefix and completed-step count. Users still need an
explicit way to check which plan revision an execution used without expanding
every task list entry or treating historical text as a new instruction.

A task ID must not become a cross-session history search key. Reading the
snapshot must also not silently enter it into a provider request: a historical
plan can contain user-authored text and is evidence of a past handoff, not
authority to repeat it.

## Decision

- `SessionStore.get_session_task(session_id, task_id)` is an exact bounded read.
  It validates the opaque task ID, first requires the supplied session, and
  queries with both identifiers. It returns `None` when that task is not in the
  session; it has no global lookup, list expansion, or mutation behavior.
- `AgentConversation` and `ProfileConversationController` expose only that
  narrow read to the active TUI binding. It does not acquire the turn lock,
  start a model request, alter the current plan, append events, change a task,
  request permission, or invoke a workspace/platform adapter.
- The local TUI command `/view-task TASK_ID` is the sole interface in this
  slice. For a plan-execution task with a snapshot, it renders the full stored
  purpose, fingerprint, and steps with a clear read-only-reference notice.
  `/tasks` remains a compact summary. A missing task or a legacy task without a
  snapshot reports no historical detail.
- This command creates no execute, retry, scheduler, background-task, approval,
  workspace, sandbox, ACP, or subagent capability. It does not expose prompts,
  commands, raw tool output, or credentials.

## Consequences

Users can audit the plan that was actually handed to a completed, failed, or
cancelled execution without confusing it with the active plan. The snapshot is
shown only after an explicit local action and is contained to the currently
open session.

Cross-session history browsing, task-result-to-step association, task graphs,
scheduling, task retry/cancellation controls, ACP task APIs, and subagent
orchestration remain separate vertical capabilities.
