# ADR 0018 — Workspace-scoped interactive session resume

[简体中文](../../zh-CN/adr/0018-workspace-scoped-interactive-session-resume.md) · **English**

## Status

Accepted.

## Context

Neuro Code can list and resume SQLite sessions from the CLI, but the initial
TUI can resume only when a caller already knows an ID and supplies `--resume`
at startup. It also opens a resumed conversation without replaying its visible
history. A useful in-app resume flow must not cross workspace boundaries,
silently transplant provider-native state, or expose persisted reasoning and
raw tool output in the transcript.

## Decision

- `ProfileConversationController` also owns the interactive session catalog and
  resume boundary. `run`, profile selection, and session selection share one
  turn lock, so an active model/tool turn cannot be rebound.
- The composition root reads at most 50 recent SQLite summaries and retains only
  records whose stored workspace has the same filesystem identity as the active
  workspace. `AgentConversation.open` repeats that check when opening the
  selected ID, making the picker filter a usability boundary rather than the
  only authorization check.
- `Ctrl+R`, `/sessions`, and bare `/resume` open the picker. `/resume SESSION_ID`
  selects directly. Rows contain only a shortened ID, update time, stored
  provider/model, readiness, and current/fallback-profile markers. They do not
  contain prompts, endpoints, credentials, or a different workspace path.
- A ready configured profile whose name matches the stored source provider is
  preferred. If that profile is absent or not ready, the currently selected
  ready profile may resume the ordinary message projection. The new
  `AgentConversation` still carries the stored source provider, model, and
  context-affinity values, so every provider adapter continues to reject
  incompatible opaque/native context. If no ready profile exists, resume fails
  closed.
- The resumed binding must open the requested session ID successfully before it
  replaces the active binding. Reselecting the current session is a no-op; the
  previously active session is never deleted or rewritten.
- History replay is a presentation projection of canonical `Message` items.
  System messages and `PreservedContextItem` reasoning/backend-tool records are
  skipped. `Message.reasoning_content`, raw tool-result content, tool arguments,
  image URLs, and provider-native payloads are never rendered. User/assistant
  model-content projections are bounded to 20,000 characters per message;
  local tool calls/results become name-only restored lifecycle entries.
- Startup `--resume` and in-app selection use the same projection. Replacing the
  visible transcript does not change SQLite history or provider context.

## Consequences

Users can discover and reopen recent conversations without leaving the TUI or
copying UUIDs, including safely projected imported sessions. The feature is
deliberately local and workspace-scoped. Session titles/content search, deletion,
cross-workspace switching, remote catalogs, and rich replay of tool cards remain
future slices.

## Historical source evidence

The following read-only paths at pinned commit
`c68e39f60462f28d9be5e683d9cbe2c57b1a5027` establish session-picker, load, and
replay behavior. Neuro Code keeps its own SQLite/application contracts and does
not copy the upstream widget or remote-session architecture:

- `crates/codegen/xai-grok-pager/src/views/session_picker.rs`;
- `crates/codegen/xai-grok-pager/src/app/dispatch/session/load.rs`;
- `crates/codegen/xai-grok-pager/src/app/dispatch/tests/session/load.rs`;
- `crates/codegen/xai-grok-pager/src/sessions_cmd.rs`.
