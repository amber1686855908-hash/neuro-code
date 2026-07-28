# ADR 0037: Workspace-scoped ACP session list

[简体中文](../../zh-CN/adr/0037-workspace-scoped-acp-session-list.md) · **English**

- Status: accepted
- Date: 2026-07-19

## Context

Durable `session/load` is useful only when a client already knows an ACP
session ID. Standard `session/list` lets clients discover persisted sessions,
but this process is deliberately bound to one launch workspace. An unfiltered
database listing would disclose metadata from unrelated workspaces. Returning
internal SQLite IDs would also undo ADR 0036's protocol/storage identity
separation.

The standard request has no client-selected page size. It accepts an optional
absolute `cwd` and opaque cursor, while `SessionInfo` exposes only session ID,
working directory, optional title and last-update timestamp, plus optional
additional roots and `_meta`.

## Decision

- Advertise `sessionCapabilities.list = {}` and implement the stable
  `session/list` SDK route. Do not advertise delete, resume, fork, or additional
  directories.
- Treat an omitted `cwd` as the connection-bound workspace. A supplied `cwd`
  must be absolute and identify that same workspace. Never list another
  workspace through this connection.
- Return only the existing durable ACP alias, recorded absolute `cwd`,
  bounded/redacted persisted title, and ISO 8601 `updatedAt`. Omit `_meta`,
  `additionalDirectories`, provider/model data, conversation content, tool
  data, and private context.
- Allocate a random `acp-<UUID>` alias atomically when a listable persisted
  session has no ACP alias. Concurrent processes converge on one alias through
  the schema-v5 uniqueness constraints. The alias is then accepted by
  `session/load`.
- Page sessions by descending normalized update timestamp and internal ID.
  Internal IDs are used only inside the store/cursor state and are never
  encoded into a client-visible token.
- Use 50 result rows per page. Scan at most 5,000 database rows per request in
  batches of 250 so filtering many unrelated workspace rows remains bounded.
  A bounded scan may return an empty page with a continuation cursor.
- Generate random connection-local cursor tokens and retain at most 256 cursor
  states. Validate cursor length/control characters and reject unknown or
  expired tokens with stable invalid-parameter errors. Clear all cursor state
  on disconnect.
- Keep list read-only with respect to conversation history and runtime
  lifecycle. Listing may create only the durable alias; it does not load a
  conversation, open a background scope, or expose session content.

## Consequences

- Standard clients can discover a session, display safe metadata, and pass its
  stable ID to `session/load`.
- No-filter requests remain workspace-isolated, and cursor tokens reveal
  neither internal IDs nor cross-workspace metadata.
- Keyset pagination avoids an unbounded offset and remains deterministic for
  unchanged rows. Concurrent session updates may move rows between pagination
  windows, which is normal best-effort cursor behavior without a database
  snapshot.
- The ACP implementation remains partial. Session resume/delete/fork,
  non-stdio MCP transports and non-tool MCP features, additional directories,
  multimedia prompt/history content, client filesystem/terminal calls,
  WebSocket transport, and custom extensions remain unsupported.
- ADR 0050 later implements resume/delete/fork while preserving this ADR's
  workspace scoping, durable aliases, and bounded list behavior.
