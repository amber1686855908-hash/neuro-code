# ADR 0148: ACP MCP Configuration Boundary

- Status: Accepted
- Date: 2026-08-30
- Scope: fourth structural slice of V1 Interface Boundary Consolidation
- Depends on: ADR 0052, ADR 0053, ADR 0056, ADR 0145, ADR 0146, and ADR 0147

## Context

The frozen PR #75 head is
`3e8a8cd6796b886213cf57bc70231b415d07ca5c`. The top-level
`neuro_code.acp` adapter still contains ACP protocol handling, session
lifecycle, live MCP callbacks and tool lifecycle, transport, and the
stateless conversion of inbound MCP server declarations.

This slice extracts only that last responsibility: bounded ACP MCP
configuration input validation and conversion into the existing application
`AcpMcpServerConfig` contracts. It does not move live MCP execution, callback
dispatch, session state, or transport behavior.

## Pre-change MCP boundary audit

The audit was performed against the exact PR #75 head before code movement.

### Configuration symbols

The following symbols were configuration-only and formed one cohesive
boundary:

- `McpServer`, the ACP input union for HTTP, SSE, ACP-transport, and stdio
  declarations;
- `_mcp_string`, the bounded string/control-character validator;
- `_mcp_server_configurations`, the conversion entry point;
- `_mcp_http_url`, the URL validator;
- `_mcp_http_headers`, the header validator;
- `_ENVIRONMENT_NAME`, `_HTTP_HEADER_NAME`, and
  `_RESERVED_MCP_HTTP_HEADERS`; and
- the server-name, command, argument, environment, URL, header, and total
  configuration bounds beginning with `MAX_MCP_`.

The ACP schema input classes above are owned by the ACP SDK. The output
`AcpMcpStdioServerConfig`, `AcpMcpHttpServerConfig`, and
`AcpMcpServerConfig` contracts remain application-owned and are consumed by
the agent/service seam; they are not duplicated in the interface module.

`MAX_MCP_SERVERS` was deliberately not moved or redefined. It is an
application ACP contract also consumed by the concrete stdio and HTTP MCP
runtime adapters, so its canonical owner remains
`application.acp.contracts`.

`MAX_MCP_URL_BYTES` and `MAX_MCP_CONFIGURATION_BYTES` are defined by the new
configuration module because they are configuration bounds. The live ACP
callback and metadata projections may reuse these values, but their live
validation and projection code remains in `neuro_code.acp`.

### Symbols deliberately outside the boundary

The audit classified the remaining ACP/MCP symbols as follows:

| Classification | Existing responsibility | Decision |
| --- | --- | --- |
| `MCP_LIVE_CALLBACK` | `_safe_mcp_callback_payload`, `_mcp_sampling_handler`, `_mcp_elicitation_handler`, `McpSamplingHandler`, `McpElicitationHandler`, `MAX_MCP_SAMPLING_MESSAGES`, `MAX_MCP_SAMPLING_TOKENS`, `MAX_MCP_ELICITATION_MESSAGE_BYTES`, and `MAX_MCP_CALLBACK_BYTES` | Retain in the ACP agent; these need live client and session state |
| `MCP_SESSION_LIFECYCLE` | `AcpMcpTools`, `AcpMcpToolError`, `mcp_tools`, `mcp_tool_names`, `_open_mcp_tools`, and MCP cleanup | Retain in the ACP agent/service lifecycle |
| `MCP_PRIVATE_EXTENSION` | `_safe_mcp_extension_value`, `_mcp_list_payload`, `_mcp_extension`, `AcpMcpQuery`, `AcpMcpQueryError`, `ACP_MCP_EXTENSION`, and `MAX_MCP_RESOURCE_BYTES` | Retain as private extension protocol behavior |
| `SESSION_LIFECYCLE` | `NeuroCodeAcpAgent`, `_AcpSession`, session registry, aliases, reservations, publication, activation, fork, cleanup, prompt, cancel, and permission coordination | Retain unchanged |
| `TRANSPORT` | router, connection, stdio stream, and WebSocket bridge symbols | Retain unchanged |
| `SHARED_ACP_VALIDATION` | `serialized_size_bytes`, `RequestError`, `MAX_MCP_SERVERS`, and the reused configuration-owned URL/serialized-size bounds | Reuse existing owners; do not create a generic MCP validator bucket |
| `OTHER` | `McpCapabilities` in `initialize` and the remaining ACP schema/session response types | Retain as capability negotiation or general ACP protocol mapping |

`_safe_mcp_extension_value` was specifically reviewed and remains outside
this slice because it primarily protects live MCP sampling, elicitation, and
private extension result projections. It is not a configuration serializer.

### ACP agent call sites and capability boundary

`NeuroCodeAcpAgent._validate_session_workspace` remains the caller that first
validates the workspace and then supplies
`self._service.protected_environment_variables` to the configuration parser.
`new_session`, `load_session`, `resume_session`, and `fork_session` continue
to receive ACP `mcpServers`, validate them before session publication, and
pass the resulting application configurations to `_open_mcp_tools`.

Capability negotiation remains in `initialize`. `_open_mcp_tools`, live
sampling/elicitation callback construction, session-owned `AcpMcpTools`, and
cleanup remain in the ACP agent/application service path.

The protected environment set therefore continues to flow as:

```text
ACP agent/service
        -> protected_environment_variables
        -> canonical MCP configuration parser
        -> AcpMcpServerConfig
```

