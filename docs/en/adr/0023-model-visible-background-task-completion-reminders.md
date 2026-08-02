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

An unrestricted auto-wake would start a new paid model turn without fresh user
input and would need queueing, cancellation, and cost controls. The policy
therefore has a persisted user-wide default plus an optional override on each
managed provider profile; both default to disabled for legacy and new settings.
The implementation also keeps a session-scoped `/auto-wake on|off` override,
which takes precedence while the TUI is open. Persisting a synthetic reminder
as an ordinary session message is also unsafe: local task records do not
survive restart, so replaying an old pointer would create stale model context.
The scheduler therefore persists only a bounded wake ledger per session; it
never stores command text, working directories, output, credentials, or a
synthetic task snapshot.

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
text and working directory are excluded. Ordinary user turns remain
metadata-only. An explicit background auto-wake may additionally include a
redacted output preview, bounded to 2 KiB per task and 8 KiB per reminder
batch. The preview is framed as untrusted evidence, never as user intent, and
the notice still points to `task_output` only when that tool is present.
Overflow remains pending for a later model boundary.

The reminder is supplied to provider context for the current run but is not
added to `SessionItem`, returned message history, or SQLite conversation items.
A metadata-only `background_task_completion_reminder` event remains in the
audit stream. The manager acknowledges a batch only after the provider produces
a valid completion event. If streaming fails before that point, a later retry
can report the same batch rather than losing it.

The default still creates no autonomous turn. A task that completes during
tool execution can be reported at the next model step in that turn. An idle TUI
session may explicitly opt in with `/auto-wake on`; it starts at most one
model-only wake for the current pending completion batch, and never starts a
wake while another turn is running. Completion reminders are consumed only
after a valid model completion, and the wake's synthetic reminder and assistant
response remain outside durable conversation items. `/auto-wake off` disables
the policy again for the current session. The Settings screen edits the
persisted global default, and the provider profile editor offers
inherit/enabled/disabled for a per-provider override. Existing TUI polling
remains presentation-only while the effective policy is disabled. Each session
wake first persists an in-flight marker. Only a successfully completed wake
consumes its pending IDs and one bounded session-budget unit; a failed or
cancelled wake retains the IDs and applies only the cooldown. An in-flight
marker is cleared on restart and pending IDs are reconciled against unreported
terminal tasks from the task supervisor, so an interrupted wake can be retried
only when the real task still exists and the budget permits it.

## Consequences

- Natural completion becomes visible to the model without repeated polling,
  while task scope and application process ownership remain unchanged.
- Blocking output/multi-task reads and explicit kills have one canonical
  model-facing result and do not generate a duplicate reminder.
- Reminder size and content are bounded independently of command/output size;
  commands, working directories, and credentials cannot enter through this
  status path. Auto-wake output previews are redacted, transient, and bounded.
- Provider failure does not consume an unseen completion, while a successful
  provider step acknowledges it exactly once.
- Synthetic status is intentionally absent from durable conversation replay;
  persisted audit events record that an injection was attempted.
- Full-output files and cross-process task restoration remain future slices.
  Automatic foreground-to-background promotion uses the same in-memory task
  record and therefore follows these completion-reminder rules; it does not
  make the persisted wake ledger a substitute for a surviving process tree or
  task result. Missing task IDs are discarded during reconciliation.
  Multi-task waits are defined separately by [ADR 0024](0024-event-driven-multi-background-task-wait.md).

Source evidence is the historical `TaskCompletionReminder`, reported-completion
state, between-turn drain, and block-wait/explicit-kill suppression behavior at
the pinned commit.
