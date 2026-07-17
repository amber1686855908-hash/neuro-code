# ADR 0007 — Provider-affine preserved-context replay

[简体中文](../../zh-CN/adr/0007-provider-affine-context-replay.md) · **English**

## Status

Accepted.

## Context

The Rust session importer and SQLite store preserve `Reasoning` and
`BackendToolCall` items in their original order, but resume previously loaded
only the ordinary-message projection into `AgentRuntime`. The data therefore
survived import and export without reaching any later model request.

The payloads are not provider-neutral. In the pinned Rust source, Responses API
reasoning and backend tools round-trip as native input items, while the legacy
Chat Completions conversion folds visible reasoning onto the following
assistant and replaces backend tools with human-readable assistant summaries.
xAI likewise documents encrypted reasoning as opaque state that can be sent
back for conversation continuity and says provider-generated encrypted content
is meaningful only to xAI's API. Sending such payloads to a different provider,
gateway, or model family is therefore unsafe.

## Decision

The model port accepts a `ModelContext` containing the complete ordered
`SessionItem` sequence plus the source session's provider and model. CLI resume
loads this canonical sequence; `AgentRuntime` appends new messages to it and
passes it unchanged into every model step. Application result views retain a
separate ordinary-message projection.

Adapters own the final projection. Anthropic, Gemini, and non-affine
OpenAI-compatible targets use ordinary messages only. The Chat Completions
adapter enables imported visible-context replay only when every condition is
true:

- the source provider marker is `upstream-rust-import`;
- the target URL uses HTTPS with no non-default port, URL credentials, query,
  or fragment, and its exact hostname is `api.x.ai`.

For an affine request, consecutive visible reasoning items are folded onto the
following assistant in source order. Backend web search, X search, and code
interpreter calls become bounded human-readable assistant summaries and do not
break the reasoning fold. An intervening system, user, or tool result clears
pending reasoning. Orphaned and malformed items are omitted.

Encrypted reasoning, raw backend-tool payloads, IDs, status fields, and outputs
are never sent through Chat Completions. Exact native replay is deferred to a
dedicated Responses API adapter, now specified by
[ADR 0008](0008-xai-responses-native-replay.md). Custom gateways are non-affine
by default because endpoint ownership cannot be proven locally.

## Consequences

Imported upstream sessions can recover useful visible context when resumed against
the official xAI Chat endpoint without exposing opaque provider state to
DeepSeek, Anthropic, Gemini, lookalike hosts, insecure endpoints, or custom
gateways. SQLite append-only prefix protection and schema-v2 export remain
unchanged.

This fallback improves semantic continuity but does not claim byte-stable
Responses replay or full prompt-cache parity. The separate `xai-responses`
adapter now covers local stateless replay of encrypted reasoning and supported
server-side-tool items; stateful response IDs and compaction items remain later
work.

## References

- [xAI reasoning and encrypted content](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [xAI multi-turn prompt caching](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/multi-turn)
- [xAI context compaction](https://docs.x.ai/developers/advanced-api-usage/context-compaction)
