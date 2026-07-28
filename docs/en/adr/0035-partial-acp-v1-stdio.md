# ADR 0035: Partial ACP v1 stdio adapter

[简体中文](../../zh-CN/adr/0035-partial-acp-v1-stdio.md) · **English**

- Status: accepted
- Date: 2026-07-19

## Context

Neuro Code needs a standard editor/client protocol surface without duplicating
ACP schema generation, JSON-RPC dispatch, or stdio framing. The existing CLI
and TUI composition lived in interface-private functions, while internal
conversation IDs may be created only when the first prompt is persisted.
Publishing complete ACP v1 compatibility now would also be incorrect: MCP
servers, additional directories, session discovery/resume, client filesystem
and terminal methods, multimedia content, and WebSocket transport are not
implemented.

The pinned official Python SDK range is
`agent-client-protocol>=0.11.0,<0.12`; the lock currently selects 0.11.0. That
SDK exposes standard `session/close` schema types but marks its router entry
unstable, ignores malformed JSON lines, and does not reject an incorrect
incoming `jsonrpc` version before normalizing its response to 2.0.

## Decision

- Add `neuro-code acp` as an explicitly partial ACP v1 stdio surface using the
  official SDK for production schema, dispatch, notifications, requests, and
  framing.
- Enable the SDK unstable-router flag only to make `session/close` reachable;
  do not implement or advertise another unstable method or custom extension.
- Advertise `sessionCapabilities.close = {}`. Implement `initialize`,
  `session/new`, `session/prompt`, `session/cancel`, and `session/close`; send
  standard `session/update` notifications and use
  `session/request_permission`. ADR 0036 subsequently adds standard
  `session/load` and the truthful `loadSession: true` capability; ADR 0037 adds
  standard `session/list` and `sessionCapabilities.list = {}`.
- Bind one connection to its normalized launch workspace. Reject relative or
  different `cwd` values and non-empty `additionalDirectories`. This ADR's
  original rejection of non-empty `mcpServers` is superseded for bounded stdio
  servers by ADR 0038; HTTP, SSE, and ACP MCP transports remain rejected.
- Generate one stable ACP ID per protocol session and keep a separate mapping
  to the lazily created internal SQLite ID. ADR 0036 makes that mapping durable.
- Accept only bounded Text and ResourceLink prompt blocks. Preserve ordering,
  project only standard allowlisted ResourceLink fields, ignore `_meta`, and
  never dereference a URI during conversion.
- Project runtime events through an explicit bounded/redacted allowlist. Keep
  reasoning private by default, use a stable `messageId` for one answer, and
  report terminal turn state only through `PromptResponse.stopReason`.
- Adapt the existing fail-closed permission path per ACP session. Client
  approval cannot override local deny, workspace, environment-protection, or
  sandbox decisions.
- Permit one prompt per session while allowing different sessions to run
  concurrently. Cancel, close, EOF, and disconnect share idempotent cleanup
  that terminates owned work/background scopes but does not delete history.
- Extract `ApplicationComposition` so CLI, TUI, and ACP share configuration,
  providers, storage, tools, permissions, workspace/sandbox binding, background
  scopes, and shutdown without importing interface types into the application
  module.

## Consequences

- Standard SDK clients can drive the implemented core slice and receive only
  capabilities that are actually present.
- ACP IDs stay stable even though persistence remains lazy and internal IDs
  retain their existing format.
- Resource links are model-visible references, not implicit I/O authority.
- Approval and cancellation are isolated per session, and close is not
  session deletion.
- The process remains a partial ACP v1 implementation. Complete conformance,
  WebSocket, non-stdio MCP transports and non-tool MCP features, additional
  directories, resume/delete/fork,
  multimedia/embedded prompt content and history, and client
  filesystem/terminal calls remain future slices.
- ADR 0050 later implements the resume/delete/fork lifecycle slice without
  changing this ADR's original partial-core decision.
- Raw stdio tests record the official 0.11 SDK's malformed-frame and JSON-RPC
  version behavior. Replacing it with a private production parser or dispatcher
  is not an accepted workaround; upstream SDK changes can be adopted within a
  separately reviewed dependency update.
