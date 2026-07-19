# ADR 0038: Session-owned stdio MCP tools

[简体中文](../../zh-CN/adr/0038-session-owned-stdio-mcp-tools.md) · **English**

- Status: accepted
- Date: 2026-07-19

## Context

The partial ACP v1 adapter rejected every non-empty `mcpServers` request. ACP
stdio MCP servers are a baseline input, while HTTP, SSE, and ACP transports
have separate optional capability flags. Supporting the stdio baseline closes
the largest remaining gap without advertising those optional transports.

Neuro Code must not implement a private MCP schema or JSON-RPC dispatcher. It
must also retain process-tree guarantees that are stricter than the official
MCP SDK's transport: POSIX children belong to a dedicated process group, and a
Windows leader must be created atomically inside a kill-on-close Job rather
than spawned and attached afterward.

MCP servers and their annotations are untrusted. Configuration, environment,
tool catalogs, arguments, results, stderr, cancellation, and cleanup therefore
need explicit limits and fail-closed ownership.

## Decision

- Pin the official MCP Python SDK to `mcp>=1.28.1,<2`. Use its
  `ClientSession`, schemas, version negotiation, JSON-RPC dispatch, tool
  pagination, calls, and result types.
- Accept only ACP `McpServerStdio` values. Keep ACP
  `agentCapabilities.mcpCapabilities` absent because no optional HTTP/SSE
  transport is implemented. Reject HTTP, SSE, and ACP transports with
  `mcp_transport_unsupported`.
- Validate bounded server counts, names, commands, arguments, environment,
  serialized configuration, tool pages/counts, tool names, schemas, frames,
  JSON complexity, call arguments, results, and timeouts. Ignore `_meta`.
- Start servers in the connection workspace with a small SDK-defined inherited
  environment plus bounded client values. Reject attempts to override active
  provider/proxy environment names. Treat every explicit value as a redaction
  source.
- Keep the official session and dispatcher but bridge its typed messages over
  the existing `ProcessTree`. The bridge only performs bounded UTF-8,
  newline-delimited transport; it does not implement MCP request routing or
  schema interpretation. MCP stderr is drained and discarded so it cannot
  block the child, pollute ACP stdout, or expose credentials.
- Initialize each server and validate the complete initial tool catalog before
  publishing `session/new` or `session/load`. Reject duplicate remote names and
  collisions with built-in tools. MCP configuration is ephemeral and must be
  provided again when loading a durable session.
- Project tools only. Sanitize and redact text, structured content, and
  ResourceLink metadata; never dereference ResourceLinks. Omit image, audio,
  and embedded bodies using bounded placeholders.
- Treat every MCP tool as side-effecting regardless of untrusted annotations.
  Add an exact ASK rule that takes precedence over bypass/always-approve while
  retaining explicit local DENY precedence. The existing runtime consequently
  preserves pending → client permission → in-progress → terminal ordering.
- Serialize calls per server. On prompt cancellation, call timeout, transport
  failure, close, or disconnect, abort the official SDK request and terminate
  the complete server tree before resolving the tool call. Session cleanup is
  idempotent and isolated from other sessions.

## Consequences

- Standard ACP clients can supply bounded stdio MCP tool servers to new and
  loaded sessions without expanding the connection workspace or bypassing
  local safety policy.
- ACP and MCP schemas/dispatch remain official-SDK owned while Neuro Code keeps
  its atomic Windows Job and POSIX process-group invariants.
- A cancelled or indeterminate call makes that server unavailable for later
  calls in the session. This conservative behavior prevents an unacknowledged
  remote side effect from continuing.
- The adapter remains partial ACP v1. HTTP/SSE/ACP MCP transports, resources,
  prompts, sampling, elicitation, dynamic tool-list refresh, configuration
  persistence, and multimedia/embedded result projection remain future slices.
