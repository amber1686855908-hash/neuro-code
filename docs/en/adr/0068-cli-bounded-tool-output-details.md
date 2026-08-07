# ADR 0068: CLI session-scoped bounded tool-output details

## Status

Accepted for Stage5BO.

## Context

Stage5BK stores only redacted, bounded tool-output artifacts when a local tool
preview is truncated. Stage5BM defines a session-scoped application service
that proves an opaque artifact handle belongs to persisted terminal tool
events. Stage5BN uses that service for TUI expansion, but headless users still
have no way to inspect the bounded output.

## Decision

Add `sessions artifacts SESSION_ID [ARTIFACT_ID]` to the CLI.

- Without an artifact ID, the command lists a bounded page of session-associated
  artifact metadata.
- With an artifact ID, it reads only that associated artifact through
  `SessionToolOutputArtifactApplicationService` and a caller-provided bounded
  byte limit.
- JSON output contains only the opaque ID, byte count, truncation facts, event
  sequence, and redacted bounded content when requested.
- Relative filesystem paths, state directories, raw metadata, tool arguments,
  and storage exceptions are not rendered.
- The bootstrap owns `FileToolOutputArtifactStore`; the CLI receives only the
  typed application service.

## Boundaries

This is a read-only interface slice. It does not add schema, events, runtime
writes, artifact deletion, retention, or ACP protocol fields. Missing or
unassociated handles continue to use the application's generic session error
boundary.

## Rejected alternatives

- Reading `state_dir/tool-output` directly from `cli.py`: would bypass session
  association and expose infrastructure details.
- Adding artifact content to `sessions list`: would perform unexpected I/O and
  make the existing session catalog unbounded.
- Adding a new persistence table: the existing terminal event metadata already
  supplies the session association for this bounded read use case.
