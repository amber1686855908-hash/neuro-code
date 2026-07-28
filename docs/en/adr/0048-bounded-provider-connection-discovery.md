# ADR 0048: Bounded provider connection discovery

**English** · [简体中文](../../zh-CN/adr/0048-bounded-provider-connection-discovery.md)

Status: accepted

## Context

Local provider validation can prove that a profile, proxy policy, and endpoint
shape are structurally valid, but it cannot distinguish a bad API key from an
incorrect base URL or wire protocol. The first real conversation previously
became that test and could fail only after the user left Settings. A remote
model catalog can validate the route and offer current identifiers, but it is
an untrusted, credential-bearing network boundary and is not uniformly
implemented by compatible servers.

## Decision

- Add a `ProviderCatalog` port and an HTTPX adapter. Discovery is explicitly
  user-triggered; saving a profile remains offline and never starts discovery.
- Reuse the draft profile's resolved `HttpClientPolicy`. Send credentials only
  in protocol-native headers, including Gemini's `x-goog-api-key`, never in a
  query string, UI message, object representation, or persisted catalog.
- Use read-only model-list requests for OpenAI-compatible/Responses, Anthropic,
  and Gemini profiles. Do not send a prompt or start model generation.
- Bound a response to one MiB and the rendered catalog to 200 unique model IDs.
  Keep results only in the current settings screen and retain manual model
  entry as the compatibility fallback.
- Classify authentication, endpoint/protocol, timeout, rate-limit, server,
  proxy, network, oversized, and malformed-response failures. Never read or
  render an HTTP error response body; redact configured credentials and proxy
  values from transport errors.
- Keep the profile unsaved during testing. Selecting a discovered model only
  updates the draft; **Save and use** remains the sole persistence action.

## Consequences

Users can diagnose common 401/404/proxy failures before starting a conversation
and select a returned model without copying an identifier. Discovery does not
incur generation charges and cannot mutate provider state through Neuro Code.
A successful catalog request proves only that this credential can reach that
catalog; it does not guarantee that every listed model accepts the account or
that a custom compatible server implements `/models`. Such servers may keep a
manual identifier and save without passing discovery. Durable/offline catalog
caching, provider-specific pagination beyond the bounded first response, and
platform-keychain storage remain future work.
