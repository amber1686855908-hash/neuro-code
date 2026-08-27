# ADR 0122: China direct provider compatibility expansion

- Status: Accepted; P3A vertical slice
- Date: 2026-08-20
- Scope: direct Kimi/Moonshot, GLM/Zhipu, and MiniMax OpenAI-compatible Chat APIs

## Context

Neuro Code already has a canonical `openai-chat` adapter and a provider-service
catalog. Adding three vendor-specific full adapters would duplicate streaming,
tool-loop, usage, redaction, proxy, and failover behavior. The three direct
APIs are close enough to Chat Completions to share that path, but their current
model, reasoning, catalog, and tool-choice contracts are not identical.

This ADR records the current official evidence used for P3A. Vendor model
catalogs and request contracts can change, so a future refresh must reread the
linked official pages before changing a capability from `unknown` to
`supported`.

## Decision

### Service, protocol, and catalog

| Service | Neuro Code profile | Direct base URL | Dialect | Catalog | Current official evidence |
|---|---|---|---|---|---|
| Kimi / Moonshot | `service_id = "kimi"` | `https://api.moonshot.ai/v1` | `kimi` | `GET /v1/models` with a small static fallback | [API overview](https://platform.kimi.ai/docs/api/overview.md), [models](https://platform.kimi.ai/docs/models.md) |
| GLM / Zhipu | `service_id = "glm"` | `https://open.bigmodel.cn/api/paas/v4` | `glm` | bounded official static list; no invented `/models` request | [OpenAI compatibility](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction.md), [model overview](https://docs.bigmodel.cn/cn/guide/start/model-overview.md) |
| MiniMax | `service_id = "minimax"` | `https://api.minimaxi.com/v1` | `minimax` | `GET /v1/models` with a small static fallback | [model list](https://platform.minimaxi.com/docs/api-reference/models/openai/list-models.md), [OpenAI API](https://platform.minimaxi.com/docs/api-reference/text-openai-api.md) |

All three use Bearer API-key authentication through the existing profile
credential port. The profile base URL is normalized and the adapter appends
`/chat/completions`; no vendor key is stored in service metadata.

### Shared adapter and capability evidence

P3A uses `OpenAICompatibleProvider` for all three services. A capability is
executable only when the intersection of upstream official evidence, the exact
model descriptor, and the trusted adapter implementation supports it. The
current descriptors conservatively expose:

- `FUNCTION_TOOLS`, `REASONING`, and `PROMPT_CACHE` for current text models
  whose official pages document those behaviors;
- `VISION` only for the current model IDs documented as multimodal, not as a
  service-wide assumption;
- `HOSTED_WEB_SEARCH`, `HOSTED_WEB_FETCH`, structured `response_format`, and
  mixed hosted/client-tool behavior as `unknown` or unsupported in this round.

The adapter does not turn an unknown upstream capability into support. Error
normalization remains the existing bounded, redacted `ProviderError` contract;
no Kimi-, GLM-, or MiniMax-specific runtime exception hierarchy is introduced.

### Reasoning and replay

Kimi uses the current model families rather than the discontinued `kimi-latest`
alias. K3 always reasons and accepts the application-owned effort mapping
`low → low`, `medium/high → high`, and `xhigh/ultracode → max`; this is the
provider-wire compatibility projection and does not implement Ultracode branch
selection. K2.7 and K2.6
send thinking enabled with `keep = all`; K2.5 sends thinking enabled but is not
advertised as preserving reasoning content. The adapter preserves the complete
assistant `reasoning_content` when the model contract requires it. For the
current K2.6 thinking contract, only `tool_choice = auto` or `none` is
accepted; `required` and specific function choices fail closed before the
request is sent. Thinking is never silently disabled. These rules follow the
[Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart),
[API overview](https://platform.kimi.ai/docs/api/overview), and [tool-use
guide](https://platform.kimi.ai/docs/api/tool-use).

GLM current thinking models send `thinking.type = enabled` and
`clear_thinking = false`, and preserve complete `reasoning_content` across tool
rounds. The verified effort mapping is local to the dialect: GLM-5.3 accepts
the mapped low/high/max values; GLM-5.2 maps low/medium/high to high and
xhigh to max. Current official function-calling evidence only permits
`tool_choice = auto`, so required or specific choices fail closed. The
optional `tool_stream` parameter is not enabled by default. See the official
[thinking](https://docs.bigmodel.cn/cn/guide/capabilities/thinking.md),
[thinking mode](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode.md),
[function calling](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling.md),
and [stream-tool](https://docs.bigmodel.cn/cn/guide/capabilities/stream-tool.md)
guides.

MiniMax uses `max_completion_tokens` and `reasoning_split = true` on its
OpenAI-compatible request. The adapter captures streamed `reasoning_details`
without duplicating cumulative text, converts the bounded structured blocks
into a provider-neutral opaque `PreservedContextItem`, and replays them for
MiniMax only when the profile, model, endpoint, protocol, and context affinity
match. The runtime, Message domain object, and SQLite schema do not interpret
the MiniMax shape. The user-facing projection remains the ordinary assistant
`content`; private reasoning is not copied into the final response. This follows the official [OpenAI-compatible
API](https://platform.minimaxi.com/docs/api-reference/text-openai-api.md) and
[M3 function-call guide](https://platform.minimaxi.com/docs/guides/text-m3-function-call.md).
The provider's M3 adaptive-thinking behavior is not represented as a false
cross-provider parity claim; the application-owned effort remains a bounded
adapter-local mapping.

### Tools, usage, images, and errors

All three use the existing standard function schema, streamed `tool_calls`,
JSON argument accumulation, tool-result continuation, redaction, and
permission-owned local tools. No provider is given a native hosted web tool in
P3A. Usage is normalized only from fields actually returned: input,
completion, total/cache fields, including Kimi top-level cache fields, GLM
nested cache details, and MiniMax nested cache details. No cache hit or
reasoning usage is inferred when the provider does not report it.

Image content is sent through the existing OpenAI-compatible image-part
boundary only when the exact model descriptor has `VISION` support. The
implementation does not claim that every model under a service accepts images.

### Settings, credentials, proxy, and failover

The TUI exposes Kimi, GLM, and MiniMax as service presets. Kimi and MiniMax
run the remote model catalog request; GLM shows the bounded official static
list without pretending to validate credentials. Manual model entry remains
available in every case. Saving keeps ordinary profile metadata separate from
the API-key store and never renders the secret.

The existing environment, direct, and explicit named-proxy modes apply to all
three profiles. HTTP policy is constructed from the profile port, and error
details are redacted before reaching the TUI or tool boundary.

Failover remains pre-output and monotonic. Kimi → GLM and GLM → MiniMax use the
existing safe capability intersection before a candidate is selected. A
provider-specific reasoning representation is never treated as portable
native state: when a profile uses `native_context = "profile"`, its context
affinity includes profile, service, protocol, canonical endpoint, and model.
Cross-provider fallback can use the canonical provider-neutral projection, but
does not replay Kimi thinking state or MiniMax `reasoning_details` to another
vendor.

### Web integration

Kimi, GLM, and MiniMax remain ordinary MAIN Chat providers. The existing
composition resolves local `web_search` through the configured WEB_SEARCH
sidecar and registers P2 local `web_fetch` when its mode permits it. This
means all three can use the same permission-owned Web architecture without
claiming native Kimi `$web_search`, GLM hosted search, MiniMax MCP, or another
vendor-specific Web capability.

## Verification

Bounded fixtures cover Kimi text/reasoning/tool/usage/image projection, GLM
reasoning/streamed tool arguments/tool-result continuation/usage, and MiniMax
structured reasoning/tool/usage replay. Composition tests cover each China
provider as MAIN with local Web Search sidecar and local Web Fetch. The live
tests are separate and explicitly gated:

| Provider | Test | Credential | Additional opt-in |
|---|---|---|---|
| Kimi | `tests/live/test_kimi_live.py` | `MOONSHOT_API_KEY` or `KIMI_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` and `NEURO_CODE_RUN_LIVE_KIMI=1` |
| GLM | `tests/live/test_glm_live.py` | `ZHIPU_API_KEY` or `GLM_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` and `NEURO_CODE_RUN_LIVE_GLM=1` |
| MiniMax | `tests/live/test_minimax_live.py` | `MINIMAX_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` and `NEURO_CODE_RUN_LIVE_MINIMAX=1` |

Model IDs and base URLs have environment overrides. Live failures report only
the provider/status class and never echo credentials or response bodies.

## Consequences and limitations

- One shared adapter keeps tool-loop, proxy, redaction, failover, and context
  behavior consistent while keeping vendor quirks at the wire boundary.
- GLM model discovery is intentionally static until a stable official catalog
  endpoint is established.
- Native hosted Web Search/Web Fetch, MiniMax MCP, optional GLM `tool_stream`,
  provider-specific structured output, and full structured-output parity are
  not part of P3A.
- Live certification depends on explicit paid/network opt-in and credentials;
  an absent credential is a skip, not a claim of provider health.
- Current official model IDs and model-specific capabilities must be refreshed
  before adding defaults or promoting a capability.
