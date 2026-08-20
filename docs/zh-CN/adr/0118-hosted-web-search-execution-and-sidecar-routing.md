# ADR 0118：Hosted Web Search 执行与 Sidecar Routing

- 状态：已接受；P1 纵向切片
- 日期：2026-08-19
- 范围：Canonical Web Search、OpenAI/xAI hosted search、route 选择和 MAIN tool 接线

## 背景

Provider service metadata、三态 capability 和 role route 已经存在，但还没有执行
Web Search。下一步需要获取当前 Web evidence，同时不能让 Provider-native payload、
外部页面或模型生成的 query 变成受信任的应用指令；还必须保持现有 Responses
lifecycle、xAI 行为、MAIN failover 和 ToolExecutor ownership。

本切片有意只覆盖 hosted search，不建立通用 Web capability framework，也不实现
页面抓取、浏览或新 Provider。

## 决策

### 1. 保持一个 Provider-neutral 且有界的 contract

`neuro_code.application.ports.web_search` 拥有 `WebSearchRequest`、
`WebSearchSource`、`WebSearchCitation`、`WebSearchResult`、`WebSearchError`、
`WebSearchMode` 和 hosted-search execution port。该 contract 不包含 OpenAI、xAI、
Responses、SSE 或任何 Provider-private 类型。

Request 对 query、source 数量和 domain filter 设置上限。allowed 与 blocked domain
互斥，并规范化为 ASCII hostname。Result 对 evidence、source/citation 数量、URL、
title、snippet、Provider/model label、metadata 和 UTF-8 总字节数设置上限。Source
与 citation 会去重；无法投影到该结构的 Provider payload 不会进入 application。

稳定错误枚举为：`SEARCH_UNAVAILABLE`、`SEARCH_UNSUPPORTED`、
`SEARCH_AUTHENTICATION`、`SEARCH_RATE_LIMIT`、`SEARCH_TIMEOUT`、
`SEARCH_PROVIDER_ERROR`、`SEARCH_PROVIDER_DID_NOT_SEARCH` 和
`SEARCH_INVALID_REQUEST`。

### 2. 在注册 client tool 前解析执行意图

`WebSearchMode` 有四个显式值：

| Mode | 解析结果 |
|---|---|
| `disabled` | 不启用 inline capability，也不注册本地 search tool |
| `auto` | MAIN 明确支持时使用 inline hosted search；否则 search route 可执行时使用 sidecar；否则 unavailable |
| `inline` | 只允许 MAIN hosted search；不支持时失败关闭 |
| `sidecar` | 只使用独立 WEB_SEARCH route；该 binding 会移除 MAIN hosted search |

`RuntimeRole.MAIN` 与 `RuntimeRole.WEB_SEARCH` 保持独立。MAIN fallback 不会被当作
search fallback，search fallback 也不会改变 MAIN Provider。通用 `ModelRoute` 仍只
包含 role、profile、model 和隔离的 fallback name；执行 mode 由 Web Search boundary
拥有。

### 3. 由 application service 拥有 route，由 composition 构造 Provider

`WebSearchService` 负责 WEB_SEARCH route 查找、capability status 校验、request/result
脱敏边界和规范化错误投影。它只通过 Provider-neutral resolver 解析显式 search route。
Composition root 为配置的 OpenAI Responses profile 构造
`ResponsesHostedWebSearchBackend`，并且只在解析结果为 sidecar 时创建 `WebSearchTool`。

Sidecar 恰好执行一个有界 hosted request，不创建 `AgentLoop`、`Subagent`、workspace、
shell、permission 或 write capability。既有 `ToolExecutor` 执行 client tool，把
canonical `ToolResult` 配对回 MAIN，并通过已有 event sink 转发既有 backend lifecycle
event。

### 4. OpenAI hosted capability 必须显式，xAI 保持兼容

标准 OpenAI Responses adapter 只接受显式 builtin name `web_search`。配置该名称时，
request body 才包含 hosted tool 以及结构化 source 提取所需的 include；未配置时，
标准 OpenAI 不获得 hosted search capability。Sidecar 还会按选定的 wire dialect 发送
canonical domain filter，并将 `tool_choice` 设置为 `required`；对于契约承诺要返回
search evidence 的 request，不能依赖 `auto`。

