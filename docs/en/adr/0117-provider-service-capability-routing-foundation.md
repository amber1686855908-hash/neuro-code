# ADR 0117: Provider service, capability, and runtime-route foundation

- Status: Accepted; hosted web execution remains intentionally unimplemented
- Date: 2026-08-19
- Scope: Provider metadata, capability resolution, model discovery, and role routes

## Context

The current Provider Runtime already owns named profiles, credentials, proxy
policy, context affinity, failover, four wire protocols, provider-specific
dialects, OpenAI Responses, and xAI hosted-tool events. Replacing that runtime
would create compatibility risk. The remaining architectural gap is that a
profile, an inference service, a wire protocol, a model, a capability, and a
runtime role are not represented as separate boundaries. In addition, the TUI
owned the six selectable Provider presets and model discovery inferred its
strategy mainly from the protocol.

## Decision

### 1. Service metadata is a catalog, not a runtime hierarchy

`ProviderServiceDescriptor` and `ProviderServiceCatalog` are canonical
application-port metadata. The default catalog describes these six services:

| Service ID | Existing UI key | Wire default |
|---|---|---|
| `openai` | `openai` | OpenAI Responses |
| `generic-openai-compatible` | `compatible` | OpenAI Chat |
| `deepseek` | `deepseek` | OpenAI Chat + `deepseek-v4` dialect |
| `anthropic` | `anthropic` | Anthropic Messages |
| `google-ai-studio` | `gemini` | Gemini generateContent |
| `xai` | `xai` | OpenAI Responses + `xai` dialect |

Publisher information is optional metadata. It does not select an adapter,
hold credentials, or own request execution. Adding a future service should
require a descriptor, optional catalog strategy, capability metadata, and
tests; it must not require an AgentLoop, ToolExecutor, or TUI provider branch.

The six default descriptors intentionally remain beside this application-port
contract because they are immutable selection metadata, not infrastructure
adapters: they import no HTTP/client implementation, credential store, or
request lifecycle. The TUI and configuration consume an injected catalog, so
moving these values into infrastructure would add a dependency direction
without removing vendor knowledge from the interface layer.

### 2. Existing protocols and dialect quirks remain authoritative

The four existing protocols remain the wire boundary. Infrastructure selects
the adapter from the protocol. The DeepSeek DSML behavior remains a bounded
`deepseek-v4` dialect in the existing OpenAI-compatible adapter; it is not
flattened into generic OpenAI behavior. The xAI Responses dialect and its
existing request/streaming behavior remain in place.

### 3. Capabilities are canonical, layered, and fail closed

`ModelCapability` names the small cross-provider vocabulary needed by the next
stages. `ModelCapabilitySet` records `supported`, `unsupported`, and `unknown`
statuses. The upstream fact chain refines, in order:

```text
service -> protocol -> model
```

The runtime then resolves a provenance-preserving `CapabilityResolution`:

```text
upstream facts
    meet trusted adapter implementation capabilities
    restrict explicit profile/configuration disables
    = effective executable capability
```

An upstream or profile `SUPPORTED` claim cannot elevate an adapter `UNKNOWN` or
`UNSUPPORTED` capability. Only an explicitly `SUPPORTED` effective capability
makes `supports()` true. Unknown is not treated as supported. The adapter
implementation set is selected by the concrete wire adapter and, for xAI,
the configured builtin-tool names. Provider-hosted names are mapped at that
trusted adapter boundary to canonical hosted capabilities; the current xAI
`web_search`, `x_search`, and `code_interpreter` behavior remains unchanged.

Managed metadata may persist service identity and capability preferences,
including explicit disables or supported claims for inspection. It is not
authoritative runtime evidence: reload still passes through the upstream,
adapter-implementation, and configuration resolution above.

### 4. Hosted tools remain distinct from local tools

Provider-hosted tools continue to execute inside the provider API and use the
existing `ModelBackendToolStarted` / `ModelBackendToolCompleted` lifecycle.
They do not become `ToolRegistry`, `ToolExecutor`, permission, or sandbox
tools. No OpenAI, Anthropic, or Gemini hosted-web behavior is enabled by this
ADR, and no new xAI behavior is introduced.

