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
- The presentation uses one application-owned neutral-dark theme. Textual's
  built-in command palette is disabled because its `Ctrl+P` and emoji search
  surface conflict with the application's provider picker and plain-text
  `/sessions QUERY` workflow.
- Full-screen terminal mode periodically compares the real TTY cell dimensions
  with the active Textual screen. It posts a normal resize event only when they
  differ, recovering from missing signal or in-band resize notifications. This
  fallback is not installed for headless, inline, or web drivers.
- Raw reasoning deltas, general tool argument mappings, and tool results are not
  rendered in the transcript. Approval modals receive only the bounded action
  summary defined by ADR 0015.
- `/help`, `/status`, `/provider`, `/model`, `/cancel`, `/clear`, `/quit`, and
  `/exit` are handled locally and do not call a model. `Ctrl+C` and `/cancel`
  route through the owned turn worker and the recovery contract in ADR 0016.
  Configured-profile selection follows
  [ADR 0017](0017-safe-interactive-profile-selection.md).
- Interactive composition uses the asynchronous, fail-closed approval boundary
  defined by [ADR 0015](0015-async-interactive-tool-approval.md). Explicit deny
  rules still take precedence. `--always-approve` remains an explicit,
  high-risk override and is not enabled by the TUI.

## Consequences

Headless and TUI runs now share context, resume, storage, provider routing, and
permission behavior. The UI can be exercised with Textual's headless test pilot,
and the application controller can be tested without importing Textual.

This is partial M3 support. Remote model-catalog and reasoning-effort selection,
pristine pre-token rewind, interjection queues, richer tool cards and
Markdown/media rendering, terminal-emulator smoke coverage, and cross-platform
PTY integration remain separate vertical slices. Recoverable in-flight
cancellation is defined by
[ADR 0016](0016-recoverable-turn-cancellation.md).

## Historical source evidence

The following read-only paths at pinned commit
`c68e39f60462f28d9be5e683d9cbe2c57b1a5027` establish the behavioral boundary;
their crate layout is not copied:

- `crates/codegen/xai-grok-pager-minimal/src/lib.rs`;
- `crates/codegen/xai-grok-pager/src/views/prompt_widget/mod.rs`;
- `crates/codegen/xai-grok-pager/src/app/event_loop.rs`;
- `crates/codegen/xai-grok-pager/src/slash/command.rs`;
- `crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs`.
