# ADR 0066: session-scoped tool-output artifact reads

[简体中文](../../zh-CN/adr/0066-session-scoped-tool-output-artifacts.md) · **English**

## Status

Accepted — Stage5BM, 2026-08-06.

## Context

Stage5BK stores a bounded, redacted artifact when a local Bash preview is
truncated. Stage5BL added a typed reader port and an infrastructure-safe
application seam, but that seam accepted only an opaque handle and had no way
to prove that a handle belonged to the session requesting it. Exposing that
reader directly to an inbound interface would therefore make session
visibility implicit and unsafe.

## Decision

Add `SessionToolOutputArtifactApplicationService` beside the existing
handle-only reader service. It:

1. verifies the session through the `SessionStore` summary read;
2. derives a bounded, immutable artifact projection only from that session's
   persisted event data;
3. extracts only the artifact ID, safe relative handle, byte count, truncation
   fact, and event sequence;
4. allows a read only when the requested opaque ID is present in that
   session's projection; and
5. delegates content reads through `ToolOutputArtifactReader`, preserving its
   root confinement, re-redaction, and byte limits.

The projection is deduplicated by artifact ID and returns the most recent
bounded page in event order. Malformed or untrusted event metadata is ignored
as unavailable artifact metadata rather than being treated as a filesystem
path. A missing association returns a generic `SessionError` and does not
reveal whether the handle exists in another session or on disk.

## Boundaries

This stage does not add a SQLite table or migration: the persisted
`TOOL_COMPLETED`/`TOOL_FAILED` event projection is the existing association
boundary. It does not change Runtime, Bash, permissions, sandboxing, session
items, or event kinds. It does not expose the service through CLI, TUI, or
ACP yet; a later inbound slice must inject this service and define its output
presentation separately.

The service does not provide artifact listing across sessions, deletion,
pagination beyond a small bounded page, absolute paths, raw arguments, raw
tool output, or authorization based only on a caller-supplied session ID.

## Consequences

- A future interface can retrieve an artifact only through a typed,
  session-scoped application query.
- Existing event replay remains compatible and no schema migration is needed.
- Corrupt or legacy events without valid artifact metadata remain readable;
  their artifact is simply unavailable to the projection.
- A later UI/CLI/ACP stage still needs an explicit user-facing contract and
  lifecycle policy for missing, expired, or deleted artifact files.

## Verification

Tests cover bounded event projection, malformed metadata, deduplication,
session association checks, re-redaction, and rejection of an unassociated
opaque handle. The application module remains inside the existing
`application` layer and no infrastructure path leaks through the API.
