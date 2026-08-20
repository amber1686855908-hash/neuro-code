# ADR 0117：Provider Service、Capability 与 Runtime Route 基础

- 状态：已接受；托管 Web 执行仍按计划不实现
- 日期：2026-08-19
- 范围：Provider 元数据、能力解析、模型发现和角色路由

## 背景

当前 Provider Runtime 已经拥有命名 profile、credential、代理策略、context
affinity、failover、四种线路协议、供应商方言、OpenAI Responses 以及 xAI
托管工具事件。重写这套 Runtime 会带来兼容性风险。剩余的架构缺口是 profile、
推理服务、线路协议、model、capability 和 runtime role 没有被清晰分开表达。
此外，TUI 仍拥有六个可选 Provider preset，模型发现主要按 protocol 推导策略。

## 决策

### 1. Service metadata 是 Catalog，不是 Runtime 层级

`ProviderServiceDescriptor` 与 `ProviderServiceCatalog` 是规范的
application port 元数据。默认 Catalog 描述以下六个服务：

| Service ID | 现有 UI key | 默认线路 |
|---|---|---|
| `openai` | `openai` | OpenAI Responses |
| `generic-openai-compatible` | `compatible` | OpenAI Chat |
| `deepseek` | `deepseek` | OpenAI Chat + `deepseek-v4` 方言 |
| `anthropic` | `anthropic` | Anthropic Messages |
| `google-ai-studio` | `gemini` | Gemini generateContent |
| `xai` | `xai` | OpenAI Responses + `xai` 方言 |

Publisher 信息是可选 metadata。它不选择 adapter、不保存 credential，也不拥有
请求执行。未来增加服务应只需要 descriptor、可选 catalog strategy、能力元数据
和测试；不能因此要求 AgentLoop、ToolExecutor 或 TUI 增加 Provider 分支。

六个默认 descriptor 有意与 application port contract 放在一起，因为它们只是不可变
的 selection metadata，而不是 infrastructure adapter：不导入 HTTP/client 实现、
credential store 或 request lifecycle。TUI 和 configuration 消费注入的 catalog，
把这些值移动到 infrastructure 只会改变依赖方向，不能消除 interface layer 对服务
知识的需要。

### 2. 现有 protocol 与 dialect quirk 继续权威

现有四种 protocol 继续作为 wire 边界，基础设施按 protocol 选择 adapter。
DeepSeek DSML 行为继续作为现有 OpenAI-compatible adapter 中受边界约束的
`deepseek-v4` 方言，不被压平为普通 OpenAI 行为。xAI Responses 方言及其已有请求、
流式行为保持不变。

### 3. Capability 规范化、分层并失败关闭

`ModelCapability` 提供下一阶段所需的最小跨 Provider vocabulary。
`ModelCapabilitySet` 记录 `supported`、`unsupported` 和 `unknown` 三态。
上游事实按以下顺序细化：

```text
service -> protocol -> model
```

Runtime 随后解析保留来源的 `CapabilityResolution`：

```text
上游事实
    meet 可信 adapter implementation capability
    restrict 显式 profile/configuration disable
    = effective executable capability
```

上游或 profile 的 `SUPPORTED` 声明不能把 adapter 的 `UNKNOWN` 或 `UNSUPPORTED`
提升为支持。只有明确为 `supported` 的 effective capability 才会让 `supports()`
返回 true；`unknown` 不会被当成支持。adapter implementation 集合由具体 wire
adapter 以及 xAI 已配置的 builtin-tool 名称决定。Provider-hosted 名称在该可信
adapter 边界映射为规范 hosted capability；当前 xAI 的 `web_search`、`x_search`、
`code_interpreter` 行为保持不变。

受管 metadata 可以持久化 service identity 和 capability preference，包括显式
disable 或用于检查的 supported 声明，但它不是 Runtime 证据；重新加载后仍必须
经过上游事实、adapter implementation 和 configuration 解析。

### 4. Hosted Tool 与本地 Tool 保持分离

Provider-hosted tool 继续在 Provider API 内执行，并使用已有的
`ModelBackendToolStarted` / `ModelBackendToolCompleted` 生命周期。它们不进入
`ToolRegistry`、`ToolExecutor`、permission 或 sandbox。本 ADR 不启用 OpenAI、
Anthropic 或 Gemini hosted-web 行为，也不新增 xAI 行为。

