# ADR 0120：Gemini Interactions 与 Hosted Web Tool

- 状态：已接受；P1.2 纵向切片
- 日期：2026-08-19
- 范围：Gemini Interactions、Google Search、URL Context 与 Gemini route 组合

## 背景

现有 Gemini adapter 负责 legacy `generateContent` 与
`streamGenerateContent` contract。Google 现在建议新开发使用 Interactions API。
Interactions 提供 typed step timeline、stateless 全量 history input、Google Search、
URL Context 与 client function call。因为 step 和 continuation 语义不等价于
`generateContent` 的 candidate/part，所以必须新增独立 adapter。

应用层已经拥有 session persistence、resume/fork、provider affinity、tool execution
和 role routing。若让 Gemini 再拥有一套 durable conversation，就会出现两套 authority，
并让 provider 切换变得不明确。

## 决策

1. `gemini-interactions` 作为独立 protocol，由 `GeminiInteractionsProvider` 独占
   wire owner。Google AI Studio service 同时声明 `gemini-generate-content` 与
   `gemini-interactions`；前者继续作为 compatibility 默认值。已有 profile 不会被
   silent rewrite。
2. Adapter 使用 stable API `v1` 与 `/v1/interactions`，发送 `store = false`，永不发送
   `previous_interaction_id`。每次 request 都发送完整 Neuro Code context。已有 profile
   中的 `v1beta` base URL 只在构造 stable Interactions endpoint 时规范化，不修改持久化
   profile。
3. Interactions SSE event 映射到现有 canonical model event：text → `ModelTextDelta`，
   thought summary → `ModelReasoningDelta`，function call → `ModelToolCall`，lifecycle
   call → `ModelBackendToolStarted`/`ModelBackendToolCompleted`，usage → `ModelUsage`，
   terminal interaction status → `ModelCompleted`。不新增 Gemini 专用 domain event family。
4. Adapter 将 response 的有界、JSON-safe step sequence 保留在一个 immutable
   `PreservedContextItem` 中，保留 thought signature、function call ID、`call_id`、
   function result、Google Search step、URL Context step 与 provider signature。只有
   provider profile、service、protocol、model 和 context affinity 全部精确匹配时才 replay；
   否则忽略 opaque item，使用 standard projection。
5. Google Search 使用显式 `google_search` builtin，URL Context 使用 `url_context`。
   Effective capability 是 service/protocol/model metadata、adapter evidence 与
   configuration 的 fail-closed 交集。Unknown model 不获得 hosted capability。Search
   与 URL Context 不互相隐式开启。
6. Catalog 与 adapter 只在文档支持的 Gemini 3 风格 model 上暴露
   `MIXED_HOSTED_AND_CLIENT_TOOLS`。Inline search 只有在 hosted search supported，且
   同时注册的 client tool 受到 mixed capability 覆盖时才可用。否则 AUTO 在存在可执行
   WEB_SEARCH route 时选择 sidecar；显式 INLINE 失败关闭。
7. Gemini WEB_SEARCH sidecar 只发送有界、已脱敏的 query 与 policy prompt。使用
   `tool_choice` 的 `allowed_tools.mode = "any"` 与
   `allowed_tools.tools = ["google_search"]`，从而成功结果必须出现真实 Google Search
   lifecycle。在 Gemini 3 兼容 profile 上，已配置的 `url_context` 仍作为 search-and-
   deepen 流程的 secondary builtin 声明；forced choice 仍要求 search call。终态回答没有
   成对且成功的 search result 时，映射为 `SEARCH_PROVIDER_DID_NOT_SEARCH`。
8. Google Search 与 URL Context lifecycle result 本身不是 evidence。Search citation 以
   model-output 的结构化 `url_citation` annotation（`url`、`title`、`start_index`、
   `end_index`）为准。URL Context annotation 使用同一 canonical source/citation 投影；
   retrieval status（包括 `unsafe`）保留为 provider-native diagnostic state，不能变成
   可信 fetch evidence。`search_suggestions` HTML 被 canonical extractor 忽略，也不会
   作为 TUI evidence render。
9. Function-result continuation 使用 canonical `function_result` input，保留原始
   `call_id`、function name 与有界 JSON/text result。Malformed streamed argument JSON、
   非法 step shape、unsupported tool combination、failed/unsafe/incomplete interaction、
   observer failure、provider error、timeout 与 cancellation 都在现有 provider boundary
   失败。Cancellation 可以关闭 HTTP stream，不会被转换成触发错误 failover 的 provider text。

## 后果

- Gemini 可以作为 MAIN 通过 inline 使用 Google Search，也可以作为独立 WEB_SEARCH
  sidecar；DeepSeek、OpenAI-compatible、Anthropic 或 xAI MAIN 继续通过现有
  `WebSearchService` 与 `ToolExecutor` 路径使用它。
- Legacy Gemini 行为保持隔离并可回归测试。Interactions native state 有界且属于
  provider-private；应用层只看到 canonical model event、capability、route 与 web-search result。
- URL Context 只是 Gemini hosted capability。本轮不提供公共 local `web_fetch` tool、任意
  browser fetcher 或 provider-independent fetch service。

## 非目标

- 不实现 server-side durable Interaction ownership、`previous_interaction_id` 或 background
  interaction mode。
- 不实现 local Web Fetch、Browser、Code Execution、Maps、File Search、Computer Use、MCP
  server、Deep Research 或新中国 Provider。
- 不做大规模 settings/TUI 重构，也不 silent migrate managed/native 的
  `gemini-generate-content` profile。本轮 Interactions 仍是显式 protocol option。

## 依据

- [Gemini API versions](https://ai.google.dev/gemini-api/docs/api-versions)
- [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Gemini Interactions API reference](https://ai.google.dev/api/interactions-api-v1)
- [Migration and streaming guide](https://ai.google.dev/gemini-api/docs/migrate-to-interactions)
- [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
- [URL Context](https://ai.google.dev/gemini-api/docs/url-context)
- [Built-in and custom tool combinations](https://ai.google.dev/gemini-api/docs/tool-combination)
