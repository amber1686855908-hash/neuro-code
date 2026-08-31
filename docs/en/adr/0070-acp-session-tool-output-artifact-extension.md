# ADR 0070: ACP session-scoped tool-output artifact extension

## Status

Accepted for Stage5BQ.

## Context

Stage5BM introduced a session-scoped application service for bounded,
redacted tool-output artifacts. Stage5BN and Stage5BO exposed that service to
the TUI and CLI. At Stage5BQ acceptance, ACP clients still had no way to
inspect the same diagnostic output.

The ACP 0.11 protocol has no standard artifact resource or artifact listing
operation. The SDK does, however, route methods beginning with `_` to an
agent extension handler. Any extension must remain private, bounded, and
session-scoped rather than becoming an undocumented second session protocol.

## Decision

Add the namespaced private extension method
`_neuro-code/session/artifacts`.

The request payload is one of the following bounded forms:

- `{ "sessionId": "...", "limit": N }` lists at most 100 artifact handles;
- `{ "sessionId": "...", "artifactId": "...", "maxBytes": N }` reads one
  associated artifact, capped at 256 KiB.

The ACP adapter resolves the external ACP session ID through the existing
`acp-v1` alias namespace and delegates to
`AcpApplicationService`, which delegates to
`SessionToolOutputArtifactApplicationService`. ACP never reads the state
directory or receives an internal session ID.

Responses contain only opaque artifact IDs, byte counts, event sequence,
truncation facts, and bounded redacted content. Paths, raw event metadata,
tool arguments, secrets, and storage exceptions are never serialized.
Malformed or cross-session handles fail closed with stable protocol errors.
The extension is not advertised as a standard ACP capability. Later bounded ACP
slices define additional namespaced methods for MCP, subagent, lifecycle, and
compaction projections; methods outside those accepted boundaries remain
unsupported.

## Boundaries

- No ACP schema or standard capability is changed.
- No SQLite schema, event kind, Runtime, Provider, Finalizer, permission, or
  TUI behavior changes.
- The existing application service remains the authorization boundary for
  workspace ownership and persisted event association.
- ACP list/read operations are read-only and do not create or mutate session
  state.

## Rejected alternatives

- Adding artifact fields to `SessionInfo`: would make the standard session
  catalog perform unexpected event and filesystem reads.
- Returning filesystem paths or raw metadata: would bypass the opaque-handle
  boundary and expose infrastructure details.
- Advertising a new standard capability: ACP 0.11 has no compatible standard
  artifact capability, so the private namespaced extension is explicit and
  opt-in.
