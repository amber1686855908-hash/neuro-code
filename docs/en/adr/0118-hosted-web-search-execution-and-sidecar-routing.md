# ADR 0118: Hosted Web Search execution and sidecar routing

- Status: Accepted; P1 vertical slice
- Date: 2026-08-19
- Scope: Canonical Web Search, OpenAI/xAI hosted search, route selection, and MAIN tool wiring

## Context

Provider service metadata, tri-state capabilities, and role routes now exist,
but they do not yet execute Web Search. The next increment needs current web
evidence without allowing a provider-native payload, an external page, or a
model-generated query to become trusted application instructions. It also needs
to preserve the existing Responses lifecycle, xAI behavior, MAIN failover, and
ToolExecutor ownership.

The slice is deliberately limited to hosted search. It does not establish a
general Web capability framework for fetching pages, browsing, or adding new
providers.

## Decision

### 1. Keep one provider-neutral, bounded contract

`neuro_code.application.ports.web_search` owns `WebSearchRequest`,
`WebSearchSource`, `WebSearchCitation`, `WebSearchResult`, `WebSearchError`,
`WebSearchMode`, and the hosted-search execution port. The contract contains no
OpenAI, xAI, Responses, SSE, or provider-private types.

The request bounds the query, source count, and domain filters. Allowed and
blocked domains are mutually exclusive and are normalized as ASCII hostnames.
The result bounds evidence, source/citation count, URLs, titles, snippets,
provider/model labels, metadata, and total UTF-8 bytes. Sources and citations
are deduplicated; a provider payload that cannot be projected into this shape
is not returned to the application.

The stable error vocabulary is:
`SEARCH_UNAVAILABLE`, `SEARCH_UNSUPPORTED`, `SEARCH_AUTHENTICATION`,
`SEARCH_RATE_LIMIT`, `SEARCH_TIMEOUT`, `SEARCH_PROVIDER_ERROR`,
`SEARCH_PROVIDER_DID_NOT_SEARCH`, and `SEARCH_INVALID_REQUEST`.

### 2. Resolve intent before registering a client tool

`WebSearchMode` has four explicit values:

| Mode | Resolution |
|---|---|
| `disabled` | no inline capability and no local search tool |
| `auto` | MAIN inline hosted search when explicitly supported; otherwise sidecar when the WEB_SEARCH route is executable; otherwise unavailable |
| `inline` | MAIN hosted search only; unsupported capability fails closed |
| `sidecar` | independent WEB_SEARCH route only; MAIN hosted search is stripped from that binding |

`RuntimeRole.MAIN` and `RuntimeRole.WEB_SEARCH` remain independent. A MAIN
fallback is never used as a search fallback, and a search fallback never changes
the MAIN provider. The generic `ModelRoute` still contains only role, profile,
model, and isolated fallback names; execution mode belongs to the Web Search
boundary.

### 3. Put routing in an application service and provider construction at composition

`WebSearchService` owns the WEB_SEARCH route lookup, capability status check,
redacted request/result boundary, and normalized error projection. It resolves
only the explicit search route through a provider-neutral resolver. The
composition root constructs `ResponsesHostedWebSearchBackend` instances for
the configured OpenAI Responses profiles and creates the optional
`WebSearchTool` only for the resolved sidecar path.

The sidecar is exactly one bounded hosted request. It does not create an
`AgentLoop`, `Subagent`, workspace, shell, permission, or write capability. The
existing `ToolExecutor` executes the client tool, pairs the canonical
`ToolResult` back to MAIN, and forwards the existing backend lifecycle events
through the existing event sink.

### 4. Use explicit OpenAI hosted capability and preserve xAI

The standard OpenAI Responses adapter accepts only the explicit builtin
`web_search` name. When it is configured, the request body contains the hosted
tool and the source include needed for structured source extraction. When it is
not configured, standard OpenAI receives no hosted search capability. A search
sidecar additionally sends the canonical domain filter in the selected wire
dialect and sets `tool_choice` to `required`; it never relies on `auto` for a
request whose contract promises search evidence.