The parser does not read global state, scan the process environment, inspect
bootstrap configuration, or obtain authority from infrastructure, providers,
or stores.

### Existing behavior and tests

The frozen behavior was covered by the `McpConfigurationTests` in
`tests/test_acp.py` and by ACP integration tests. The audit confirmed coverage
for valid HTTP/SSE/stdio inputs, application contract construction, server and
argument bounds, environment validation and protection, URL and header
validation, duplicate and reserved names, unsupported ACP transport, the
serialized configuration bound, session-owned MCP opening, cancellation, and
cleanup. `tests/test_acp_e2e.py`, `tests/test_acp_raw_stdio.py`,
`tests/test_mcp_stdio.py`, and `tests/test_mcp_http.py` cover the downstream
runtime and protocol surfaces and remain integration tests rather than parser
tests.

## Decision

`neuro_code.interfaces.acp.mcp_config` is the canonical owner of the
stateless ACP MCP configuration conversion. It imports the ACP SDK schema,
existing application ACP configuration contracts, and the existing canonical
ACP serialized-size helper.

`neuro_code.acp` imports the canonical function, type alias, helpers, and
configuration bounds as direct compatibility aliases. It remains the
protocol caller and continues to own live MCP, session, permission, and
transport behavior.

## Supported ACP input shapes

The accepted behavior remains exactly the frozen behavior:

- stdio `McpServerStdio` declarations become
  `AcpMcpStdioServerConfig`;
- Streamable HTTP `HttpMcpServer` declarations become
  `AcpMcpHttpServerConfig(transport="http")`;
- legacy SSE `SseMcpServer` declarations become
  `AcpMcpHttpServerConfig(transport="sse")`; and
- ACP-transport `AcpMcpServer` declarations are recognized as an ACP schema
  input but rejected with `mcp_transport_unsupported`, as before.

No new application contract or transport discriminator was introduced.
Names, arguments, environment tuples, URLs, headers, and transport values
retain their existing representation.

## Preserved stdio validation

The parser preserves:

- non-empty bounded server names and case-insensitive duplicate detection;
- direct executable command validation without shell parsing;
- bounded argument count, individual argument bytes, and aggregate argument
  bytes;
- bounded environment count, identifier syntax, case-insensitive duplicate
  detection, protected-name rejection, value bytes, and aggregate bytes;
- empty argument and environment value behavior;
- control-character rules, including the existing allowance for environment
  values; and
- the existing invalid-parameter reason strings.

Protected environment matching remains case-insensitive and is performed
only against the set supplied by the ACP service.

## Preserved HTTP and SSE validation

The parser preserves:

- HTTP/HTTPS scheme and host requirements;
- rejection of embedded credentials, fragments, invalid ports, and oversized
  URLs;
- bounded header count, names, values, and aggregate bytes;
- case-insensitive duplicate header rejection;
- reserved framing/routing header rejection; and
- exact URL wire-value preservation without normalization or network access.

No redirects, DNS lookup, socket creation, or endpoint probing occurs during
configuration conversion.

## Serialized configuration bound

The parser continues to build the same canonical JSON-compatible projection
and applies `serialized_size_bytes` to that projection before returning
configuration contracts. `MAX_MCP_CONFIGURATION_BYTES` remains a UTF-8
serialized payload bound; it is not replaced by `sys.getsizeof` or another
in-memory measurement.

## Zero-I/O and dependency direction

The dependency direction is:

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.mcp_config
        -> ACP SDK schema
        -> application ACP configuration contracts
        -> interfaces.acp.serialization
```

The canonical module has no dependency on `neuro_code.acp`, bootstrap,
infrastructure, providers, or stores. It performs no filesystem I/O,
subprocess creation, network access, environment mutation, database access,
provider call, or MCP connection opening.

## Compatibility and private aliases

The old `neuro_code.acp` path retains direct aliases for the private parser
helpers and the configuration-only bounds because existing repository tests
and internal call sites use those names. The aliases preserve object identity;
they are not wrappers and no duplicate configuration implementation remains
in `acp.py`. The moved private helpers do not become public ACP API merely
because their owner changed, and they are not added to an interface package
barrel.

The application configuration dataclasses remain owned by
`application.acp.contracts`. `MAX_MCP_SERVERS` remains there as a shared
application/runtime bound. The live ACP callback and extension code remains
in the ACP agent even when it reuses a configuration-owned URL or serialized
size bound.

## Live MCP, session, and transport non-migration

This ADR deliberately does not move or redesign:

- sampling or elicitation callbacks and callback payload projection;
- `AcpMcpTools` opening, refresh, tool execution, resource/prompt reads, or
  session-owned cleanup;
- private `ext_method` routing and `neuro-code/session/mcp` behavior;
- `NeuroCodeAcpAgent`, `_AcpSession`, session registry, aliases, reservations,
  publication, activation, fork, prompt, cancel, or permission coordination;
- ACP capability negotiation;
- stdio, WebSocket, or MCP infrastructure transports; or
- retry, failure, sandbox, provider, checkpoint/rollback, delegation, or UI
  behavior.

## Validation and acceptance

The slice adds focused canonical configuration tests and architecture guards
for canonical ownership, direct alias identity, forbidden reverse/concrete
dependencies, protected-environment caller ownership, and absence of
authoritative duplicate parser definitions in `neuro_code.acp`.

Acceptance requires focused ACP/MCP tests, ACP raw-stdio and E2E tests,
repository quality gates, documentation parity, and a fully green PR
merge-ref CI. Local tests alone do not freeze this boundary.
