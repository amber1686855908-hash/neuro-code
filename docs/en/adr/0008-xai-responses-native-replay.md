# ADR 0008 — Native xAI Responses replay

[简体中文](../../zh-CN/adr/0008-xai-responses-native-replay.md) · **English**

## Status

Accepted.

Amended by [ADR 0010](0010-provider-profiles-and-cc-switch.md): the adapter is
now generic Responses with xAI as an optional dialect; this ADR still governs
the xAI-specific native replay behavior.

## Context

ADR 0007 permits a useful but lossy Chat Completions projection for imported
xAI context. Chat cannot safely carry encrypted reasoning or complete
server-side-tool items. The pinned Rust implementation instead sends ordered
reasoning and backend-tool siblings back through the Responses API and treats
the terminal response `output` array as the canonical model turn.

xAI documents the Responses API as its preferred REST interface. Encrypted
reasoning is returned only when requested with
`include: ["reasoning.encrypted_content"]` and can be sent back unchanged for
continuity. Function definitions are flat Responses tools, function results are
`function_call_output` input items, and streaming text uses
`response.output_text.delta`. Opaque content is meaningful only to xAI, so a
provider label or model name is insufficient evidence for replay.

## Decision

Add the explicit provider kind `xai-responses`. It uses `/v1/responses` with:

- `stream: true` and bounded `max_output_tokens`;
- `store: false`, because SQLite remains the canonical local history;
- `include: ["reasoning.encrypted_content"]`;
- concise reasoning summaries;
- flat function tool schemas.

Messages are projected in order to Responses message, function-call, and
function-call-output input items. Validated user images use native
`input_image` blocks; unsupported references use the existing visible
placeholder. Assistant display-only `reasoning_content` is not duplicated when
a native reasoning sibling exists.

Native preserved items are admitted only when all affinity checks pass:

- the endpoint uses HTTPS with no non-default port, URL credentials, query, or
  fragment, and its exact hostname is `api.x.ai`;
- the source is either a pinned Rust import (`upstream-rust-import`) or a previous
  `xai-responses` session.

Reasoning inputs retain ID, summary, visible content, and encrypted content but
strip output-only `status`. Supported web-search, X-search/custom-tool, and code
interpreter items retain their native JSON and relative order. Malformed,
unknown, mismatched, or non-affine preserved items are omitted; ordinary
messages continue to work. Custom endpoints never persist opaque terminal
items, preventing a later official-endpoint resume from laundering untrusted
state into xAI.

Streaming deltas remain the interactive event surface. The terminal
`response.completed` or `response.incomplete` object is authoritative for
function calls, usage, stop reason, canonical assistant text, and persisted
native output items. `ModelCompleted` carries the canonical text and ordered
`PreservedContextItem` values; `AgentRuntime` inserts those values before the
assistant and commits the complete sequence through `SessionStore`. If visible
reasoning arrived only in stream deltas, it repairs an encrypted-only reasoning
item or creates a synthetic visible reasoning sibling, matching the pinned Rust
fallback. HTTP, protocol, terminal, and function-argument failures are bounded
and credential-redacted.

## Consequences

Imported and newly generated official xAI context can now survive local
tool loops, SQLite round trips, process restarts, and later native Responses
requests without copying opaque state to DeepSeek, Anthropic, Gemini, custom
gateways, insecure URLs, or lookalike hosts. Terminal text rather than
potentially lossy SSE chunk reconstruction becomes the model-facing and
persistent truth, while UIs retain immediate deltas.

This slice intentionally does not use `previous_response_id`; server-side
storage is disabled and complete local context is resent. Compaction items,
MCP/file-search output preservation, retry policy, and a credential-gated live
xAI fixture remain subsequent slices. Built-in xAI tool configuration and
lifecycle ownership are specified separately in
[ADR 0009](0009-xai-hosted-tools.md).

## References

- [xAI Responses text generation](https://docs.x.ai/developers/model-capabilities/text/generate-text)
- [xAI reasoning and encrypted content](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [xAI function calling](https://docs.x.ai/developers/tools/function-calling)
- [xAI multi-turn prompt caching](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/multi-turn)
- [xAI context compaction](https://docs.x.ai/developers/advanced-api-usage/context-compaction)