### 5. Runtime Role 与 Route 是增量 projection

`RuntimeRole` 当前包含 `MAIN` 与 `WEB_SEARCH`。`ModelRoute` 把 role 绑定到
provider profile 与 model，并支持 role 隔离的 fallback。它不编码 Web Search
execution strategy；未来的 Web-specific type 可以在 Web 执行实现时拥有
inline-versus-sidecar 选择。

未来语义必须明确：`INLINE_HOSTED` 表示 MAIN inference request 自身直接启用
provider-hosted search，不注册本地 `web_search` tool；Provider 继续发出已有的
`ModelBackendToolStarted` / `ModelBackendToolCompleted` 生命周期。`SIDECAR_HOSTED`
表示 MAIN agent 调用 client-side tool，未来依次经过 `ToolExecutor`、WebSearchService、
独立的 `WEB_SEARCH` route、search provider、规范结果和 `ToolResult`。即使两个 route
最终选择同一个 Provider，只要使用独立 model request，仍然属于 sidecar 而不是 inline。
本 ADR 只记录这些语义，不启用任一路径。

现有 `[routing] default` 与 `fallbacks` 继续作为活动 main runtime 的权威配置，
并 projection 为 `RuntimeRole.MAIN`。可选的 `[routing.web_search]` 只做配置校验和
脱敏 projection，本阶段不由 AgentLoop 调用。不同 role 或 profile 之间不会隐式
推断 fallback。

### 6. Model discovery 使用显式 strategy 接缝

`ProviderConnectionSpec.catalog_strategy` 与 descriptor 的 catalog strategy
可以选择 `openai-compatible-models`、`anthropic-models`、`gemini-models`、
`static` 或 `manual-only`。现有按 protocol 的默认推导仍作为兼容回退，但未来
服务不必为了不同的 model-list 策略继续增加 protocol 条件。没有稳定 discovery
endpoint 时仍允许手工填写 model ID。

### 7. TUI 消费 Catalog

`ProviderSettingsScreen` 渲染注入的 `ProviderServiceCatalog`，并从 descriptor
读取 endpoint、protocol、dialect、label 与 discovery strategy。现有 UI key 与
持久 profile matching 保持当前行为。TUI 不再拥有六个 Provider preset 定义。

### 8. Security 与兼容性边界不变

Service、route 和 capability metadata 不包含 credential。现有
`providers.json` / `credentials.json` 分离、environment/direct/explicit HTTP
策略、脱敏、context affinity、Provider failover、native replay、prompt-cache
行为和 Provider adapter ownership 均保持。

在 failover candidate 选定前，`FailoverModelProvider.capabilities` 返回所有已配置
candidate 的安全交集。因此 supported primary 加 unsupported fallback 在请求开始前
不会暴露 hosted capability。首个 candidate 选定后，capability 跟随当前 active
provider。这是最小的失败关闭策略；未来 route-aware hosted service 可以增加按所需
capability 过滤 candidate，而不重写单向 failover loop。

## 非目标

- 不新增 Kimi、GLM、MiniMax、Volcengine Ark、百度千帆、阿里百炼或腾讯
  TokenHub 的 Provider adapter。
- 不实现 `web_search` / `web_fetch` 本地工具、Search Sidecar、本地 HTTP fetcher
  或新的 provider-native Web 行为。
- 不在通用 route contract 中加入 execution mode。
- 不重写 AgentLoop、ToolRegistry、ToolExecutor、PermissionManager、Sandbox 或
  conversation core。
- 不迁移 keyring。

## 结果

现在的 service/profile/protocol/model/capability/role/route vocabulary 足以校验未来
的 `DeepSeek MAIN + Gemini WEB_SEARCH`，而不修改 AgentLoop core。Runtime 仍只有一条
活动执行路径：已有的 main Provider/failover 路径。后续 hosted-web 与 sidecar 工作
必须由 Web-specific application boundary 拥有 inline-versus-sidecar 选择，再使用
通用 `WEB_SEARCH` route 和相同的 HTTP policy。同一个 provider 并不自动意味着
inline；只有 MAIN inference lifecycle 自身启用 hosted capability 时才是 inline。

## 证据

Provider Service Catalog、capability、route、ProviderCatalog strategy 与 TUI 注入
测试覆盖了该 contract。现有 Provider、xAI hosted-tool、failover、configuration 与
TUI regression suite 继续属于验收门禁。
