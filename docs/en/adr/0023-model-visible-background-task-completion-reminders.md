# ADR 0023: report background-task completions at explicit model boundaries

[简体中文](../../zh-CN/adr/0023-model-visible-background-task-completion-reminders.md) · **English**

- Status: accepted
- Date: 2026-07-18
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

ADR 0022 made session-scoped background tasks visible to the person using the
TUI, but the model still had to poll `task_output` to discover natural
completion. The fixed Rust baseline has three related surfaces: completion
reminders attached to later tool results, between-turn reminders, and optional
auto-wake turns. It suppresses duplicate reminders after a blocking task-output
or multi-task wait result, or an explicit kill, because those tool results
already informed the model.

Immediate auto-wake would start a new paid model turn without fresh user input
and would need queueing, cancellation, and cost controls that the current TUI
does not yet have. Persisting a synthetic reminder as an ordinary session
message is also unsafe: local task records do not survive restart, so replaying
an old pointer would create stale model context.

## Decision

Each conversation-scoped `BackgroundTaskManager` tracks whether a terminal task
has been reported to the model. It exposes pending terminal snapshots and an
idempotent acknowledgement operation. `task_output` and `wait_tasks`
acknowledge terminal snapshots before returning them, and `kill_task`
acknowledges its result, so the next model step does not repeat information
already present in a tool result.

Before every explicit model step, `AgentRuntime` reads the current scope's
pending completions. It injects at most 20 entries as a model-only user-role
runtime notice. Each entry is JSON-escaped and contains only task ID, status,
exit code, total output bytes, and whether the preview is truncated. Command
text, working directory, and captured output are excluded. The notice points to
`task_output` only when that tool is present. Overflow remains pending for a
later model boundary.

The reminder is supplied to provider context for the current run but is not
added to `SessionItem`, returned message history, or SQLite conversation items.
A metadata-only `background_task_completion_reminder` event remains in the
audit stream. The manager acknowledges a batch only after the provider produces
a valid completion event. If streaming fails before that point, a later retry
can report the same batch rather than losing it.

No autonomous turn is created. A task that completes during tool execution can
be reported at the next model step in that turn. A task that completes while
the conversation is idle waits for the next explicit user prompt. Existing TUI
polling remains presentation-only and does not consume model reminders.

## Consequences

- Natural completion becomes visible to the model without repeated polling,
  while task scope and application process ownership remain unchanged.
- Blocking output/multi-task reads and explicit kills have one canonical
  model-facing result and do not generate a duplicate reminder.
- Reminder size and content are bounded independently of command/output size;
  commands and credentials cannot enter through this status path.
- Provider failure does not consume an unseen completion, while a successful
  provider step acknowledges it exactly once.
- Synthetic status is intentionally absent from durable conversation replay;
  persisted audit events record that an injection was attempted.
- Auto-wake turns, user-configurable wake policy, queue/preemption semantics,
  and completion output injection remain future slices. Multi-task waits are
  defined separately by [ADR 0024](0024-event-driven-multi-background-task-wait.md).

Source evidence is the historical `TaskCompletionReminder`, reported-completion
state, between-turn drain, and block-wait/explicit-kill suppression behavior at
the pinned commit.
