# ADR 0065: bounded tool-output read seam

[简体中文](../../zh-CN/adr/0065-bounded-tool-output-read-seam.md) · **English**

- Status: accepted
- Date: 2026-08-06

## Context

Stage5BK writes local Bash artifacts outside the conversation and SQLite. A
future interface may need to inspect one artifact, but letting TUI, CLI, or ACP
join a state-directory path would bypass application boundaries and make
authorization unclear.

## Decision

Add a typed `ToolOutputArtifactReader` port and a small
`ToolOutputArtifactApplicationService`. The request carries the opaque
`ToolOutputArtifact` handle produced by the running tool and an explicit byte
limit. The adapter derives the filename from the validated artifact ID; callers
cannot supply an arbitrary path.

Reads are bounded to 256 KiB by default and never exceed the existing 8 MiB
artifact ceiling. The file adapter confines resolved paths below its configured
artifact root, re-applies redaction before decoding and truncation, and returns
a frozen text projection. Missing or forged handles fail closed.

The service is not yet exposed through CLI, TUI, ACP, or a new tool. Stage5BK
did not persist a session-to-artifact association, so this slice deliberately
does not claim session-level authorization or cross-process recovery. A
user-facing read path must define that association and visibility policy first.

## Non-goals

This decision does not add SQLite rows, session items, event kinds, filesystem
path arguments, artifact listing, pagination, or raw-output replay. It does not
change Bash execution, permissions, sandboxing, background tasks, or the model
context.

## Validation

Application and file-adapter tests cover bounded redacted reads, forged-path
rejection, missing artifacts, explicit byte limits, and unchanged write
behavior.
