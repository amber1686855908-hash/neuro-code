# ADR 0123：多模型平台 Provider 兼容性

- 状态：Accepted；P3B 纵向切片
- 日期：2026-08-20
- 范围：火山方舟 Ark、百度千帆、阿里云 Model Studio / 百炼、腾讯 TokenHub

## 背景

P3A 增加了直连 Kimi、GLM 和 MiniMax 服务，它们主要各自暴露一条
OpenAI-compatible Chat 路径。P3B 的形状不同：一个推理服务可以承载多个
publisher，同一模型在不同协议上可能有不同限制，而且线路还可能随地域、工作空间
或计费 endpoint class 改变。

本切片重新阅读了下面的官方页面。它们是当前矩阵的证据边界；模型目录、endpoint
class、plan 规则和协议兼容性都可能变化，在提升任何 `UNKNOWN` 事实前必须重新刷新。

### 官方 evidence matrix

| 服务 | 官方服务和推理 endpoint | 认证与发现 | 本轮使用的协议证据 |
|---|---|---|---|
| 火山方舟 Ark | [Ark 快速开始](https://www.volcengine.com/docs/82379/1795150) 记录 `https://ark.cn-beijing.volces.com/api/v3` 与 Responses 请求；[Ark API 概览](https://api.volcengine.com/api-docs/view/overview?serviceCode=ark) 记录 Chat `/chat/completions`，并把控制面 API 分开 | 官方示例使用 API key；本切片没有接受稳定的推理 `/models` 合约，因此 UI 使用有界版本化列表并允许手工填写 | 已列 Ark model descriptor 支持 OpenAI-compatible Chat 和 Responses；Anthropic Messages 明确不支持。Function calling 作为 portable 能力支持；Ark 内置工具不在范围内。见 [function calling/Responses 文档](https://www.volcengine.com/docs/82379/1958524?lang=zh) |
| 百度千帆 Qianfan | [OpenAI-compatible API](https://cloud.baidu.com/doc/qianfan/s/Hmh4suq26) 使用 `https://qianfan.baidubce.com/v2`；[V2 兼容页面](https://cloud.baidu.com/doc/qianfan/s/qmh4sv5vi) 描述共享路径 | OpenAI-compatible 请求使用 API key/Bearer；[模型列表](https://cloud.baidu.com/doc/qianfan-api/s/Dmba8k71y) 记录 `GET /v2/models`。[Anthropic-compatible 页面](https://cloud.baidu.com/doc/qianfan-docs/s/6mh3e6gjp) 记录 `/anthropic` 与 `x-api-key` Messages wire 兼容，但没有 Anthropic model-list endpoint | Chat 是最宽的 portable 路径。Responses 只对 [Responses 文档](https://cloud.baidu.com/doc/qianfan-docs/s/4mi400l1m) 明确列出的模型标记 `SUPPORTED`。Anthropic 是 manual-only model discovery 且仅表示 Messages wire 兼容，不继承 Anthropic server-tool 能力 |
| 阿里云 Model Studio / 百炼 | [Base URL 与地域指引](https://www.alibabacloud.com/help/en/model-studio/base-url) 记录地域、共享和 workspace-scoped endpoint；[Model Studio 概览](https://www.alibabacloud.com/help/en/model-studio/what-is-model-studio) 记录 OpenAI-compatible 访问 | API key 按地域区分。OpenAI Chat/Responses 使用 compatible-mode 路径；[Anthropic Messages](https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages) 使用 Anthropic 路径，并明确没有 `/v1/models`，因此手工填写模型 | Qwen descriptor 支持 Chat、Responses、Anthropic Messages。第三方 descriptor 支持 Chat 和 Anthropic；Responses 保持 `UNKNOWN`。不声称 hosted `web_search`、`web_extractor` 或 code-interpreter 行为 |
| 腾讯 TokenHub | [API 文档](https://cloud.tencent.com/document/product/1823/130078) 记录广州 `https://tokenhub.tencentmaas.com`、新加坡 `https://tokenhub-intl.tencentmaas.com` 以及 `/v1/models`。[兼容性概览](https://cloud.tencent.com/document/product/1823/130079) 记录 Chat、Responses 和 Anthropic 路径 | Chat/Responses 使用 Bearer；Anthropic Messages 使用 `x-api-key`；推理 `/v1/models` 是有界远程发现路径 | model descriptor 区分原生 Responses（`hy3`）、兼容转换 Responses（列出的 GLM/Kimi/DeepSeek 路线）、不支持 Responses（`hy-mt2-pro`）和未知 Responses（列出的 Qwen 路线）。[Responses 转换文档](https://cloud.tencent.com/document/product/1823/133813) 不会把内置工具变成 Neuro Code hosted tool |

矩阵有意窄于各厂商的完整能力。只有选定 service、model descriptor、protocol 和现有
adapter 一致时，厂商功能才会成为 Neuro Code 的可执行能力。

## 决策

### Service 不等于 Publisher

`ProviderServiceDescriptor` 表示推理服务，不拥有 credential 或 runtime adapter。
当官方模型身份足够明确时，`ProviderModelDescriptor` 可以携带可选 publisher metadata。
比如 TokenHub、百炼或千帆暴露的 DeepSeek 模型可以带 DeepSeek hint，但 dispatch 仍由
`service_id`、profile protocol、dialect 和显式 base URL 决定。四个平台 service 都不
填写 service-level publisher。

### Model-specific protocol 矩阵

每个列出的模型保存三态协议事实：

| 服务/模型证据族 | `openai-chat` | `openai-responses` | `anthropic-messages` | Responses 模式 |
|---|---:|---:|---:|---|
| Ark `doubao-seed-2-0-lite-260215`、`doubao-seed-1-6-250615` | `SUPPORTED` | `SUPPORTED` | `UNSUPPORTED` | native |
| 千帆官方 Responses 模型 | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | 千帆专用 wire 路径 |
| 百炼 Qwen descriptor | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | Model Studio wire 路径 |
| 百炼第三方 descriptor | `SUPPORTED` | `UNKNOWN` | `SUPPORTED` | 不假定 Responses |
| TokenHub `hy3`、`hy3-preview` | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | native |
| TokenHub 列出的 GLM/Kimi/DeepSeek 模型 | `SUPPORTED` | `SUPPORTED` | `SUPPORTED` | compatibility-converted |
| TokenHub `hy-mt2-pro` | `SUPPORTED` | `UNSUPPORTED` | `SUPPORTED` | 无 Responses 路径 |
| TokenHub 列出的 Qwen 模型 | `SUPPORTED` | `UNKNOWN` | `SUPPORTED` | 不声明 Responses |

`UNKNOWN` 保留为明确的手工/配置状态，绝不渲染为已确认兼容；`UNSUPPORTED` 同时由
`ProviderProfile` 和 `ManagedProviderProfile` 拒绝。协议事实不从 publisher 名称或远程
模型 ID 的存在推断。

### 共享 adapter 与 compatibility firewall

factory 继续使用：

- `openai-chat` 使用 `OpenAICompatibleProvider`；
- `openai-responses` 使用 `OpenAIResponsesProvider`；
- `anthropic-messages` 使用 `AnthropicProvider`。

不新增 `ark.py`、`qianfan.py`、`bailian.py` 或 `tokenhub.py` runtime adapter。服务级
catalog strategy、endpoint metadata、model matrix 和有界 protocol hint 放在
provider-service catalog。adapter 不按 publisher dispatch，也不会因为 wire 兼容就继承
OpenAI hosted tools。本轮不增加 Ark search、千帆原生 search、百炼 hosted tools 或
TokenHub hosted tools；provider-neutral `web_search` 与 P2 local safe `web_fetch` 保持为
唯一 Web composition。

### Endpoint 与 profile identity

`ProviderEndpointVariant` 记录非 secret 的地域、workspace scope、计费 plan label、usage
scope，以及每个协议的文档 base URL。TUI 选择顺序是 service → endpoint variant →
protocol → model。选择 endpoint 只提供默认 URL；用户显式输入的 URL 仍然是 override，
adapter 永远不重写它。界面还提供仅用于选择的“自动 / 推荐”协议；在连接测试、保存或
运行前，它会解析成由模型证据支持的具体协议，不会把含糊的 auto 值持久化。

当前 variant：

| 服务 | Variant | 有意排除 |
|---|---|---|
| Ark | 北京推理 endpoint | 控制面模型管理 |
| 千帆 | 中国大陆推理 endpoint | 控制面 credential 生命周期 |
| 百炼 | 北京按量、 新加坡 workspace、新加坡共享、美国弗吉尼亚按量 | trial、Token Plan、Coding Plan 的开通/管理 |
| TokenHub | 广州、新加坡 | account 或 billing 管理 |

canonical base URL、protocol、model 和 service ID 都参与 native context affinity。因此同一
publisher/model 通过直连 DeepSeek、TokenHub、百炼和千帆时，不能在 profile 之间重放
provider-native state。地域/workspace label 只是 metadata；endpoint identity 是 profile
中选定的 URL。

### Discovery 与 settings

远程发现是只读且有界的。千帆 OpenAI-compatible 和 TokenHub 路径使用文档化的推理模型
路径；千帆 Anthropic 与百炼 Anthropic 因官方兼容页面没有安全的 `/v1/models` endpoint
而设为 manual-only。Ark 使用版本化有界列表/手工输入，不
伪造控制面 credential 流程。远程模型列表只证明可用，不会提升全部协议或 capability 事实。

现有 settings store 将 credential 与普通 metadata 分开，继续使用 redaction 和
`HttpClientPolicy`，并允许同一 service 创建多个 profile。TUI 不包含 platform runtime
分支，只消费 catalog label、variant、protocol fact 和 hint。

### Portable capability 语义

descriptor 当前只在官方证据支持且属于 portable 范围时暴露 function、reasoning 和模型级
vision 事实。Hosted Web Search/Web Fetch、厂商原生 search、structured output parity、
parallel-tool 保证和 plan-specific usage 行为，在没有独立 adapter 实现时保持 `UNKNOWN`、
不支持或不属于本切片。reasoning 由选定 wire adapter 规范化；publisher hint 不选择
reasoning parser。TokenHub 的 compatibility-converted Responses 只是描述性 metadata，
不伪装成原生 OpenAI item parity。

## 后果

正面后果：

- 四个新推理服务共用一套已测试的协议/runtime surface；
- model/protocol 限制集中且可测试；
- endpoint、workspace 和 billing identity 不会静默合并成一个 native context；
- local tools、sidecar web search、local safe fetch、权限、proxy、redaction 和 failover
  保持 provider-neutral。

已知限制：

- 官方模型目录和 capability fact 是版本化证据，不是实时 billing 或 control-plane inventory；
- Ark 和百炼 Anthropic discovery 有意采用 bounded/manual；
- 不包含 hosted vendor tools、control-plane SDK、cost estimator、billing manager、激活流、
  MCP、Browser、LSP 或绕过 plan 限制的行为；
- live tests 需要显式 paid/network opt-in，缺凭据报告为 `SKIPPED`，不表示 provider 已认证。

## 验证

离线 contract 覆盖 protocol matrix、unsupported/unknown profile validation、按协议区分的
model discovery header/path、共享 factory adapter 选择、fake-platform 可扩展性、TUI
endpoint/protocol 控件、同 publisher affinity 隔离和 pre-output failover contract。四个
live 文件如下：

| 服务 | 测试 | Credential | 必需 opt-in |
|---|---|---|---|
| Ark | `tests/live/test_ark_live.py` | `ARK_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_ARK=1` |
| 千帆 | `tests/live/test_qianfan_live.py` | `QIANFAN_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_QIANFAN=1` |
| 百炼 | `tests/live/test_bailian_live.py` | `DASHSCOPE_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_BAILIAN=1` |
| TokenHub | `tests/live/test_tokenhub_live.py` | `TOKENHUB_API_KEY` | `NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1` + `NEURO_CODE_RUN_LIVE_TOKENHUB=1` |

每个 live 文件支持 model、protocol、base URL 和现有 proxy override，并测试文本及本地
function-tool continuation。不测试未实现的 hosted tool，异常报告不包含 credential 或
响应 body。
