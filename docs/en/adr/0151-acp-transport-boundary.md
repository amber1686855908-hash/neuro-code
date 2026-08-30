# ADR 0151: ACP Transport Boundary

- Status: Proposed
- Date: 2026-08-31
- Scope: the ACP transport structural slice stacked on PR #78
- Depends on: ADR 0145, ADR 0146, ADR 0147, ADR 0148, ADR 0149, and ADR 0150

## Context

The exact frozen PR #78 base is
`99af1d1e9339b3baaa657b1a946279a7ecffff61`. At that base,
`neuro_code.acp` had already extracted prompt/content conversion, history/live
update projection, client filesystem/terminal adaptation, MCP declaration
conversion, binding resource closure, and per-session runtime ownership. It
still mixed protocol-agent semantics with the ACP SDK connection, stdio
startup/framing, and WebSocket transport loop.

This slice extracts only that transport boundary. It preserves the public
`neuro_code.acp.serve_acp` and `serve_acp_websocket` entrypoints, the existing
private compatibility names, the SDK router workaround, all framing and size
bounds, and the existing close, cancellation, and Agent shutdown behavior.
It does not redesign the client ports, capability negotiation, session model,
background task semantics, or any other ACP boundary.

## Pre-change transport audit

The audit was completed against the exact PR #78 head before moving code.

### Transport-specific symbols

The transport-specific symbols in `neuro_code.acp` were:

- `ACP_STDIO_BUFFER_LIMIT_BYTES`;
- `_build_acp_router`, including the SDK 0.11 stable
  `session/delete` route workaround;
- `_AcpSdkConnection`;
- `_WebSocketWriter`;
- the `serve_acp` stdio stream/connection loop; and
- the `serve_acp_websocket` server, feeder, newline framing, and per-connection
  close loop.

The client filesystem and terminal adapters are not part of this audit: they
are already owned by `neuro_code.interfaces.acp.client_io` under ADR 0147.
Capability negotiation remains in `NeuroCodeAcpAgent`.

### State and lifecycle

The SDK connection adapter owns only the SDK `Connection` instance and the
injected Agent attachment. The WebSocket writer owns its pending byte buffer
and closed flag. Each WebSocket handler owns one bounded `StreamReader` and
one feeder task. The feeder converts text to UTF-8, accepts bytes, appends a
newline when needed, and closes the reader with EOF in its finalizer.

The transport does not own retained background terminal tasks, pending
terminal starts, terminal watchers, task completion state, output state,
timeouts, kill/release policy, session shutdown state, session locks,
capability snapshots, or permission state. Those remain with the existing
client-I/O, session-runtime, application, or Agent owners.

### Agent call sites and capability gates

`serve_acp_websocket` was the only WebSocket bootstrap call site. It accepted
the application service, constructed one `NeuroCodeAcpAgent` for each accepted
connection, and passed it to the connection loop. `serve_acp` constructed one
`NeuroCodeAcpAgent` for the stdio process and passed it to the stdio loop.

The transport itself had no capability checks. The Agent still negotiates
capabilities in `initialize`, and its `_client_file_system` and
`_client_terminal` properties still decide whether the already-canonical
client-I/O adapters are created. No capability decision moves into the
transport module.

### Dependency direction and frozen behavior

Before extraction, the top-level ACP adapter directly imported the SDK router,
connection, schemas, stdio streams, and normalization helpers. The intended
direction after extraction is:

```text
neuro_code.acp public wrappers
        -> neuro_code.interfaces.acp.transport
        -> ACP SDK connection/router/schema/stdio primitives
        -> injected ACP Agent protocol
```

The canonical transport module must not import `neuro_code.acp`, bootstrap,
infrastructure, providers, stores, or application composition. It must not
construct an Agent from a service, inspect a session registry, make permission
decisions, validate a workspace, configure a sandbox, register tools, or call
a Provider.

Existing behavior was frozen by `tests/test_acp.py`,
`tests/test_acp_raw_stdio.py`, and `tests/test_acp_e2e.py`, including router
dispatch, private alias use, stdio setup and shutdown, WebSocket dependency
failure, host/port validation, text/binary conversion, newline framing,
message bounds, writer batching, feeder cancellation, and per-connection
cleanup.

## Decision

`neuro_code.interfaces.acp.transport` is the canonical owner of:

