# ADR 0014 — Minimal event-stream TUI

[简体中文](../../zh-CN/adr/0014-minimal-event-stream-tui.md) · **English**

## Status

Accepted.

## Context

The headless agent loop already emits one normalized, append-only event stream,
but a usable terminal session needs durable multi-turn context, prompt input,
scrollback, streaming feedback, and local commands. Reimplementing the historical
terminal widget graph would couple application state to a UI framework and would
violate the vertical-slice rewrite rule.

The first M3 slice therefore needs a narrow interactive boundary that reuses the
M2 runtime and session store without claiming parity with approval dialogs, model
selection, rich rendering, or platform PTY behavior.

## Decision

- `AgentConversation` is the application-facing multi-turn controller. It owns
  the current ordered session items, session identifier, provider-origin
  metadata, resume workspace validation, and turn serialization. Both headless
  and interactive entry points use it.
- Textual is an optional interface dependency. Running `neuro-code` without a
  subcommand or prompt opens `NeuroCodeApp`; `neuro-code -p ...` and
  `neuro-code agent -p ...` retain the machine-friendly headless path.
- The TUI renders `AgentEvent` values and never mutates runtime state directly.
  Text deltas update the live response. Provider selection/failure and tool
  lifecycle events become bounded status lines. The later stable-message and
  localization design is specified by
  [ADR 0026](0026-stable-localized-tui-conversation.md).
- Assistant responses use safe Markdown rendering with an application-owned
  semantic theme, while local system/status/tool/error rows use an aligned
  label gutter and semantic value highlights. Model text is not Rich/Textual
  markup and hyperlink activation is disabled. The rendering boundary is
  specified by
  [ADR 0027](0027-semantic-tui-and-application-reasoning-effort.md).
- The presentation uses one application-owned cool neutral-dark theme. Textual's
  built-in command palette is disabled because its `Ctrl+P` and emoji search
  surface conflict with the application's provider picker and plain-text
  `/sessions QUERY` workflow. A persistent bar above the prompt displays the
  active provider/model, compact workspace path, context-window usage,
  requested/effective reasoning effort, and interaction mode.
- Full-screen terminal mode periodically compares the real TTY cell dimensions
  with the active Textual screen. It posts a normal resize event only when they
  differ, recovering from missing signal or in-band resize notifications. This
  fallback is not installed for headless, inline, or web drivers.
- Textual owns terminal application-mode setup and restoration; Neuro Code does
  not duplicate raw-mode, alternate-screen, cursor, or focus-tracking control.
  After `run_async` completes, the CLI propagates Textual's public
  `return_code`, while its composition-root `finally` always shuts down the
  background-task supervisor, including launch failures.
- Opt-in production-CLI smoke tests send a real `Ctrl+Q` through a Python-
  standard-library PTY on Linux/macOS and through the private stdlib ConPTY
  adapter on Windows. Without submitting a model prompt, they verify process
  exit codes and ordered enable/disable sequences for the alternate screen,
  cursor visibility, and focus tracking. POSIX compares complete `termios`;
  Windows also exercises idle `Ctrl+C`, resize, a non-zero console probe, and
  any available parent console modes. The ConPTY lifecycle is defined by
  [ADR 0032](0032-native-windows-conpty-lifecycle-evidence.md).
- Raw reasoning deltas and general tool argument/result mappings are not
  rendered. A bounded useful-argument allowlist supports invocation previews;
  completed calls expose only control-safe, credential-redacted and bounded
  output/change previews in the stable card defined by
  [ADR 0029](0029-auditable-in-place-tool-cards.md). Model-step, tool, and
  whole-turn durations use client monotonic clocks and follow
  [ADR 0028](0028-timed-tool-feedback-and-interaction-modes.md). Approval modals
  receive only the bounded action summary defined by ADR 0015.
- `/help`, `/status`, `/provider`, `/model`, `/effort`, `/reasoning`, `/mode`, `/cancel`,
  `/clear`, `/quit`, and `/exit` are handled locally and do not call a model.
  `Ctrl+C` and `/cancel` route through the owned turn worker and the recovery
  contract in ADR 0016. Configured-profile selection follows
  [ADR 0017](0017-safe-interactive-profile-selection.md); application-owned
  effort selection follows ADR 0027. `Shift+Tab` and `/mode` select the
  application-owned permission behavior defined by ADR 0028.
- Interactive composition uses the asynchronous, fail-closed approval boundary
  defined by [ADR 0015](0015-async-interactive-tool-approval.md). Explicit deny
  rules still take precedence. `--always-approve` remains an explicit,
  high-risk override and is not enabled by the TUI.

## Consequences

Headless and TUI runs now share context, resume, storage, provider routing, and
permission behavior. The UI can be exercised with Textual's headless test pilot,
and the application controller can be tested without importing Textual.

This is partial M3 support. Remote model catalogs, provider-native effort
mapping and workflow orchestration, Mermaid/media rendering, and the public
cross-platform interactive ACP PTY integration remain separate vertical slices.
The TUI now uses an explicit pristine-rewind policy before model output and
restores the cancelled prompt to its draft; it retains the prompt once output or
tool activity has begun. Three-platform production
terminal smoke coverage does not implement that user-facing PTY capability or
complete the broader remaining M3 work. Bounded interactive tool-card details were subsequently added by
[ADR 0030](0030-bounded-interactive-tool-card-details.md).
Recoverable in-flight cancellation is defined by
[ADR 0016](0016-recoverable-turn-cancellation.md).

## Validation

Neuro Code verifies terminal startup and exit restoration at the executable
boundary rather than inferring those properties from headless widgets.
