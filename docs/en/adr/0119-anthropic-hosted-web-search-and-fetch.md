# ADR 0119: Anthropic hosted Web Search and Web Fetch

- Status: Accepted; P1.1 vertical slice
- Date: 2026-08-19
- Scope: Anthropic Messages hosted `web_search` and `web_fetch`

## Context

P1.0.5 established the provider-neutral hosted Web Search contract and the
OpenAI Responses/xAI route. Anthropic Messages exposes Web Search and Web
Fetch as server tools whose request and result blocks must stay in the native
assistant context. Treating them as local function tools would lose the
server-tool result, citations, and continuation state; treating model text as
proof of execution would also allow a pure answer to masquerade as a search.

## Decision

1. The Anthropic adapter is the sole owner of the server-tool wire protocol.
   It emits the existing `ModelBackendToolStarted` and
   `ModelBackendToolCompleted` lifecycle events and never adds an Anthropic
   event family to the application port.
2. Configured server tools use the current versioned definitions
   `web_search_20260318` and `web_fetch_20260318`, with
   `allowed_callers = ["direct"]`. Web Fetch enables citations, has bounded
   uses/content, and is never registered as a local `ToolDefinition`.
3. Anthropic hosted capabilities are fail-closed. The catalog records the
   explicitly documented model families; an unknown model or an unconfigured
   builtin remains `UNKNOWN` and cannot activate a hosted route.
4. Each response containing server-tool blocks is retained as one bounded
   `PreservedContextItem` with provider/model affinity. The item is projected
   back as the native assistant content and suppresses the duplicate ordinary
   assistant projection. Opaque encrypted fields may remain in this private
   continuation item, but never enter canonical `WebSearchResult`, normal
   text, logs, or UI evidence.
5. `pause_turn` is continued inside one provider stream, preserving the exact
   server content and tool definitions. The continuation count is capped at
   three. A mixed server/client response returns the client `ModelToolCall`
   while retaining the server content; a later request can complete the
   server lifecycle from the preserved native item without replaying the
   ordinary assistant message twice.
6. The sidecar route uses the existing `HostedWebSearch` boundary. It forces
   Anthropic `tool_choice = {"type": "tool", "name": "web_search"}`, sends
   only a bounded redacted query and canonical domain filters, requires a
   paired successful server search result, and maps structured result blocks
   and citation locations into the existing source/citation contracts.
   Web Fetch remains an Anthropic server capability available during that
   sidecar turn; no local HTTP fetcher is introduced.
7. Provider errors, server-result errors, cancellation, and observer failures
   remain errors at the provider boundary. The sidecar maps them to the
   existing stable `WebSearchErrorCode` vocabulary and preserves cancellation.

## Consequences

- MAIN can use Anthropic hosted search inline when its profile explicitly
  enables it; WEB_SEARCH can use the same provider through independent route
  resolution and fallback.
- Search sources and citations are canonical, bounded, domain-filtered, and
  safe to render as untrusted evidence. Native encrypted continuation state
  is not a canonical result format.
- The adapter supports current versioned server-tool schemas only. A future
  Anthropic tool-version change requires an explicit adapter/catalog update and
  fixture coverage.

## Non-goals

- No local `web_search` or `web_fetch` tool is added.
- No browser, arbitrary URL fetcher, code execution, or Anthropic dynamic
  filtering is enabled by this decision.
- No new provider, Gemini hosted capability, or UI redesign is included.

## Evidence

- [Anthropic Web Search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
- [Anthropic Web Fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)
- [Anthropic server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)
