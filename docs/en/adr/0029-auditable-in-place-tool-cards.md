# ADR 0029 — Auditable in-place tool cards

[简体中文](../../zh-CN/adr/0029-auditable-in-place-tool-cards.md) · **English**

## Status

Accepted.

## Context

Rendering every tool lifecycle event as a separate labelled row makes one call
look like several calls. A terminal “completed” line without the actual bounded
result also prevents the user from auditing what a read, search, directory list,
or command returned. Edit tools can describe their target in arguments, but a
shell command may create or rewrite files without returning any stdout, so its
effect cannot be reconstructed from the terminal result alone.

Copying unbounded tool results into the transcript would create different
problems: credentials, control sequences, very large output, binary data, and
model- or command-owned markup must not take control of the UI. A file-change
observer must also remain an audit aid rather than a new authorization or
sandbox boundary.

## Decision

- The TUI keeps one `ToolFeedbackState` per active call ID. `TOOL_REQUESTED`
  mounts one stable card; permission, approval, start, result, failure, and
  duration events update that card in place. The fixed left gutter therefore
  renders `Tool` once per call, while lifecycle details form child lines inside
  the same body. Provider-hosted calls use the same identity rule but show only
  lifecycle data because their provider contract does not expose result text.
- The card title contains only the existing selected invocation fields. A
  terminal local event may add actual tool content after ANSI/control cleanup,
  heuristic and configured-value credential redaction, and display bounding.
  The TUI keeps at most 40 preview lines and 6,000 characters, retaining a
  bounded head/tail view and an explicit omission marker. Payload text is added
  as literal Rich `Text`, never parsed as markup.
- Around each permitted, side-effecting local tool execution, `AgentRuntime`
  takes bounded read-only snapshots of the current workspace and emits a
  JSON-safe change report with the terminal event. The observer scans at most
  4,000 files and 8 MB of eligible UTF-8 text, with a 256 KB per-file limit. It
  excludes VCS internals, dependency/build/cache directories, and symlinks.
  Sensitive filenames hide content entirely. Binary, oversized, and
  budget-exhausted files report only path/status.
- Snapshot comparison reports created, modified, and deleted relative paths.
  Eligible text uses a credential-redacted unified diff bounded to 20 changed
  files and 240 lines/24,000 characters per file before the tighter TUI display
  bound is applied. The card gives additions/deletions, uses green/red
  foregrounds with distinct tinted backgrounds for inserted/deleted lines, and
  labels every hidden or truncated case.
- Snapshotting starts only after the normal permission/approval decision and
  immediately before tool execution. It does not grant access, change the tool
  result sent to the model, or turn an observed change into proof of successful
  execution. Shell and edit side effects remain subject to their existing
  permission, workspace, and process-sandbox adapters.
- Application-owned labels rerender when the interface language changes. Tool
  names, paths, commands, output, and diff content are never translated.
- The default agent guidance prefers workspace edit tools over shell redirection
  so edits are intentional and easier to audit, while snapshot comparison still
  covers shell-created changes.

## Consequences

Read/list/search calls now default to a concise action sentence while retaining
their bounded result for interactive expansion. Edit or shell file writes show
where and how files changed. A call contributes one transcript entry rather
than repeating the tool label for every lifecycle transition.
Credentials and terminal control data remain outside the rendered preview, and
large or sensitive changes degrade to explicit metadata instead of disappearing
silently.

The snapshot is best-effort observation, not a filesystem transaction. A
concurrent external write can be attributed to the same interval; changes in
excluded or over-budget paths may be reported without content or missed after a
scan limit. Background commands can change files after their start call has
returned, so those later changes are not part of the launch card. Interactive
expansion is specified by
[ADR 0030](0030-bounded-interactive-tool-card-details.md); durable full tool
transcripts and checkpoint-grade rollback remain future vertical slices.

## Verification

Headless runtime tests cover exact-edit and Bash-created file reports. Workspace
tests cover create/modify/delete detection, ignored directories, sensitive-file
hiding, and credential redaction. Textual tests assert one stable card across
all lifecycle events, actual result rendering, colored diff text, secret
removal, and English-to-Chinese rerendering.
