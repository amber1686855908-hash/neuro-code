# ADR 0051: Bounded remote MCP transports

[简体中文](../../zh-CN/adr/0051-bounded-remote-mcp-transports.md) · **English**

- Status: accepted
- Date: 2026-07-29

## Context

ADR 0038 established session-owned stdio MCP tools. ACP also defines optional
HTTP and SSE server forms. The pinned official MCP SDK already provides the
Streamable HTTP and legacy SSE transports, so duplicating their schema,
negotiation, or JSON-RPC implementation would create an incompatible second
protocol stack.

Remote servers are not children of Neuro Code. They therefore cannot inherit
the POSIX process-tree or atomic Windows Job guarantees that make stdio
cancellation definitive. Remote configuration and server responses remain
untrusted and may contain credentials, proxy surprises, unbounded bodies, or
unsafe transport input.

## Decision

- Accept ACP `McpServer::Http` as Streamable HTTP and `McpServer::Sse` as
  legacy SSE. Advertise `mcpCapabilities.http = true` and `sse = true`.
  Continue rejecting unstable ACP-transport MCP servers.
- Keep `ClientSession`, schemas, version negotiation, and JSON-RPC dispatch in
  the official `mcp>=1.28.1,<2` SDK. Neuro Code only owns validation, bounded
  HTTP client construction, tool projection, lifecycle, and permission wiring.
- Require an absolute HTTP/HTTPS URL with a host, no embedded user credentials,
  no fragment, bounded bytes, and a valid port. Bound header count, names,
  values, and aggregate size; reject duplicate, hop-by-hop, framing, routing,
  and proxy headers. Treat every configured header value as a redaction source.
- Use an application-owned `httpx.AsyncClient` that keeps TLS verification,
  disables environment proxy inheritance and redirects, and caps every remote
  response body at 1 MiB. No secret is emitted in a stable error reason.
- Preserve session ownership: initialize and validate every tool catalog before
  publishing a session; apply collision and aggregate limits across stdio and
  remote collections; keep configuration ephemeral; and close all collections
  idempotently on creation failure, close, EOF, or disconnect.
- Project tools only and treat every remote tool as side-effecting. Existing
  exact ASK-over-bypass approval and local DENY precedence apply unchanged.
- On remote cancellation, timeout, or transport failure, close the local SDK
  connection and make it unavailable for later calls. Do not claim that an
  in-flight remote side effect was stopped or successfully cancelled.

## Consequences

- Standard ACP clients can use bounded HTTP and SSE MCP tools without a custom
  MCP protocol implementation or an environment-proxy dependency.
- The user receives a permission decision before every remote tool invocation,
  but cancellation remains intentionally conservative: later calls fail closed
  instead of reusing an indeterminate connection.
- MCP ACP transport, resources, prompts, sampling, elicitation, dynamic
  list-change refresh, configuration persistence, and multimedia/embedded
  projection remain separate future slices.