The xAI Responses dialect continues to use its existing builtin-tool set,
native context affinity, reasoning include, and backend lifecycle behavior.
The sidecar reuses the same Responses adapter and HTTP policy; it does not add
a second xAI implementation. Canonical blocked domains map to xAI's
`excluded_domains` (and to standard Responses `blocked_domains`); xAI's bounded
domain-filter limit is rejected before the request is sent. The sidecar
exposes only `web_search`, even when the selected xAI MAIN profile also has
other hosted tools.

Anthropic hosted search, Gemini search/context, local fetch, browser, LSP,
Tool Search, and new provider adapters are not part of this decision.

### 5. Treat web evidence as untrusted external data

The extractor reads only bounded structured source/citation fields, including
Responses `web_search_call.action.sources` and current nested output-text
`url_citation` payloads. Structured terminal evidence is authoritative; a
sidecar fails closed with `SEARCH_PROVIDER_DID_NOT_SEARCH` when the terminal
response has no completed provider-side `web_search_call` or equivalent
provider-side usage/citation evidence, even if it contains a plausible
model-written answer. xAI's complete URL citation list is projected as
bounded sources, while inline Markdown citations remain a bounded
compatibility fallback, and inline assistant text receives only a
bounded visible URL list so the TUI's non-activating Markdown renderer does not
silently lose sources. Raw provider responses, arbitrary annotations, page
instructions, and unbounded payloads do not cross the provider boundary.

The model-visible local result starts with `[UNTRUSTED WEB EVIDENCE]`, identifies
the query, lists bounded sources and evidence, and places the bounded synthesis
after that boundary. Source URLs are restricted to HTTP(S), domain filters are
applied locally as a second check, and redaction is applied to the query,
evidence, source metadata, citations, provider label, model label, events, and
persisted Web Search call arguments.

### 6. Keep cancellation and lifecycle ownership explicit

Cancellation propagates through `WebSearchTool`, `WebSearchService`, the
sidecar provider stream, and the Responses HTTP context; the sidecar does not
continue or fail over after cancellation. Hosted backend start/completion
events remain provider-neutral and use the existing `BACKEND_TOOL_STARTED` /
`BACKEND_TOOL_COMPLETED` event kinds. Credentials never enter canonical
contracts, tool results, TUI projections, or logs. Sidecar usage is auxiliary
and is not merged into MAIN's model usage or context-budget accounting.

Configuration supports `[web_search] mode`, explicit standard OpenAI
`builtin_tools`, and an independent `[routing.web_search]` chain. The current
TUI can continue to configure providers through its catalog; a full Web Search
settings editor is intentionally deferred until the runtime contract has more
provider capability evidence. The runtime remains capability-aware and fails
closed rather than presenting unsupported hosted search as available.

## Non-goals

- Anthropic search/fetch or Gemini search/context.
- Local HTTP fetch, browser control, LSP, Tool Search, or general web browsing.
- New provider adapters or a provider-specific application import.
- Provider-private request/response objects in application contracts.
- A Search Sidecar implemented as an AgentLoop or subagent.
- Search-result trust elevation, automatic page instruction execution, or
  workspace mutation based on external evidence.

## Consequences

The current vertical slice supports OpenAI and xAI hosted Web Search through
one bounded, testable contract and supports DeepSeek/OpenAI-compatible MAIN
tool use through an independent search route. Future Anthropic/Gemini hosted
search and Local Fetch can implement the same provider-neutral boundary without
changing ToolExecutor or the route model. The explicit untrusted boundary and
structured extraction rules remain the compatibility gate for those additions.
Portable contract/provider/TUI tests pass; OpenAI and xAI live smoke tests are
opt-in and remain skipped unless their own credential and network flags are
supplied; each test allows a model override.

## Evidence and references

- OpenAI hosted web search guide: [Responses `web_search`, filters, sources,
  and tool choice](https://platform.openai.com/docs/guides/tools-web-search)
- xAI documentation: [Web Search](https://docs.x.ai/developers/tools/web-search),
  [citations](https://docs.x.ai/developers/tools/citations), and
  [tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- `tests/test_web_search.py`, `tests/test_openai_responses_provider.py`,
  `tests/test_provider_routes.py`, `tests/test_tui.py`,
  `tests/live/test_openai_web_search_live.py`,
  `tests/live/test_xai_web_search_live.py`, and the existing xAI Responses
  regression suite.
