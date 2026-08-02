# ADR 0022: scope background-task visibility to the active conversation

[简体中文](../../zh-CN/adr/0022-session-scoped-background-task-visibility.md) · **English**

- Status: accepted
- Date: 2026-07-18
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

ADR 0021 introduced process-owned background shell tasks, but its first
composition shared one registry across every TUI conversation binding. That is
broader than the fixed Rust baseline, whose background registry is per session.
After a profile switch or in-process resume, a shared registry could expose task
IDs from the previous conversation or leave work running without a reachable
owner.

The user also needs to see that work without asking the model repeatedly. A
local interface must not print raw task output or potentially sensitive command
text, and it must not create a cancellation shortcut that bypasses the existing
`kill_task` permission and approval path.

## Decision

The composition root owns one `BackgroundTaskSupervisor`. Every TUI
`ConversationBinding` receives a new `BackgroundTaskManager` scope. A scope can
start, inspect, wait for, and kill only its own task IDs. The supervisor retains
process ownership across all scopes so application shutdown can still terminate
every live tree.

A successful provider or session switch validates the replacement binding,
then closes the previous scope before publishing the new binding. Closing a
scope terminates its running process trees and drops its records. A rejected
replacement binding closes its newly allocated scope. Selection results report
how many live tasks were stopped so the TUI can make cleanup visible.

The TUI adds an argument-free, read-only `/tasks` command. It lists at most 20
records from the active scope and shows only the full task ID, lifecycle status,
exit code, output byte count/truncation flag, and start time. It does not render
the command or captured output. A bounded periodic poll emits one local notice
when each task reaches a terminal state. Poll failures remain silent to avoid
repeated transcript noise; an explicit `/tasks` request reports its error.

`/tasks` cannot terminate work. Cancellation remains the model-facing
`kill_task` tool and therefore continues through `PermissionManager` and any
interactive approval. Model-visible, metadata-only completion reminders are a
separate boundary defined by
[ADR 0023](0023-model-visible-background-task-completion-reminders.md); this
slice does not wake the model automatically or inject completion output.

## Consequences

- Task IDs and records cannot cross profile/session bindings, while the
  application still owns every child process until cleanup completes.
- Switching bindings deliberately terminates old-scope work instead of leaving
  inaccessible commands running; the number stopped is shown to the user.
- Fast tasks that finish between polls still produce one terminal notice, and
  repeated polls do not duplicate it.
- The task list is useful for status and ID discovery without copying raw tool
  results or command text into scrollback.
- Cross-process task restoration, full-output files, a rich task pane, and
  direct user cancellation with a separately designed approval flow remain
  future work. Model auto-wake now has an explicit bounded session policy with
  persisted global/provider configuration and a restart-aware wake ledger; it
  does not pretend process-owned task results survive an application restart.
  Automatic foreground promotion uses this same scope and process ownership;
  it does not add cross-process restoration.

Source evidence is the per-session `BackgroundTaskRegistry`, task-completion
notifications, and task output/kill behavior at the pinned historical commit.
