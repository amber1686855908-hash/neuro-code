# ADR 0009 — xAI hosted tools and lifecycle ownership

[简体中文](../../zh-CN/adr/0009-xai-hosted-tools.md) · **English**

## Status

Accepted.

Amended by [ADR 0010](0010-provider-profiles-and-cc-switch.md): configuration
now uses an `openai-responses` profile with `dialect = "xai"`; hosted-tool
ownership and lifecycle rules remain unchanged.

## Context

xAI Responses can execute web search, X search, and code interpretation during
model inference. These hosted tools differ from Neuro Code function tools:
xAI owns their execution and returns backend output items, whereas local tools
must pass through the permission manager and a workspace-scoped executor. A
shared lifecycle would incorrectly imply that local policy can approve, deny,
or reproduce a provider-side effect.

The pinned Rust baseline models hosted tools separately, lets a hosted tool win
over a same-named function, and normalizes streamed web/X progress into backend
tool events. Current xAI documentation also exposes `code_interpreter` and
detailed include selectors for web sources and code outputs.

## Decision

Add an optional, provider-specific configuration field:

```toml
[provider.default]
kind = "xai-responses"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

The array is ordered, duplicate-free, and limited to those three exact names.
Non-empty use with another provider kind fails configuration loading. The xAI
adapter emits each configured item as a native Responses tool and then appends
local function schemas whose names do not collide. A configured hosted tool
wins a collision. Web search requests include
`web_search_call.action.sources`; code interpreter requests include
`code_interpreter_call.outputs`; encrypted reasoning remains included.

Add provider-domain `ModelBackendToolStarted` and
`ModelBackendToolCompleted` events and runtime-level
`backend_tool_started` / `backend_tool_completed` events. Their public payload
contains only the provider call ID and canonical tool name. They never enter
the local permission, registry, execution, or tool-result-message path.
Provider-native terminal output remains the durable result under ADR 0008.

The adapter recognizes native web/X/code progress events, xAI custom-tool input
events, and generic output-item added/done events. It deduplicates repeated
notifications by call ID and tool name. When a terminal response contains a
backend output without streamed lifecycle events, it synthesizes one ordered
start/complete pair so JSON, JSONL, session events, and future UIs receive a
coherent audit trail.

## Consequences

Users can opt into xAI-hosted research and code execution without adding
temporary client tools or confusing provider effects with local workspace
effects. The same normalized runtime events work for immediate progress and
terminal-only fixtures, while complete native outputs continue to survive
SQLite and provider-affine replay.

Hosted tools may incur provider charges in addition to model tokens, so they
remain disabled by default and inspection exposes only their names. Domain,
X-handle, date, image-understanding, and other advanced filters are deferred to
a typed configuration design. A credential-gated live xAI fixture remains
pending; protocol behavior is covered with mock SSE and pinned Rust evidence.

## References

- [xAI tools overview](https://docs.x.ai/developers/tools/overview)
- [xAI web search](https://docs.x.ai/developers/tools/web-search)
- [xAI X search](https://docs.x.ai/developers/tools/x-search)
- [xAI streaming and synchronous tools](https://docs.x.ai/developers/tools/streaming-and-sync)
- [xAI pricing](https://docs.x.ai/developers/pricing)
