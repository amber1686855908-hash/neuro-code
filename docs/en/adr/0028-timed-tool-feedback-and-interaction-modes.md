# ADR 0028 — Timed tool feedback and interaction modes

[简体中文](../../zh-CN/adr/0028-timed-tool-feedback-and-interaction-modes.md) · **English**

## Status

Accepted.

## Context

The event-stream TUI exposed tool lifecycle names but not enough information to
understand what was running, why it was allowed, or how long it took. It also
had no persistent operating-mode indicator. Copying another agent's labels
without mapping them to Neuro Code's permission boundary would be dangerous:
an `Auto` label must not silently bypass explicit rules or the process sandbox.

Provider latency is observable, but a client cannot measure a model's private
internal reasoning phase exactly. Timing labels therefore need a precise local
definition instead of claiming provider-internal telemetry.

## Decision

- `AgentRuntime` emits `MODEL_THINKING_COMPLETED` once per model step. Its
  monotonic duration runs from `MODEL_STEP_STARTED` until the first visible text,
  local tool call, hosted-tool activity, or terminal model completion. The TUI
  labels this as “Thought for”; it is client-observed time to the first actionable
  result, not proof of private model reasoning time.
- `TURN_COMPLETED` and `TURN_FAILED` carry monotonic whole-turn duration.
  Successful TUI turns render the elapsed summary after the final assistant
  message. Local and provider-hosted tool terminal events also carry elapsed
  duration.
- Tool feedback is a bounded tree: invocation, permission path or approval,
  result preview, then completion/failure with elapsed time. Invocation previews
  only use selected useful arguments such as path, command, pattern, query, or
  task ID. ADR 0029 extends this timing contract with one in-place card per call,
  redacted actual output, and bounded workspace-change diffs.
- `InteractionMode` defines four application-owned modes, cycled with
  `Shift+Tab` or selected through `/mode MODE`:

  | Mode | Permission behavior |
  | --- | --- |
  | `normal` | Read-only tools run automatically; side effects ask. |
  | `accept-edits` | Read-only and workspace edit tools run automatically; commands and other effects ask. |
  | `plan` | Read-only tools run automatically; unmatched side effects are denied without prompting. |
  | `auto` | Safe preview uses `accept-edits` behavior until a safety classifier exists. An explicit startup `--always-approve` retains its existing bypass authorization. |

- Explicit deny/ask rules are evaluated before mode defaults. Every mode remains
  inside the configured workspace adapters and process sandbox. Prompt guidance
  describes the selected mode but cannot grant permission.
- Mode changes are serialized with turns, reapplied to new provider/session
  bindings, and stored with language and reasoning effort in the user-only UI
  preferences file. A corrupt or unknown value falls back to `normal`.
- The runtime bar shows the compact home-relative working directory and current
  mode alongside model, context usage, and reasoning effort. The primary theme
  uses cool blue, violet, cyan, and green semantic roles; warm color is reserved
  for warnings and errors.

## Consequences

Users can see when the model becomes actionable, the total cost in wall time of
each turn, and the lifecycle of every tool call without exposing unbounded or
unredacted results. Modes are useful immediately while remaining honest about incomplete automation.
`Auto` cannot be mistaken for an implemented model-based safety classifier, and
the existing explicit `--always-approve` switch remains the only way to request
unrestricted permission defaults.

Durations are process-local observations and may include network scheduling,
provider queueing, approval wait time, or streaming overhead. Interactive card
expansion, provider-native reasoning telemetry, and an automatic safety
classifier remain future vertical slices.