xAI Responses dialect 继续使用既有 builtin-tool 集合、native context affinity、
reasoning include 和 backend lifecycle 行为。Sidecar 复用同一个 Responses adapter 与
HTTP policy，不新增第二套 xAI 实现。Canonical blocked domain 会映射为 xAI 的
`excluded_domains`（标准 Responses 则为 `blocked_domains`）；超过 xAI 有界 domain
filter 限制的 request 会在发送前拒绝。即使所选 xAI MAIN profile 还有其他 hosted tool，
sidecar 也只暴露 `web_search`。

Anthropic hosted search、Gemini search/context、本地 fetch、browser、LSP、Tool Search
和新 Provider adapter 不在本决策范围内。

### 5. 将 Web evidence 视为不可信外部数据

Extractor 只读取有界的结构化 source/citation 字段，包括 Responses 的
`web_search_call.action.sources` 与当前 output-text 嵌套 `url_citation` payload。结构化
终态 evidence 是权威依据；如果终态 response 没有已完成的 Provider-side
`web_search_call` 或等价的 Provider-side usage/citation evidence，即使其中有看似合理的
模型回答，sidecar 也会以 `SEARCH_PROVIDER_DID_NOT_SEARCH` 失败关闭。xAI 完整的 URL
citation list 会投影为有界 source；xAI inline Markdown citation 只作为有界兼容回退；
inline assistant text 只追加有界的可见 URL 列表，避免 TUI 的不可激活 Markdown 渲染器
静默丢失来源。Raw Provider response、任意 annotation、页面指令和无界 payload 不会跨过
Provider boundary。

面向模型的本地 result 以 `[UNTRUSTED WEB EVIDENCE]` 开始，标明 query，随后列出有界
source/evidence，并把有界 synthesis 放在该边界之后。Source URL 只允许 HTTP(S)，
domain filter 还会在本地进行第二次校验；query、evidence、source metadata、citation、
Provider/model label、event 和持久化的 Web Search call arguments 都会经过既有脱敏。

### 6. 明确取消与 lifecycle ownership

取消会从 `WebSearchTool`、`WebSearchService`、sidecar Provider stream 传到 Responses
HTTP context；sidecar 不会在取消后继续执行或 failover。Hosted backend 的开始/完成
event 仍使用 Provider-neutral 形状，并映射到既有 `BACKEND_TOOL_STARTED` /
`BACKEND_TOOL_COMPLETED` event kind。Credential 不会进入 canonical contract、ToolResult、
TUI projection 或日志。Sidecar usage 是 auxiliary 数据，不会合并进 MAIN 的 model usage
或 context-budget accounting。

配置支持 `[web_search] mode`、显式的标准 OpenAI `builtin_tools` 和独立的
`[routing.web_search]` chain。当前 TUI 继续通过 catalog 配置 Provider；完整 Web Search
设置编辑器等运行时 contract 获得更多 Provider capability evidence 后再扩展。Runtime
保持 capability-aware，并在不确定时失败关闭，不把不支持的 hosted search 显示为可用。

## 非目标

- Anthropic search/fetch 或 Gemini search/context。
- 本地 HTTP fetch、browser control、LSP、Tool Search 或通用 Web browsing。
- 新 Provider adapter，或 application 对具体 Provider 的 import。
- 在 application contract 中引入 Provider-private request/response object。
- 把 Search Sidecar 实现为 AgentLoop 或 subagent。
- 提升 search result 的信任级别，自动执行页面指令，或根据外部 evidence 修改工作区。

## 结果

当前纵向切片通过一个有界、可测试的 contract 支持 OpenAI 与 xAI hosted Web Search，
也支持 DeepSeek/OpenAI-compatible MAIN 通过独立 search route 使用 tool。未来 Anthropic/
Gemini hosted search 与 Local Fetch 可以复用该 Provider-neutral boundary，不需要改动
ToolExecutor 或 route model。显式不可信边界与结构化提取规则仍是这些后续能力的兼容性门槛。
Portable contract/provider/TUI 测试已通过；OpenAI 与 xAI live smoke 只有在分别提供凭据和
network flag 时才会显式运行，并允许 model override，默认保持跳过。

## 证据与参考

- OpenAI hosted web search guide：[Responses `web_search`、filter、source 与 tool choice](https://platform.openai.com/docs/guides/tools-web-search)
- xAI 文档：[Web Search](https://docs.x.ai/developers/tools/web-search)、[citation](https://docs.x.ai/developers/tools/citations) 与 [tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- `tests/test_web_search.py`、`tests/test_openai_responses_provider.py`、
  `tests/test_provider_routes.py`、`tests/test_tui.py`、
  `tests/live/test_openai_web_search_live.py`、`tests/live/test_xai_web_search_live.py`
  以及既有 xAI Responses regression suite。
