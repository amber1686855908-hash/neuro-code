# ADR 0121: Local safe Web Fetch

- Status: Accepted; P2 vertical slice
- Date: 2026-08-19
- Scope: public HTTP(S) text fetch, SSRF defense, bounded extraction, and main-facing routing

## Context

Hosted Web Search, Anthropic hosted Web Fetch, and Gemini URL Context are
provider-owned capabilities. They must remain separate from a local client
that owns DNS, TCP, TLS, HTTP, redirects, body limits, and extraction. The
application also needs a small model-visible fetch capability without allowing
an arbitrary URL to become a private-network or credential exfiltration
primitive.

## Decision

1. The canonical `WebFetchRequest` contains one absolute URL and an optional
   bounded `max_chars`; `WebFetchResult` exposes only requested/final URL,
   title, media type, clean text, status, truncation, and provenance. Raw
   binary bytes, complete HTML, response headers, cookies, and authentication
   are never part of the application or tool result.
2. The local implementation is `LocalWebFetcher` behind
   `WebFetchService`. It uses `aiohttp` with a custom pinned resolver,
   `trust_env = false`, no proxy, a dummy cookie jar, a fixed User-Agent and
   Accept policy, GET only, TLS certificate verification, and one connector
   per validated hop. Provider HTTP policy is not reused.
3. `is_public_destination` is the single address decision. Literal and DNS
   A/AAAA results reject loopback, private, link-local, multicast, reserved,
   unspecified, shared, IPv4-mapped-private, and other non-global addresses.
   A DNS lookup rejects the whole destination if any candidate is unsafe. The
   validated result is pinned into the resolver actually used by the TCP
   connector, preventing a second hostname lookup from creating a DNS
   rebinding/TOCTOU gap.
4. Only `http` and `https` are accepted, with no userinfo and only the
   scheme-matching default ports 80 and 443. Fragments are removed before the
   request. Redirects are manual, limited to five, and revalidate every hop;
   HTTPS-to-HTTP downgrade, unsafe destinations, unsupported schemes, and
   malformed targets fail closed. No credential-bearing header is forwarded
   across hosts because the local client sends no credentials, cookies,
   Authorization, Referer, or proxy headers at all.
5. Responses are bounded before and during streaming. Header count/line/field
   limits are set at the client boundary, `Content-Length` is an early guard,
   and the streamed post-decompression byte count is the authoritative body
   limit. Only HTML/XHTML, plain text, Markdown, JSON, and XML are allowed;
   binary, image, audio, video, PDF, octet-stream, and unknown media are
   rejected. Missing types use conservative text/JSON/HTML sniffing.
6. HTML is parsed with a dedicated non-executing standard-library
   `HTMLParser` boundary that drops scripts/styles and emits bounded clean
   Markdown. JSON and XML are returned as bounded decoded text without parsing
   or entity expansion, so no XXE-capable XML operation is introduced. No
   JavaScript, browser, PDF, crawling, cache, robots, auth, cookies, local
   network, or non-HTTP protocol is supported.
7. A configured redaction value in an outbound initial or redirected URL is a
   `SECRET_IN_URL` failure before DNS/HTTP. Fetched titles, content, metadata,
   tool results, lifecycle projections, and generic TUI/JSON/JSONL boundaries
   are redacted again. The rendered result begins with `[UNTRUSTED WEB
   CONTENT]`; this is a provenance boundary, not a claim that prompt
   injection has been eliminated.
8. `[web_fetch] mode` defaults to `disabled` and accepts `disabled`, `local`,
   `inline`, and `auto`. `disabled` exposes neither local nor MAIN hosted
   fetch; `local` strips MAIN hosted fetch and registers only the local tool;
   `inline` requires an explicitly supported MAIN hosted capability and fails
   closed otherwise; `auto` keeps explicit MAIN hosted fetch when supported and
   otherwise uses the local tool. The capability is never enabled merely
   because a provider happens to advertise a related tool. Hosted fetch used
   internally by a search sidecar remains provider-owned and is not exposed as
   a second main-facing tool.
9. `web_fetch` is registered only after mode resolution and runs through the
   existing `ToolRegistry` → `ToolExecutor` → permission path. It is marked
   side-effecting so default headless execution denies an unmatched network
   read, interactive callers can approve it, and explicit permission modes or
   rules remain the authority. Cancellation propagates as `CancelledError`
   after the request/session boundary is closed and is not converted into an
   ordinary tool failure.

## Consequences

- DeepSeek/OpenAI-compatible MAIN can use local fetch when the user explicitly
  selects `local` or `auto`; no DeepSeek-specific Web Fetch provider is added.
- The fetch contract is provider-neutral and can later accept a trusted proxy
  seam without inheriting the current provider proxy environment by accident.
- The first version has no cache and does not automatically fetch every search
  result. Search-to-fetch remains an explicit model/application decision.
- TUI, JSON, and JSONL receive the same bounded `ToolResult` projection and do
  not learn HTTP or HTML semantics.

## Verification

Portable tests cover URL normalization, schemes/ports/userinfo, public
destination classes, all-candidate DNS validation, resolver pin reuse,
redirects and downgrade/secret rejection, streaming and decompressed bounds,
MIME/sniffing/charset, HTML extraction, redaction, untrusted rendering,
permission behavior, cancellation, configuration, and composition ownership.
An opt-in live smoke test is reserved for `NEURO_CODE_LIVE_WEB_FETCH=1` and
requires no provider key.
