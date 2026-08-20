# ADR 0122：中国直连 Provider 兼容性扩展

- 状态：已接受；P3A 纵向切片
- 日期：2026-08-20
- 范围：Kimi/Moonshot、GLM/Zhipu、MiniMax 的直连 OpenAI-compatible Chat API

## 背景

Neuro Code 已有规范的 `openai-chat` adapter 与 Provider service catalog。若为三家
Provider 各复制一个完整 adapter，就会重复 streaming、tool loop、usage、redaction、
proxy、failover 等行为。三家的 direct API 足够接近 Chat Completions，可以共享这条
路径，但当前 model、reasoning、catalog 与 tool-choice contract 并不完全相同。

本 ADR 记录 P3A 使用的当前官方 evidence。Vendor model catalog 与 request contract
可能变化；未来刷新 capability 前必须重新阅读链接的官方页面，不能只凭旧 fixture 把
`unknown` 提升为 `supported`。

## 决策

### Service、protocol 与 catalog

| Service | Neuro Code profile | Direct base URL | Dialect | Catalog | 当前官方 evidence |
|---|---|---|---|---|---|
| Kimi / Moonshot | `service_id = "kimi"` | `https://api.moonshot.ai/v1` | `kimi` | `GET /v1/models`，附有小型 static fallback | [API overview](https://platform.kimi.ai/docs/api/overview.md)、[models](https://platform.kimi.ai/docs/models.md) |
| GLM / Zhipu | `service_id = "glm"` | `https://open.bigmodel.cn/api/paas/v4` | `glm` | 有界官方 static list；不虚构 `/models` 请求 | [OpenAI compatibility](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction.md)、[model overview](https://docs.bigmodel.cn/cn/guide/start/model-overview.md) |
| MiniMax | `service_id = "minimax"` | `https://api.minimaxi.com/v1` | `minimax` | `GET /v1/models`，附有小型 static fallback | [model list](https://platform.minimaxi.com/docs/api-reference/models/openai/list-models.md)、[OpenAI API](https://platform.minimaxi.com/docs/api-reference/text-openai-api.md) |

三家都通过现有 profile credential port 使用 Bearer API key。profile base URL 会被规范化，
adapter 追加 `/chat/completions`；service metadata 不保存 vendor key。

### 共享 adapter 与 capability evidence

P3A 三家都使用 `OpenAICompatibleProvider`。只有上游官方 evidence、exact model
descriptor 与 trusted adapter implementation 的交集，才可作为可执行 capability。当前
descriptor 保守暴露：

- 对官方页面明确说明的 current text model 暴露 `FUNCTION_TOOLS`、`REASONING`、
  `PROMPT_CACHE`；
- 只对官方明确为 multimodal 的 current model 暴露 `VISION`，不做 service-wide 假设；
- 本轮对 `HOSTED_WEB_SEARCH`、`HOSTED_WEB_FETCH`、structured `response_format` 与
  mixed hosted/client-tool 保持 `unknown` 或 unsupported。

Adapter 不会把 unknown upstream capability 变成 supported。错误继续使用现有有界、脱敏的
`ProviderError` contract，不增加 Kimi、GLM、MiniMax 专用 runtime exception hierarchy。

### Reasoning 与 replay

Kimi 使用 current model family，不使用已停止的 `kimi-latest` alias。K3 始终 reasoning，
并采用 application-owned effort mapping：`low → low`、`medium/high → high`、
`xhigh/ultracode → max`。K2.7 与 K2.6 发送 thinking enabled 及 `keep = all`；K2.5
发送 thinking enabled，但不把它标记为可保留 reasoning content。Adapter 在 model contract
要求时保留完整 assistant `reasoning_content`。依据当前 K2.6 thinking contract，只有
`tool_choice = auto` 或 `none` 被接受；`required` 与 specific function choice 在请求发送前
直接失败关闭；绝不静默关闭 thinking。依据是 [Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart)、
[API overview](https://platform.kimi.ai/docs/api/overview) 与 [tool-use guide](https://platform.kimi.ai/docs/api/tool-use)。

GLM current thinking model 发送 `thinking.type = enabled` 与 `clear_thinking = false`，
并在 tool round 之间保留完整 `reasoning_content`。已确认的 effort mapping 只属于该
dialect：GLM-5.3 使用映射后的 low/high/max；GLM-5.2 将 low/medium/high 映射为 high，
将 xhigh 映射为 max。当前官方 function-calling evidence 只允许 `tool_choice = auto`，
因此 required 或 specific choice 失败关闭。可选的 `tool_stream` 默认不启用。依据是官方
[thinking](https://docs.bigmodel.cn/cn/guide/capabilities/thinking.md)、[thinking mode](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode.md)、
[function calling](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling.md) 与
[stream-tool](https://docs.bigmodel.cn/cn/guide/capabilities/stream-tool.md) guide。

MiniMax 的 OpenAI-compatible request 使用 `max_completion_tokens` 与
`reasoning_split = true`。Adapter 捕获 streaming `reasoning_details`，不重复 cumulative
text，将有界 structured block 转换为 provider-neutral 的 opaque `PreservedContextItem`，
只有 profile、model、endpoint、protocol 与 context affinity 全部匹配时才为 MiniMax replay。
Runtime、Message domain object 与 SQLite schema 不解释 MiniMax shape。user-visible projection
仍是普通 assistant `content`；private reasoning 不会复制到最终回答。依据是官方 [OpenAI-compatible
API](https://platform.minimaxi.com/docs/api-reference/text-openai-api.md) 与 [M3
function-call guide](https://platform.minimaxi.com/docs/guides/text-m3-function-call.md)。
Provider 的 M3 adaptive-thinking 不被伪装成跨 Provider parity；application-owned effort
只做有界的 adapter-local mapping。

### Tools、usage、image 与 error

三家都使用现有标准 function schema、streamed `tool_calls`、JSON argument accumulation、
tool-result continuation、redaction 与 permission-owned local tools。本轮不给任何一家
native hosted web tool。Usage 只从真实返回字段映射：input、completion、total/cache，包含
Kimi top-level cache、GLM nested cache detail 与 MiniMax nested cache detail。Provider 没有
报告时不推算 cache hit 或 reasoning usage。

Image content 通过现有 OpenAI-compatible image-part boundary 发送，前提是 exact model
descriptor 有 `VISION`。不声称某个 service 下所有 model 都接受图片。

### Settings、credential、proxy 与 failover

TUI 增加 Kimi、GLM、MiniMax service preset。Kimi 与 MiniMax 执行 remote model catalog；
GLM 显示有界官方 static list，不把它伪装成 credential validation。三家都保留手动输入
model。保存时普通 profile metadata 与 API-key store 分离，secret 不回显。

三家复用现有 environment、direct、explicit named-proxy mode。HTTP policy 通过 profile
port 构造，错误到达 TUI 或 tool boundary 前会脱敏。

Failover 仍是 pre-output、monotonic。Kimi → GLM 与 GLM → MiniMax 在 candidate selected
前使用现有 safe capability intersection。Provider-specific reasoning representation
不能被当作跨 Provider native state：当 profile 使用 `native_context = "profile"` 时，
context affinity 包含 profile、service、protocol、canonical endpoint 与 model。跨 Provider
fallback 只能使用 canonical provider-neutral projection，不能把 Kimi thinking state 或
MiniMax `reasoning_details` replay 给另一家。

### Web integration

Kimi、GLM、MiniMax 仍是普通 MAIN Chat Provider。现有 composition 会通过配置的 WEB_SEARCH
sidecar 解析 local `web_search`，并在 mode 允许时注册 P2 local `web_fetch`。因此三家都能
通过同一条 permission-owned Web architecture 获得外部知识能力，但本轮不声称 Kimi
`$web_search`、GLM hosted search、MiniMax MCP 或其他 vendor-specific Web capability。

## 验证

有界 fixture 覆盖 Kimi text/reasoning/tool/usage/image projection、GLM
reasoning/streamed tool argument/tool-result continuation/usage，以及 MiniMax structured
reasoning/tool/usage replay。Composition test 覆盖三种 China Provider 作为 MAIN 时的 local
Web Search sidecar 与 local Web Fetch。Live test 单独显式门禁：

| Provider | Test | Credential | 额外 opt-in |
|---|---|---|---|
| Kimi | `tests/live/test_kimi_live.py` | `MOONSHOT_API_KEY` 或 `KIMI_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` 且 `NEURO_CODE_RUN_LIVE_KIMI=1` |
| GLM | `tests/live/test_glm_live.py` | `ZHIPU_API_KEY` 或 `GLM_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` 且 `NEURO_CODE_RUN_LIVE_GLM=1` |
| MiniMax | `tests/live/test_minimax_live.py` | `MINIMAX_API_KEY` | `NEURO_CODE_RUN_LIVE_TESTS=1` 且 `NEURO_CODE_RUN_LIVE_MINIMAX=1` |

Model ID 与 base URL 支持环境变量 override。Live failure 只报告 Provider/status class，
不会回显 credential 或 response body。

## 后果与限制

- 一个共享 adapter 保持 tool loop、proxy、redaction、failover 与 context 行为一致，vendor
  quirk 只停留在 wire boundary。
- GLM model discovery 在确认稳定的官方 catalog endpoint 前保持 static。
- Native hosted Web Search/Web Fetch、MiniMax MCP、可选 GLM `tool_stream`、Provider-specific
  structured output 与完整 structured-output parity 不属于 P3A。
- Live certification 依赖显式 paid/network opt-in 与 credential；缺少 credential 是 skip，
  不是对 Provider 健康状态的声明。
- 增加默认 model 或提升 capability 前必须刷新当前官方 model ID 与 model-specific evidence。