### 5. Runtime roles and routes are an additive projection

`RuntimeRole` currently contains `MAIN` and `WEB_SEARCH`. `ModelRoute` binds a
role to a provider profile and model, with isolated fallbacks. It deliberately
does not encode Web Search execution strategy. A future Web-specific type may
own inline-versus-sidecar selection once Web execution is implemented.

The future semantics are explicit: `INLINE_HOSTED` means the MAIN inference
request itself enables a provider-hosted search, with no local `web_search`
tool; the provider continues to emit the existing
`ModelBackendToolStarted` / `ModelBackendToolCompleted` lifecycle. `SIDECAR_HOSTED`
means the MAIN agent invokes a client-side tool which will later pass through
`ToolExecutor`, a WebSearchService, an independent `WEB_SEARCH` route, the
search provider, a canonical result, and a `ToolResult`. Even when both routes
select the same provider, an independent model request remains sidecar rather
than inline. These semantics are recorded here only; neither path is enabled
by this ADR.

Existing `[routing] default` and `fallbacks` remain authoritative for the
active main runtime and are projected to `RuntimeRole.MAIN`. An optional
`[routing.web_search]` table is validated and redacted as configuration only;
it is not invoked by the AgentLoop in this stage. Role-specific fallbacks are
never inferred across roles or profiles.

### 6. Model discovery uses an explicit strategy seam

`ProviderConnectionSpec.catalog_strategy` and the descriptor's catalog
strategy can select `openai-compatible-models`, `anthropic-models`,
`gemini-models`, `static`, or `manual-only`. Existing protocol defaults remain
the compatibility fallback, but future services do not need another protocol
conditional merely to use a different model-list strategy. Manual model IDs
remain valid when a service has no stable discovery endpoint.

### 7. TUI consumes the catalog

`ProviderSettingsScreen` renders the injected `ProviderServiceCatalog` and
reads endpoint, protocol, dialect, labels, and discovery strategy from each
descriptor. Existing UI keys and persisted profile matching preserve current
behavior. The TUI no longer owns the six Provider preset definitions.

### 8. Security and compatibility boundaries are unchanged

Service, route, and capability metadata never contain credentials. Existing
`providers.json` / `credentials.json` separation, environment/direct/explicit
HTTP policy, redaction, context affinity, provider failover, native replay,
prompt-cache behavior, and provider adapter ownership remain in force.

Before a failover candidate is selected, `FailoverModelProvider.capabilities`
returns the safe intersection of every configured candidate. A supported
primary plus an unsupported fallback therefore cannot expose the hosted
capability before the request starts. After the first candidate is selected,
capabilities follow that active provider. This is intentionally the minimal
fail-closed policy; future route-aware hosted services may add required-
capability candidate filtering without changing the monotonic failover loop.

## Non-goals

- No new provider adapter for Kimi, GLM, MiniMax, Volcengine Ark, Baidu
  Qianfan, Alibaba Bailian, or Tencent TokenHub.
- No `web_search` or `web_fetch` local tool, Search Sidecar, local HTTP fetcher,
  provider-native web implementation, or generic route execution mode.
- No AgentLoop, ToolRegistry, ToolExecutor, PermissionManager, Sandbox, or
  conversation-core rewrite.
- No keyring migration.

## Consequences

The service/profile/protocol/model/capability/role/route vocabulary is now
explicit enough to validate a future `DeepSeek MAIN + Gemini WEB_SEARCH`
route without changing the AgentLoop core. The runtime still has exactly one
active execution path: the existing main Provider/failover path. Future
hosted-web work must own inline-versus-sidecar selection in a Web-specific
application boundary, then use the generic `WEB_SEARCH` route and the same
HTTP policy. A same-provider search request is not automatically inline;
inline requires that the MAIN inference lifecycle itself enables a hosted
capability.

## Evidence

The contract is covered by Provider Service Catalog, capability, route,
ProviderCatalog strategy, and TUI injection tests. Existing Provider, xAI
hosted-tool, failover, configuration, and TUI regression suites remain part of
the acceptance gate.
