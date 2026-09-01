# ADR 0067: Session-scoped bounded tool-output details in the TUI

## Status

Accepted for Stage5BN.

## Context

Stage5BK stores only deliberately truncated local tool output as a redacted,
bounded artifact. Stage5BL adds an opaque-handle reader and Stage5BM adds
`SessionToolOutputArtifactApplicationService`, which proves that a handle was
recorded by the current session before reading it. The TUI previously showed
the bounded event preview but had no safe way to reveal the remaining output.

## Decision

The bootstrap composition creates a
`SessionToolOutputArtifactApplicationService` from the existing `SessionStore`
and `FileToolOutputArtifactStore`, and injects that application boundary into
`NeuroCodeApp`. The TUI stores only the bounded `output_artifact_id` from a
terminal tool event. It never receives a filesystem path or artifact store.

When a user advances from a bounded Inline Peek into the independent Tool
Inspector, the TUI asynchronously requests the artifact for the runner's current
session. Summary and Peek never request or render artifact content. The
application service checks the persisted session event association, and the
reader enforces the opaque-handle path boundary, redaction, and 256 KiB read
limit. Inspector Output is scrollable and explicitly states read-limit or
artifact-storage truncation instead of implying that a truncated projection is
the original complete output. Read failures show a generic localized notice.
Artifact IDs, paths, arbitrary metadata, and exception text are never rendered;
the separately selectable Input view is recursively redacted.

The read is opt-in at expansion time, bounded, and does not alter events,
session items, Runtime behavior, Provider behavior, permissions, or SQLite
schema. Provider switching and session switching reuse the same composition
service; session authorization is evaluated by the service for every read.

## Consequences

- Long Bash output can be inspected without placing the full output in the
  model-visible conversation or session event payload.
- A missing, deleted, malformed, or cross-session artifact degrades to a safe
  UI notice instead of exposing storage details.
- The TUI owns a small asynchronous worker that starts only after Inspector is
  opened. ADR 0108 moves disclosure to Summary → selected-call Peek → Inspector;
  the session-authorized application seam and streaming event flow remain
  unchanged.
- At this ADR's acceptance, CLI and ACP artifact views remained separate. Later
  bounded slices implement both read-only views; other inbound artifact views
  remain separate capabilities.

## Rejected alternatives

- Passing the state-directory path or `FileToolOutputArtifactStore` into the
  TUI: this would leak infrastructure details across the interface boundary.
- Reading an artifact by caller-supplied path: this would bypass the persisted
  session association and path validation.
- Automatically reading every artifact when a tool completes: this adds I/O
  and memory pressure even when the user never expands the card.
- Adding a new AgentEventKind or SQLite table: the existing terminal tool
  metadata and session event projection are sufficient for this read-only
  slice.
