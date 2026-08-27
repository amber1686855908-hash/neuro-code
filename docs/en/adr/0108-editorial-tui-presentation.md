# ADR 0108: Editorial TUI presentation and grouped tool activity

[简体中文](../../zh-CN/adr/0108-editorial-tui-presentation.md) · **English**

## Status

Accepted.

## Context

The TUI already preserved safe, bounded tool details, but it projected every
tool call as a visually independent log card. Permission decisions, output
counts, workspace scans, completion labels, and long commands competed with the
assistant response. The persistent shortcut row and verbose runtime labels also
made the bottom chrome heavier than the conversation.

The runtime event stream, permission policy, output-artifact boundary, and
session transcript remain authoritative. This change is presentation-only.

## Decision

- `NeuroCodeApp` owns a TUI-only activity-group projection over consecutive tool
  lifecycle events. Calls retain their call IDs and in-place state, while one
  visible group summarizes read, search, command, edit, and other activity.
  Visible assistant, user, plan, status, or error entries end the active group.
- A group is collapsed by default, including workspace edits. Its stable summary
  keeps success/failure/running state, bounded intent or aggregate counts, key
  failure text, and elapsed time. Enter or click opens a fixed-height Inline Peek
  for exactly one selected call. Up/Down changes the selected call, Enter opens
  its independent Tool Inspector, and Escape returns to the Summary. Clicking an
  open Peek also collapses it, and Escape still works if streaming updates move
  focus back to the composer.
- Inline Peek has both a ten-logical-line presenter budget and a twelve-row widget
  maximum, so terminal wrapping cannot grow Conversation without bound. It uses
  metadata-first renderers for tree, search, file-read, Bash, and generic tools;
  formatted stdout is only a bounded literal-text fallback when metadata is
  insufficient. Normal allow decisions and a duplicate `Completed` label are not
  shown in Summary or Peek.
- The Tool Inspector owns scrollable Output, Input, and Meta views and copy
  actions for each view. Output includes available workspace diffs and loads a
  session-scoped artifact only after the Inspector opens. The existing 256 KiB
  read limit, credential redaction, opaque handle, and session-ownership check
  remain authoritative; read/storage truncation is stated explicitly. Input is
  recursively redacted and Meta is allowlisted, so artifact paths, artifact IDs,
  arbitrary metadata, and exception text are not displayed.
- An open Inspector remains bound to its selected live call. Lifecycle events
  update its presentation without querying the modal for Conversation widgets;
  the persistent base screen receives transcript updates while the modal is
  active. Running elapsed-time refresh is deduplicated to at most once per
  activity group and skips open Peek/Inspector layouts that do not display that
  changing Summary timer.
- The interface uses one compact semantic token set for three backgrounds, one
  border, primary/secondary/muted foregrounds, one restrained interaction
  accent, semantic success/warning/error colors, and shared spacing values.
  Paths, models, and tool names no longer receive accent color solely by type.
- Conversation, plan, activity, status, and error blocks share one left reading
  axis and a 116-column maximum. The persistent bottom area contains only the
  composer and a label-free compact status row. The full shortcut row is
  removed; `/help` and F1 retain on-demand discovery.
- Modal dialogs use small, medium, and large size classes with common padding
  and borders. Selection lists use a focus chevron and a separate selected
  checkmark without a full-row selected fill. Ultracode remains visually distinct
  and reports bounded delegation progress. The transcript-copy editor uses dividers rather than a second
  complete box.

## Consequences

- Long tool sequences remain one secondary activity block instead of becoming
  a CI-style log. Conversation never renders full stdout or every call's details
  at once, while individual safe details remain inspectable in a modal.
- Disclosure is presentation-only. Tool execution, permissions, persistence,
  cancellation, Provider behavior, and artifact authorization do not change.
- Transcript Copy always uses the stable Activity Summary, independent of the
  current Summary/Peek state; Inspector copying is deliberately separate.
- Narrow terminals preserve effort, mode, context, and workspace visibility by
  splitting the status row into two bounded table projections; long model and
  path values ellipsize.
- F1 now renders the existing local command reference. All previous keyboard
  commands remain available even though their permanent footer labels are gone.

## Validation

Headless Textual tests cover grouping boundaries, fixed-height single-selection
Peek behavior in narrow terminals and large groups, artifact-free Peek,
Inspector-only session-scoped reads, redaction and truncation notices, stable
Transcript Copy, renderer fallback, modal copying, wide reading width, narrow
status containment, modal sizes, sparse settings rows, distinct Ultracode delegation status, live
Inspector completion, focus-independent collapse, and per-group timer refresh.