- the SDK router extension and `_AcpSdkConnection`;
- the WebSocket writer bridge;
- the official stdio stream entrypoint;
- the bounded WebSocket server/reader feeder;
- the transport-local 1 MiB buffer bound; and
- the outer connection close and injected-Agent shutdown lifecycle.

The canonical API receives an already-constructed Agent for stdio and an
Agent factory for WebSocket connections. The optional connection, stream, and
writer factories are narrow test/compatibility seams; they do not introduce a
second implementation or change ownership.

`neuro_code.acp` retains only the service-to-Agent public wrappers for these
entrypoints plus identity-preserving private imports of the moved symbols.
`NeuroCodeAcpAgent` remains the owner of protocol semantics, capability
negotiation, connection attachment, session registry/publication, extension
dispatch, live MCP orchestration, and application-facing lifecycle routing.

## Router and SDK connection

The canonical router still calls the official SDK
`build_agent_router(agent, use_unstable_protocol=True)`. It adds only the
generated stable `DeleteSessionRequest` route that the SDK 0.11 Agent router
omits, using the existing `AGENT_METHODS`, `MessageRouter`, and
`normalize_result` behavior. No home-grown JSON-RPC dispatcher is introduced.

`_AcpSdkConnection` still creates the SDK `Connection` with the canonical
router and `listening=False`, then calls the injected Agent's `on_connect`
with the connection client surface. Its `listen`, `close`,
`session_update`, and `request_permission` methods preserve the existing SDK
notification/request schemas and normalization.

## STDIO boundary

`serve_stdio` still obtains streams from the official SDK `stdio_streams`
with `limit=ACP_STDIO_BUFFER_LIMIT_BYTES`, creates one SDK connection, and
awaits its main loop. It closes the connection under `asyncio.shield` and
then shuts down the injected Agent under `asyncio.shield`, preserving the
existing exception propagation and cleanup order. Agent shutdown also occurs
when stream setup fails. The public legacy wrapper constructs the Agent and
injects the historical `neuro_code.acp.stdio_streams` alias, so existing
private test patching remains valid.

## WebSocket boundary

`serve_websocket` preserves host and port validation and fails closed with the
existing `ConfigurationError` when the optional `websockets` dependency is
missing. It configures the official server with the 1 MiB maximum message
size and `max_queue=16`.

Each accepted connection receives a fresh Agent, `StreamReader` with the same
1 MiB limit, `_WebSocketWriter`, and SDK connection. Text frames are encoded
as UTF-8; bytes are forwarded unchanged; unsupported frame values, empty
messages, and oversized messages fail closed with the existing connection
errors. A missing trailing newline is appended before data reaches the SDK
reader. The feeder is cancelled and joined before the connection is closed;
the Agent is then shut down exactly once. The writer retains its existing
batched `write`/`drain`, closed-state, and no-op `wait_closed` behavior.

Interactive stdin, resize, PTY framing, cursor streaming, and a general
WebSocket transport framework remain out of scope.

## Compatibility and architecture contracts

`neuro_code.acp` imports `ACP_STDIO_BUFFER_LIMIT_BYTES`,
`_build_acp_router`, `_AcpSdkConnection`, `_WebSocketWriter`, and
`stdio_streams` from the canonical module. The moved private classes and
helpers report the canonical module as `__module__`, while old imports retain
object identity. The public entrypoint signatures remain service-based.

Architecture tests assert that transport definitions exist only in the
canonical module, the legacy names are identity-preserving aliases, the
canonical module has no reverse/concrete application dependency, no Agent
construction or session/permission state, and the Agent retains capability
and session ownership. Existing ACP tests assert the observable protocol and
cleanup behavior.

## Explicit non-goals

This ADR does not change client filesystem/terminal adapters, capability
negotiation, Agent protocol semantics, session lifecycle or runtime ownership,
MCP, permissions, workspace or sandbox policy, provider behavior, background
terminal tasks, retries, replay, checkpoint/rollback, automatic delegation,
writable subagents, parallel/dataflow execution, UI behavior, or the ACP
application service.

## Status and validation

This ADR is Proposed until the stacked pull request's merge-ref CI is fully
green. Local focused tests and the repository quality gates are necessary but
not sufficient for freezing the boundary. After a green merge-ref result,
the ADR may be marked Accepted together with the slice's final commit and
PR metadata.
