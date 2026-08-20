# ADR 0120: Gemini Interactions and hosted Web Search tools

- Status: Accepted; P1.2 vertical slice
- Date: 2026-08-19
- Scope: Gemini Interactions, Google Search, URL Context, and Gemini route composition

## Context

The existing Gemini adapter owns the legacy `generateContent` and
`streamGenerateContent` contract. Google now recommends the Interactions API
for new development. Interactions exposes a typed step timeline, stateless
full-history input, Google Search, URL Context, and client function calls. A
second adapter is required because those step and continuation semantics are
not equivalent to `generateContent` candidates and parts.

The application already owns session persistence, resume/fork, provider
affinity, tool execution, and role routing. Letting Gemini own a second durable
conversation would create two authorities and would make provider switching
ambiguous.

## Decision

1. `gemini-interactions` is a separate protocol and
   `GeminiInteractionsProvider` is its sole wire owner. The Google AI Studio
   service advertises both `gemini-generate-content` and
   `gemini-interactions`; the former remains the default for compatibility.
   Existing profiles are never silently rewritten.
2. The adapter targets stable API `v1` at `/v1/interactions`, sends
   `store = false`, and never sends `previous_interaction_id`. It sends the
   complete Neuro Code context on every request. A `v1beta` base URL in an
   existing profile is normalized to the stable Interactions endpoint without
   changing the persisted profile.
3. Interactions SSE events are translated to the existing canonical model
   events: text to `ModelTextDelta`, thought summaries to
   `ModelReasoningDelta`, function calls to `ModelToolCall`, lifecycle calls to
   `ModelBackendToolStarted`/`ModelBackendToolCompleted`, usage to
   `ModelUsage`, and terminal interaction status to `ModelCompleted`. No
   Gemini-specific domain event family is introduced.
4. The adapter preserves the response's bounded JSON-safe step sequence in one
   immutable `PreservedContextItem`. It retains thought signatures, function
   call IDs, `call_id`, function results, Google Search steps, URL Context
   steps, and provider signatures. Native steps replay only when provider
   profile, service, protocol, model, and context affinity match exactly;
   otherwise the opaque item is ignored and the standard projection is used.
5. Google Search is represented by the explicit `google_search` builtin and
   URL Context by `url_context`. Effective capability is the fail-closed
   intersection of service/protocol/model metadata, adapter evidence, and
   configuration. Unknown models do not receive hosted capability. Search and
   URL Context are not implied by one another.
6. The catalog and adapter expose
   `MIXED_HOSTED_AND_CLIENT_TOOLS` only for the documented Gemini 3-style
   model set. Inline search is available only when hosted search is supported
   and any simultaneously registered client tools are covered by that mixed
   capability. Otherwise AUTO selects an executable WEB_SEARCH sidecar when
   one exists; explicit INLINE fails closed.
7. The Gemini WEB_SEARCH sidecar sends only the bounded redacted query and
   policy prompt. It uses `tool_choice` with `allowed_tools.mode = "any"` and
   `allowed_tools.tools = ["google_search"]` so a successful result must
   contain a real Google Search lifecycle. On Gemini 3-compatible profiles,
   configured `url_context` remains declared as a secondary built-in for the
   search-and-deepen flow, while the forced choice still requires the search
   call. A terminal answer without a paired successful search result becomes
   `SEARCH_PROVIDER_DID_NOT_SEARCH`.
8. Google Search and URL Context lifecycle results are not evidence by
   themselves. Search citations come from structured model-output
   `url_citation` annotations (`url`, `title`, `start_index`, `end_index`).
   URL Context annotations use the same canonical source/citation projection;
   retrieval status, including `unsafe`, remains provider-native diagnostic
   state and never becomes trusted fetch evidence. `search_suggestions` HTML is
   ignored by the canonical extractor and is never rendered as TUI evidence.
9. Function-result continuation uses the canonical `function_result` input
   with the original `call_id`, function name, and bounded JSON/text result.
   Malformed streamed argument JSON, invalid step shapes, unsupported tool
   combinations, failed/unsafe/incomplete interactions, observer failures,
   provider errors, timeouts, and cancellation fail at the existing provider
   boundary. Cancellation is allowed to close the HTTP stream and is not
   converted into failover-triggering provider text.

## Consequences

- Gemini can be selected as MAIN for inline Google Search or as the independent
  WEB_SEARCH sidecar for DeepSeek, OpenAI-compatible, Anthropic, or xAI MAIN
  profiles through the existing `WebSearchService` and `ToolExecutor` path.
- The legacy Gemini behavior remains isolated and regression-testable. Native
  Interactions state is bounded and provider-private; application code sees
  only canonical model events, capabilities, routes, and web-search results.
- URL Context is a hosted Gemini capability only. There is no public local
  `web_fetch` tool, arbitrary browser fetcher, or provider-independent fetch
  service in this slice.

## Non-goals

- No server-side durable Interaction ownership, `previous_interaction_id`, or
  background interaction mode.
- No local Web Fetch, Browser, Code Execution, Maps, File Search, Computer Use,
  MCP server, Deep Research, or new China provider.
- No large settings/TUI redesign and no silent migration of managed or native
  `gemini-generate-content` profiles. Interactions remains an explicit
  protocol option for this increment.

## Evidence

- [Gemini API versions](https://ai.google.dev/gemini-api/docs/api-versions)
- [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Gemini Interactions API reference](https://ai.google.dev/api/interactions-api-v1)
- [Migration and streaming guide](https://ai.google.dev/gemini-api/docs/migrate-to-interactions)
- [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
- [URL Context](https://ai.google.dev/gemini-api/docs/url-context)
- [Built-in and custom tool combinations](https://ai.google.dev/gemini-api/docs/tool-combination)
