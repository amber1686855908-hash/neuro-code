# ADR 0119：Anthropic Hosted Web Search 与 Web Fetch

- 状态：已接受；P1.1 纵向切片
- 日期：2026-08-19
- 范围：Anthropic Messages hosted `web_search` 与 `web_fetch`

## 背景

P1.0.5 已建立 Provider-neutral 的 Hosted Web Search contract，以及
OpenAI Responses/xAI route。Anthropic Messages 将 Web Search 和 Web Fetch
暴露为 server tool；它们的 request/result block 必须保留在原生 assistant
context 中。若把它们当作本地 function tool，就会丢失 server-tool result、citation
和 continuation state；若把模型文本当成执行证据，则纯知识回答也可能伪装成搜索。

## 决策

1. Anthropic adapter 独占 server-tool wire protocol。它复用现有的
   `ModelBackendToolStarted` 与 `ModelBackendToolCompleted` lifecycle event，
   不在 application port 中增加 Anthropic 专用 event family。
2. 配置的 server tool 使用当前版本化定义 `web_search_20260318` 与
   `web_fetch_20260318`，并设置 `allowed_callers = ["direct"]`。Web Fetch
   开启 citation，并限制 uses/content；它永远不会注册为本地 `ToolDefinition`。
3. Anthropic hosted capability 采用 fail-closed。Catalog 只记录明确文档支持的
   model family；未知 model 或未配置的 builtin 始终为 `UNKNOWN`，不能激活 hosted route。
4. 包含 server-tool block 的每个 response 保留为一个有界、带 provider/model
   affinity 的 `PreservedContextItem`。回放时作为原生 assistant content 投影，并抑制
   重复的普通 assistant projection。不透明 encrypted field 可以留在私有 continuation
   item 中，但不得进入 canonical `WebSearchResult`、普通文本、日志或 UI evidence。
5. `pause_turn` 在一次 provider stream 内继续，保留原始 server content 和 tool definition，
   continuation 次数上限为 3。server/client 混合 response 返回 client `ModelToolCall`
   并保留 server content；后续 request 可以从 preserved native item 完成 server lifecycle，
   且不会重复回放普通 assistant message。
6. Sidecar route 复用现有 `HostedWebSearch` boundary。它强制使用 Anthropic
   `tool_choice = {"type": "tool", "name": "web_search"}`，只发送有界且已脱敏的 query
   与 canonical domain filter，要求成对且成功的 server search result，并把结构化 result
   block 与 citation location 映射为现有 source/citation contract。Web Fetch 仍是该
   sidecar turn 中可用的 Anthropic server capability；不新增本地 HTTP fetcher。
7. Provider error、server-result error、取消和 observer failure 都在 provider boundary
   作为 error 处理。Sidecar 将它们映射到现有稳定的 `WebSearchErrorCode`，并保留取消语义。

## 后果

- MAIN 在 profile 明确开启时可 inline 使用 Anthropic hosted search；WEB_SEARCH 也可通过
  独立 route resolution 与 fallback 使用同一 Provider。
- Search source/citation 是 canonical、有界、经过 domain filter 的数据，可按不可信 evidence
  渲染。Native encrypted continuation state 不是 canonical result format。
- Adapter 只支持当前版本化 server-tool schema。未来 Anthropic tool-version 变化必须显式
  更新 adapter/catalog，并补充 fixture coverage。

## 非目标

- 不新增本地 `web_search` 或 `web_fetch` tool。
- 不启用 browser、任意 URL fetcher、code execution 或 Anthropic dynamic filtering。
- 不包含新 Provider、Gemini hosted capability 或 UI 重构。

## 依据

- [Anthropic Web Search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
- [Anthropic Web Fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)
- [Anthropic server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)
