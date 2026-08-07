# ADR 0064: bounded redacted tool-output artifacts

[简体中文](../../zh-CN/adr/0064-bounded-tool-output-artifacts.md) · **English**

- Status: accepted
- Date: 2026-08-06

## Context

Tool results are intentionally bounded before they cross the model and UI
boundary. That protects context size and avoids replaying unbounded command
output, but it makes a long local command difficult to inspect after the
preview has been truncated. The existing process, workspace, permission, and
redaction boundaries must remain authoritative.

## Decision

The application injects an optional `ToolOutputArtifactStore` into
`ToolContext`. The production composition uses a file-backed adapter below the
configured application state directory. The adapter creates private
directories/files, writes through a temporary file followed by `os.replace`,
redacts configured and shape-detected credentials before byte truncation, and
retains at most 8 MiB per artifact.

Only local `BashTool` output that is actually truncated creates an artifact.
The same bounded store is passed to conversation-scoped managed Bash tasks;
their terminal snapshots expose only an opaque ID, a relative path below the
state artifact directory, byte count, and an artifact-truncated flag. No
command arguments, environment, absolute path, or raw output is added to the
model-visible message. Artifact persistence is diagnostic and fail-open: a
write failure does not change the command result, exit code, or cancellation
semantics.

The existing preview, `ToolResult` content, event ordering, permission checks,
sandbox launch, and background-task lifecycle remain unchanged. Filesystem
readers, ACP client-owned terminals, and automatic wake summaries continue to
use their existing bounded paths; extending artifacts to those sources is a
separate reviewed slice.

## Non-goals

This decision does not add SQLite rows, session replay/import/export, a new
tool for reading artifacts, cross-process task restoration, raw command
transcripts, or unbounded capture. It does not weaken workspace/sandbox
containment or expose secrets through tool metadata.

## Validation

Focused artifact-store, foreground Bash, managed background Bash, background
manager, tool-pipeline, architecture, and import-contract tests cover redaction
ordering, byte bounds, private file modes, atomic creation, optional-store
compatibility, terminal artifact metadata, and unchanged task behavior.
