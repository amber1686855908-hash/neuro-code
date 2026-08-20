# ADR 0123: Multi-model platform provider compatibility

- Status: Accepted; P3B vertical slice
- Date: 2026-08-20
- Scope: Volcengine Ark, Baidu Qianfan, Alibaba Model Studio / Bailian, and Tencent TokenHub

## Context

P3A added direct Kimi, GLM, and MiniMax services that mostly expose one
OpenAI-compatible Chat route. P3B has a different shape: an inference service
can host several publishers, a model can have protocol-specific restrictions,
and a route can change with region, workspace, or billing endpoint class.

The official pages below were reread for this slice. They are the evidence
boundary for the current matrix; model catalogs, endpoint classes, plan rules,
and protocol compatibility can change and must be refreshed before promoting
an `UNKNOWN` fact.

### Official evidence matrix

| Service | Official service and inference endpoints | Authentication and discovery | Protocol evidence used here |
|---|---|---|---|
| Volcengine Ark | [Ark quick start](https://www.volcengine.com/docs/82379/1795150) documents `https://ark.cn-beijing.volces.com/api/v3` and Responses requests; the [Ark API overview](https://api.volcengine.com/api-docs/view/overview?serviceCode=ark) documents Chat `/chat/completions` and separates control-plane APIs | API key in the official examples; no stable inference `/models` contract was accepted for this slice, so the UI uses a bounded versioned list and manual entry | OpenAI-compatible Chat and Responses are supported for the listed Ark model descriptors. Anthropic Messages is explicitly unsupported. Function calling is portable; Ark built-in tools are out of scope. See the [function-calling/Responses documentation](https://www.volcengine.com/docs/82379/1958524?lang=zh) |
| Baidu Qianfan | [OpenAI-compatible API](https://cloud.baidu.com/doc/qianfan/s/Hmh4suq26) uses `https://qianfan.baidubce.com/v2`; the [V2 compatibility page](https://cloud.baidu.com/doc/qianfan/s/qmh4sv5vi) describes the shared route | API key/Bearer for OpenAI-compatible requests; [model listing](https://cloud.baidu.com/doc/qianfan-api/s/Dmba8k71y) documents `GET /v2/models`. The [Anthropic-compatible page](https://cloud.baidu.com/doc/qianfan-docs/s/6mh3e6gjp) documents `/anthropic` and `x-api-key` Messages wire compatibility, but not an Anthropic model-list endpoint | Chat is the broad portable route. Responses is `SUPPORTED` only for the official model list in [Responses documentation](https://cloud.baidu.com/doc/qianfan-docs/s/4mi400l1m). Anthropic is manual-only model discovery and Messages wire compatibility only; no Anthropic server-tool capability is inherited |
| Alibaba Model Studio / Bailian | [Base URL and region guidance](https://www.alibabacloud.com/help/en/model-studio/base-url) documents regional, shared, and workspace-scoped endpoints. The [Model Studio overview](https://www.alibabacloud.com/help/en/model-studio/what-is-model-studio) documents OpenAI-compatible access | API keys are region-specific. OpenAI Chat/Responses use the compatible-mode routes; [Anthropic Messages](https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages) uses the Anthropic route and explicitly says `/v1/models` is not available, so model entry is manual there | Qwen descriptors are supported for Chat, Responses, and Anthropic Messages. Third-party descriptors are supported for Chat and Anthropic; Responses remains `UNKNOWN`. Hosted `web_search`, `web_extractor`, and code-interpreter behavior is not claimed |
| Tencent TokenHub | [API documentation](https://cloud.tencent.com/document/product/1823/130078) documents Guangzhou `https://tokenhub.tencentmaas.com` and Singapore `https://tokenhub-intl.tencentmaas.com`, including `/v1/models`. [Compatibility overview](https://cloud.tencent.com/document/product/1823/130079) documents Chat, Responses, and Anthropic routes | Bearer auth for Chat/Responses; `x-api-key` for Anthropic Messages; the inference `/v1/models` endpoint is the bounded remote discovery path | Model descriptors distinguish native Responses (`hy3`), compatibility-converted Responses (listed GLM/Kimi/DeepSeek routes), unsupported Responses (`hy-mt2-pro`), and unknown Responses (listed Qwen routes). The [Responses conversion documentation](https://cloud.tencent.com/document/product/1823/133813) does not turn built-in tools into supported Neuro Code hosted tools |

The matrix is intentionally narrower than each vendor's feature set. A vendor
feature is not an executable Neuro Code capability until the selected service,
model descriptor, protocol, and existing adapter all agree.

## Decision

### Service is not publisher

`ProviderServiceDescriptor` represents an inference service and owns no
credential or runtime adapter. `ProviderModelDescriptor` may carry optional
publisher metadata when the official model identity makes it clear. For
example, a DeepSeek model exposed by TokenHub, Bailian, or Qianfan carries a
DeepSeek hint, but dispatch is still selected by `service_id`, profile
protocol, dialect, and explicit base URL. The four platform services have no
service-level publisher assignment.

### Model-specific protocol matrix

Each listed model stores a three-state protocol fact:

| Service/model evidence family | `openai-chat` | `openai-responses` | `anthropic-messages` | Response mode |
|---|---:|---:|---:|---|
| Ark `doubao-seed-2-0-lite-260215`, `doubao-seed-1-6-250615` | `SUPPORTED` | `SUPPORTED` | `UNSUPPORTED` | native |
| Qianfan official Responses models | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | Qianfan-specific wire paths |
| Bailian Qwen descriptors | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | Model Studio wire paths |
| Bailian third-party descriptors | `SUPPORTED` | `UNKNOWN` | `SUPPORTED` | Responses is not assumed |
| TokenHub `hy3`, `hy3-preview` | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | native |
| TokenHub listed GLM/Kimi/DeepSeek models | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | compatibility-converted |
| TokenHub `hy-mt2-pro` | `SUPPORTED` | `UNSUPPORTED` | `SUPPORTED` | no Responses route |
| TokenHub listed Qwen models | `SUPPORTED` | `UNKNOWN` | `SUPPORTED` | no Responses claim |

`UNKNOWN` is retained as an explicit manual/configuration state. It is never
rendered as confirmed compatibility, and `UNSUPPORTED` is rejected by both
`ProviderProfile` and `ManagedProviderProfile`. Protocol capability facts are
not inferred from a publisher name or from the existence of a remote model ID.

### Shared adapters and compatibility firewall

The factory continues to use:

- `OpenAICompatibleProvider` for `openai-chat`;
- `OpenAIResponsesProvider` for `openai-responses`;
- `AnthropicProvider` for `anthropic-messages`.

No `ark.py`, `qianfan.py`, `bailian.py`, or `tokenhub.py` runtime adapter is
added. Service-level catalog strategy, endpoint metadata, model matrix, and
bounded protocol hints are held in the provider-service catalog. The adapter
does not dispatch on publisher and does not inherit OpenAI hosted tools merely
because the wire shape is compatible. P3B adds no Ark search, Qianfan native
search, Bailian hosted tools, or TokenHub hosted tools; provider-neutral
`web_search` and P2 local safe `web_fetch` remain the only web composition.

### Endpoint and profile identity

`ProviderEndpointVariant` records non-secret region, workspace scope, billing
plan label, usage scope, and the documented base URL for each protocol. The
TUI selection order is service → endpoint variant → protocol → model. Selecting
an endpoint only supplies a default URL; an explicit user URL remains an
override and is never rewritten by an adapter. The UI also offers Auto /
Recommended as a convenience choice; before connection, save, or runtime use,
it resolves to a concrete model-supported protocol and is never persisted as an
ambiguous protocol.

The current variants are:

| Service | Variants | Deliberately excluded |
|---|---|---|
| Ark | Beijing inference endpoint | control-plane model management |
| Qianfan | mainland inference endpoint | control-plane credential lifecycle |
| Bailian | Beijing pay-as-you-go, Singapore workspace, Singapore shared, US Virginia pay-as-you-go | trial, Token Plan, and Coding Plan activation/management |
| TokenHub | Guangzhou, Singapore | account or billing management |

The canonical base URL, protocol, model, and service ID participate in native
context affinity. Consequently the same publisher/model through direct
DeepSeek, TokenHub, Bailian, and Qianfan cannot replay provider-native state
between profiles. Region/workspace labels are metadata; endpoint identity is
the URL selected in the profile.

### Discovery and settings

Remote discovery is read-only and bounded. Qianfan OpenAI-compatible and
TokenHub routes use their documented inference model paths. Qianfan Anthropic
and Bailian Anthropic are manual-only because their official compatibility
pages do not document a safe `/v1/models` endpoint. Ark uses a
versioned bounded list/manual entry rather than inventing a control-plane
credential flow. A remote model list proves availability only; it does not
elevate every protocol or capability fact.

The existing settings store keeps credentials separate from normal metadata,
uses the existing redaction and `HttpClientPolicy`, and permits multiple
profiles for one service. The TUI does not contain platform runtime branches;
it consumes catalog labels, variants, protocol facts, and hints.

### Portable capability semantics

The shared descriptors currently expose only conservative portable function,
reasoning, and model-specific vision facts where the official evidence table
supports them. Hosted Web Search/Web Fetch, vendor native search, structured
output parity, parallel-tool guarantees, and plan-specific usage behavior
remain `UNKNOWN`, unsupported, or outside this slice unless separately
implemented by an existing adapter. Reasoning is normalized by the selected
wire adapter; a publisher hint never chooses a reasoning parser. TokenHub's
compatibility-converted Responses mode is descriptive metadata and does not
pretend to be native OpenAI item parity.

## Consequences

Positive consequences:

- four new inference services use one tested protocol/runtime surface;
- model/protocol restrictions are centralized and testable;
- endpoint, workspace, and billing identity cannot silently collapse into one
  native context;
- local tools, sidecar web search, local safe fetch, permissions, proxy policy,
  redaction, and failover remain provider-neutral.

Known limitations:

- official model catalogs and capability facts are versioned evidence, not a
  live billing or control-plane inventory;
- Ark and Bailian Anthropic discovery are intentionally bounded/manual;
- no hosted vendor tools, control-plane SDK, cost estimator, billing manager,
  activation flow, MCP, Browser, LSP, or plan-bypass behavior is included;
- live tests require paid/network opt-in and report missing credentials as
  `SKIPPED`, never as provider certification.

## Verification

Offline contract coverage includes the protocol matrix, unsupported/unknown
profile validation, protocol-specific model discovery headers and paths,
shared factory adapter selection, fake-platform extensibility, TUI endpoint and
protocol controls, same-publisher affinity separation, and pre-output failover
contracts. The four live files are:

| Service | Test | Credential | Required opt-in |
|---|---|---|---|
| Ark | `tests/live/test_ark_live.py` | `ARK_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_ARK=1` |
| Qianfan | `tests/live/test_qianfan_live.py` | `QIANFAN_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_QIANFAN=1` |
| Bailian | `tests/live/test_bailian_live.py` | `DASHSCOPE_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_BAILIAN=1` |
| TokenHub | `tests/live/test_tokenhub_live.py` | `TOKENHUB_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_TOKENHUB=1` |

Each live file supports model, protocol, base-URL, and existing proxy
overrides, and exercises text plus a local function-tool continuation. No
unimplemented hosted tool is tested, and exception reporting does not include
credential values or response bodies.
