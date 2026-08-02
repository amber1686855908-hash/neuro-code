# ADR 0062: promote long foreground Bash commands into owned background tasks

[简体中文](../../zh-CN/adr/0062-automatic-foreground-to-background-promotion.md) · **English**

- Status: accepted
- Date: 2026-08-02

## Context

The managed Bash surface already owns background process trees, but a normal
foreground call used to terminate its tree when the tool wait budget expired.
That made long tests, builds, and services impossible to continue without
starting the command a second time. A promotion must preserve the one approved
side effect, the conversation owner, and the existing sandbox boundary.

## Decision

When `BashTool` was constructed with background management enabled, a normal
foreground call has a bounded foreground wait budget. The command is started
once, from the beginning, by the conversation-scoped `BackgroundTaskManager`
using the existing `ProcessTree`, shell-sandbox launch, protected-environment
stripping, output bounds, and permission decision. The manager receives no
task-level deadline for this path: the foreground budget is only how long this
tool call waits.

If the same task reaches a terminal state within the budget, Bash returns the
ordinary bounded foreground result and removes that terminal record. This
prevents a duplicate completion reminder and does not expose a task ID. If it
is still running when the budget expires, Bash returns its opaque task ID with
`status: running`, `is_background: true`, and
`promoted_from_foreground: true`. The model can use the existing
`task_output`, `wait_tasks`, and `kill_task` tools; the result metadata contains
no command, cwd, environment, or output preview.

Cancellation during the foreground wait terminates that same process tree,
waits for bounded cleanup, discards its terminal record, and propagates
`CancelledError`. Startup, manager, and terminal-discard failures remain
errors; they cannot silently report success or leave a live task unowned. The
managed registry's running or retained-task capacity is not a new limit on
ordinary foreground Bash: when it cannot accept the launch, Bash uses the
established bounded direct foreground path instead. A detached descendant that
retains an inherited output pipe is outside the owned process tree; bounded
capture marks that managed task failed rather than allowing `kill`, shutdown,
or cancellation to wait forever.
Explicit `is_background=true` keeps its immediate-start behavior and its
existing explicit task deadline. A disabled background capability, or a
missing manager, keeps the existing foreground timeout-and-terminate behavior.
Scope shutdown and application shutdown retain ownership of promoted tasks.

## Non-goals

This decision does not add full-output files, cross-process task restoration,
general tool-timeout promotion for other tools, or automatic replanning. It
does not change Provider, Runtime/Finalizer, persistence, permission, or
sandbox policy and does not require an application restart.

## Validation

Focused Bash and background-manager tests cover one-launch promotion,
terminal-record disposal, scope isolation, cancellation, explicit background
deadlines, sandbox/environment reuse, output bounds, capacity fallback,
detached-pipe cleanup, and existing timeout and shutdown behavior.
