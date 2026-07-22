# ADR 0030 — Bounded interactive tool-card details

[简体中文](../../zh-CN/adr/0030-bounded-interactive-tool-card-details.md) · **English**

## Status

Accepted.

## Context

ADR 0029 made each tool invocation auditable by rendering its bounded, redacted
output and workspace diff in one stable card. That detail is useful, but a long
sequence of reads, commands, or edits can occupy most of the terminal viewport.
Users need to collapse finished detail without losing the invocation, permission
path, change summary, terminal status, or elapsed time.

An expansion control must not become a route to unbounded output, raw provider
payloads, credentials, or a second filesystem read. It also must not turn one
invocation back into several transcript entries.

## Decision

- Runtime tool entries use a focusable `ToolFeedbackMessage` whenever bounded
  details are available. Mouse click, `Enter`, or `Space` toggles the card in
  place. Application-owned hints and tooltips are localized.
- New cards start compact. Successful edits with a workspace-change report and
  failures expand automatically; completed read/list/search/image/skill calls
  collapse to one localized action sentence plus elapsed time. Other compact
  cards retain their invocation, summaries, terminal status, and duration.
  Opening any card reveals only its already-bounded output or unified diff.
- Expansion never fetches more data. Both states render from the same
  `ToolFeedbackState` after ANSI/control cleanup, credential redaction, workspace
  report filtering, and the existing 40-line/6,000-character TUI bound. Payload
  text remains literal Rich `Text` and cannot inject styles.
- Expansion is presentation-only, in-memory state. It is not persisted in the
  SQLite session, sent to the model, or restored as a rich historical tool card.
  Lifecycle updates, language changes, and toggles continue updating one widget
  for the original call ID, while existing scroll-follow protection applies.

## Consequences

Users can reclaim terminal space and reopen the exact safe preview without
changing execution, authorization, or audit metadata. Cards without output or
diff detail do not enter keyboard focus traversal. Because the application does
not retain an unbounded transcript, expansion cannot reveal content beyond the
original safety limits; durable full command transcripts remain a separate
future capability.

## Verification

Textual tests verify a one-line completed read, then complete an edit lifecycle,
verify its automatically expanded redacted diff, collapse it with the keyboard,
expand it by mouse click, and confirm that the credential remains absent after
both renders and after English-to-Chinese relocalization.
