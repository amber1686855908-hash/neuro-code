# ADR 0050: ACP durable session lifecycle

[简体中文](../../zh-CN/adr/0050-acp-session-lifecycle.md) · **English**

- Status: accepted
- Date: 2026-07-28

## Context

The partial ACP adapter can create, list, load, prompt, cancel, and close
sessions, but a client cannot yet delete durable history, fork an independent
conversation, or resume without replaying visible history. These operations
must preserve the existing separation between client-visible ACP IDs and
internal SQLite IDs, remain scoped to the launch workspace, and keep provider,
sandbox, permission, MCP, and background-task ownership fail-closed.

The pinned `agent-client-protocol==0.11.0` SDK generates
`DeleteSessionRequest`, `DeleteSessionResponse`, and delete capability models,
but its agent router does not register the stable `session/delete` method.
The same router registers `session/fork`, `session/resume`, and
`session/close` behind its unstable-protocol gate.

## Decision

- Advertise `sessionCapabilities.delete`, `fork`, `resume`, and `close` in
  addition to list. Keep `loadSession: true`.
- Extend the canonical `SessionStore` port with durable delete and fork
  operations. SQLite performs each operation under the existing write lock and
  one transaction.
- Delete accepts only a bounded valid ACP ID resolving to a session in the
  connection workspace. It first applies close/cancel cleanup to an active
  binding, then deletes the durable row. Foreign-key cascades remove events,
  aliases, and the search document; the search trigger removes the FTS row.
  Deleting a new active session that has not persisted an internal ID only
  closes its owned resources.
- Fork requires a persisted source session and rejects a source with an active
  prompt. SQLite creates a fresh internal ID and timestamps while copying the
  ordered context, provider/model affinity, sandbox profile, and title. It does
  not copy events or aliases. ACP allocates a fresh external ID, recreates a
  normal binding and optional session-owned MCP tools, and publishes the fork
  only after all resources and its alias are ready. Any later failure deletes
  the copied row and closes the new resources.
- Resume uses the same workspace, alias, fixed-sandbox, provider-affinity, MCP,
  reservation, and publication checks as load, but sends no history updates.
  Load remains the operation that replays bounded visible history.
- Continue using the SDK's stdio streams, `Connection`, dispatcher, generated
  schemas, and `MessageRouter`. Build the official router with the unstable
  gate enabled for fork/resume/close, then register only the generated stable
  delete request on that router. The project does not replace or reinterpret
  JSON-RPC framing or dispatch behavior. A routing test locks this compatibility
  seam until the SDK registers delete itself.

## Consequences

- Standard clients can now discover, resume, fork, and delete workspace-local
  durable sessions without seeing internal database IDs.
- Load and resume have distinct observable semantics: load replays safe visible
  history; resume restores context silently.
- Forked conversations share an immutable prefix at creation time but have
  independent IDs, events, aliases, runtime resources, and future history.
- Delete is destructive by protocol intent. Workspace filtering and stable
  not-found errors prevent it from becoming a cross-workspace metadata oracle.
- The ACP adapter remains partial: additional directories, non-stdio MCP
  transports and features, multimedia prompt/history content, client
  filesystem/terminal methods, WebSocket, and extensions remain open.
