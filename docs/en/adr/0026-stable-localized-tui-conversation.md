# ADR 0026 — Stable localized TUI conversation

[简体中文](../../zh-CN/adr/0026-stable-localized-tui-conversation.md) · **English**

## Status

Accepted.

## Context

The first TUI rendered committed history in a pre-rendered log and streamed the
active assistant response in a separate widget beside the prompt. Completing a
turn removed the temporary widget and appended a new log entry. The same answer
therefore changed both container and screen position, which produced visible
jumps after streaming and could place transient text next to the input during a
resize.

Prefixing every line with `You:` or `Assistant:` also made long conversations
look like diagnostic logs. In addition, all application-owned copy was fixed in
English even though model and user content already supported Unicode.

## Decision

- The transcript is a scrollable vertical conversation made from one stable
  widget per visible message. User prompts use a full-width muted prompt block;
  assistant responses use an independent body block without role-name prefixes.
- Submitting a prompt mounts one pending assistant widget at the end of the
  conversation. Provider, reasoning, and tool notices are inserted before that
  pending widget. Text deltas and the terminal response update the same widget;
  completion never copies the response to another container.
- Streaming follows the end only when the viewport was already at the end. A
  user who scrolls upward is not pulled back by later deltas.
- `UiLanguage` supports English and Simplified Chinese for application-owned
  chrome, dialogs, labels, and local status messages. `/settings`, `/setting`,
  and `Ctrl+,` open the language picker. Conversation prompts, model responses,
  tool payloads, external error details, identifiers, and paths are not
  translated.
- `UiPreferencesStore` is an injected persistence port. Its JSON adapter stores
  the selected language in `ui-preferences.json` under the configured state
  directory. Writes are atomic and the resulting file mode is restricted to
  the user. Provider configuration and credentials are not read or rewritten by
  this adapter. ADR 0027 subsequently extends the same isolated preference file
  with the requested application reasoning effort.
- Both language catalogs must contain the same keys. Local entries retain their
  translation key and interpolation values so switching can rerender
  application-owned history without changing user or model entries.

## Consequences

Streaming has one visual identity from waiting state through completion, and
the input remains a separate fixed control. Message roles are distinguishable
by layout without adding noisy repeated labels. The language selection survives
future launches while remaining isolated from provider routing and secrets.

This decision did not originally add Markdown, Mermaid, media, or rich tool-card
rendering. [ADR 0027](0027-semantic-tui-and-application-reasoning-effort.md)
subsequently adds safe semantic Markdown for assistant text, and
[ADR 0029](0029-auditable-in-place-tool-cards.md) adds bounded in-place tool
cards. Mermaid, media, and interactive card expansion remain separate slices.

## Historical source evidence

The following read-only paths at pinned commit
`c68e39f60462f28d9be5e683d9cbe2c57b1a5027` establish the upstream interactive
boundary without prescribing this Textual widget design:

- `crates/codegen/xai-grok-pager/src/views/prompt_widget/mod.rs`;
- `crates/codegen/xai-grok-pager/src/app/event_loop.rs`;
- `crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs`.
