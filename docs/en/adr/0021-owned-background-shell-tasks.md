# ADR 0021: own background shell tasks within the application lifetime

[简体中文](../../zh-CN/adr/0021-owned-background-shell-tasks.md) · **English**

- Status: accepted
- Date: 2026-07-18
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

A coding agent needs long-running test, build, server, and monitoring commands
without blocking a model step. Launching a shell with `&` and forgetting it is
not sufficient: output becomes unbounded or inaccessible, cancellation reaches
only the shell leader, and exiting the CLI can leave descendants behind.

The fixed Rust baseline separates two capabilities. Its Bash terminal backend
returns a task ID for background commands and exposes snapshot/wait/kill tools.
Its interactive PTY sessions additionally support input and resize through the
ACP surface. The current Python runtime can deliver the former as a complete
local-tool slice without prematurely coupling M3 process ownership to the
future M4 ACP protocol.

## Decision

`BackgroundTaskManager` is the conversation-scope application port and
`LocalBackgroundTaskManager` is the first application supervisor/platform
adapter. The CLI/TUI composition root creates one supervisor. A TUI conversation
binding receives an isolated manager scope through `ToolContext`; a one-shot
headless run uses the supervisor's root scope. The supervisor owns every
`ProcessTree`, watcher task, and bounded combined stdout/stderr preview. It
permits at most 16 running tasks across the process, while each scope retains at
most 64 task records; older completed records are evicted before a new task
starts.

The model-facing contract is:

- `bash` accepts optional `is_background`. `true` returns a task ID immediately;
  when background management is enabled, an omitted or `false` value waits for
  the foreground budget and promotes a still-running command to that same task
  without restarting it. If the command finishes within the budget, its
  terminal record is discarded and the result has normal foreground metadata.
- An omitted background `timeout_seconds` means no tool-level deadline. A
  positive explicit value terminates the task after that interval.
- `task_output` returns a non-blocking snapshot by default or waits for at most
  30 seconds. Status is one of `running`, `completed`, `failed`, `timed_out`, or
  `cancelled`.
- `wait_tasks` performs a bounded event-driven wait for any or all requested
  tasks as refined by [ADR 0024](0024-event-driven-multi-background-task-wait.md).
- `kill_task` is side-effecting and therefore passes through ordinary
  permission/approval policy. It is idempotent for a known completed task.

Background output never grows without bound. The adapter counts all received
bytes but retains only a configured head/tail preview in memory. It merges
stderr into stdout at process creation so the captured stream preserves the
operating system's pipe order. Provider and proxy credentials are stripped
before both foreground and background launches, and sandboxed launches use the
same `LocalProcessSandbox` request boundary.

`ProcessTree.wait` waits for the direct child and then the owned POSIX process
group or Windows Job Object. This keeps a shell command containing an internal
background operator owned even after its shell leader exits. POSIX explicit
timeout, `kill_task`, cancellation while a launch is in flight, and manager
shutdown use the existing bounded TERM-to-KILL sequence; Windows uses immediate
whole-Job termination with kill-on-close as a backstop. Headless shutdown and
TUI exit always call supervisor shutdown. Switching a TUI provider profile or
session closes the previous conversation scope after the new binding has been
validated. No task is deliberately detached from its conversation or
application.

## Consequences

- Models can start, inspect, wait for, and stop long-running commands through a
  testable capability instead of sleep-based polling or unmanaged shell jobs.
- Task records and output previews are process-local. They are not persisted in
  SQLite and cannot survive application restart or session resume.
- A one-shot headless invocation may use a background task during its tool loop,
  but any still-running task is terminated when that invocation returns. A TUI
  task may live across turns in the same binding until it finishes, is killed,
  the binding changes, or the TUI exits.
- TUI metadata visibility and local completion notices are refined by
  [ADR 0022](0022-session-scoped-background-task-visibility.md), while explicit
  model-boundary completion metadata is defined by
  [ADR 0023](0023-model-visible-background-task-completion-reminders.md). Full
  output files and a shared subagent task namespace remain future slices;
  automatic foreground-to-background promotion is defined by ADR 0062.
- ACP PTY create/input/resize/ring-buffer/close behavior remains separate. It
  will build on the process-ownership boundary rather than changing this tool
  contract.
- Windows Job Object ownership and failure behavior are specified by
  [ADR 0031](0031-fail-closed-windows-job-objects.md); atomic creation-time Job
  assignment and restricted standard-handle inheritance are specified by
  [ADR 0033](0033-atomic-windows-job-process-creation.md).

Source evidence is the historical Bash, task-output, kill-task, local-terminal,
and background-task user-guide behavior at the pinned commit.
