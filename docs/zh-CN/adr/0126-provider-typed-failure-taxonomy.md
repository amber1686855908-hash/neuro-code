# ADR 0126：Provider 类型化失败分类

**简体中文** · [English](../../en/adr/0126-provider-typed-failure-taxonomy.md)

## 状态

接受，适用于当前 pre-alpha Runtime。

## 背景

此前 Provider 边界会把大多数 HTTP、传输和协议失败压成通用的
`ProviderError`。resilience 再从错误文本片段猜测是否可重试，health 只暴露 Python
异常类名，failover 也无法区分错误请求和瞬态上游故障。这会让永久请求错误污染瞬态
熔断，也让公开失败事件缺少有用语义，同时存在暴露原始 Provider 载荷的风险。

五个模型 HTTP 适配器是 OpenAI-compatible Chat、OpenAI Responses、Anthropic Messages、
Gemini Generate Content 和 Gemini Interactions。Provider 目录发现拥有独立的
`ProviderCatalogError` 契约，仍然属于单独的只读发现边界。

## 决策

- `ProviderFailure` 是不可变、有界、已脱敏的事实对象，包含 `kind`、安全 detail、可选
  HTTP 状态码、有界 `Retry-After`、已知时的 Provider/模型身份、可选生命周期 phase 和
  证据来源（`provider`、`transport`、`local` 或 `unknown`）。它不包含 retry、circuit
  或 failover 决策，不包含请求体、请求头、原始 cause 或凭据。异常链仍通过 `__cause__`
  保留。
- Runtime 分类为 `authentication`、`authorization`、`rate_limit`、`invalid_request`、
  `model_not_found`、`context_overflow`、`server`、`timeout`、`network`、`protocol` 和
  `unknown`。配置仍保留现有 `ConfigurationError` 层次，而不是变成 Provider kind。
  `asyncio.CancelledError` 原样传播。
- HTTP 状态和结构化 Provider 错误字段在适配器边界分类。共享 HTTP 分类器采取保守边界：
  不解析人类可读 message，不把通用 404 直接说成 `model_not_found`，不把没有结构化依据的
  429 直接变成可重试的 `rate_limit`，并把通用 413 归为 `invalid_request`。随后由各适配器
  只负责本协议已记录的精确 envelope 字段。传输异常类型区分 timeout 与 network；损坏的
  流/协议载荷属于 Provider protocol 事实。只有有效的 `Retry-After` 才会被解析，并在
  进入本地调度器前限制上界。
- `ProviderFailurePolicy` 拥有三个相互独立的决策。认证、授权、模型不存在和上下文
  超限不重试、不计入瞬态熔断，但可在输出前隔离候选项。限流可重试、可故障转移，且
  不计为不健康。server、timeout 和 network 可重试、计入熔断并可故障转移。invalid
  request 不执行这些动作；protocol 不重试、不计熔断但可故障转移；Provider/transport
  unknown 不重试、不计熔断但可在输出前故障转移，local unknown 则停在当前候选项。配置
  错误跳过候选项但不污染熔断。
- 一旦观察到任意模型事件，就同时禁止 retry 和 failover。部分流也不新增熔断失败，
  因为无法安全重放，且它不代表一次干净的请求尝试。
- `ProviderHealth.last_failure_kind` 是稳定的类型化观测；原有 `last_error_type` 作为
  兼容字段保留。失败尝试事件保留原字段，并追加可选的类型化 kind/status 字段。

## 一致性证据

实现使用来自官方协议错误 envelope 的离线 fixtures。这是有界的一致性切片，不宣称完整
的 Provider 兼容性。

| 协议 | 使用的精确结构化证据 | 规范事实与策略边界 |
|---|---|---|
| OpenAI-compatible Chat / OpenAI Responses | OpenAI 文档中的认证、临时限流、余额、组织/项目 spend、usage limit、服务端错误和 `response.failed` 的 `server_error` 字段 | 明确的 billing/spend/usage code 归为 `authorization`；明确的瞬时 rate code 归为 `rate_limit`；未知 429 保持 `unknown` |
| Anthropic Messages | `error.type` 的 `authentication_error`、`billing_error`、`permission_error`、`invalid_request_error`、`request_too_large`、`rate_limit_error`、`api_error`、`timeout_error`、`overloaded_error` 等 | `billing_error` 归为 `authorization`；`rate_limit_error` 归为 `rate_limit` 并保留文档规定的 `Retry-After` 提示；`not_found_error` 保留为通用资源/endpoint 404 |
| Gemini Generate Content | `error.status` / ErrorInfo reason 的 `API_KEY_INVALID`、`INVALID_ARGUMENT`、`FAILED_PRECONDITION`、`PERMISSION_DENIED`、`RESOURCE_EXHAUSTED`、`INTERNAL`、`UNAVAILABLE`、`DEADLINE_EXCEEDED` 等 | `RESOURCE_EXHAUSTED` 是文档定义的 429 rate-limit 事实，覆盖 RPM/TPM/RPD/spend 维度，归为 `rate_limit`；有界策略会重试但不计入熔断 |
| Gemini Interactions | `error.code` 的 `authentication`、`permission_denied`、`model_not_found`、`not_found`、`rate_limit_exceeded`、`quota_exceeded`、`api_error`、`service_unavailable`、`deadline_exceeded` 等 | 明确的 rate、quota、model code 分开分类；未来 code 保持 `unknown` |

主要参考：[OpenAI error codes](https://developers.openai.com/api/docs/guides/error-codes)、
[OpenAI rate limits](https://developers.openai.com/api/docs/guides/rate-limits)、
[OpenAI Responses streaming](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal/delta)、
[Anthropic API errors](https://platform.claude.com/docs/en/api/errors)、
[Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits)、
[Gemini Generate Content API errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors)、
[Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)、
和 [Gemini API errors](https://ai.google.dev/gemini-api/docs/api-errors)。

## 熔断与配置语义

`consecutive_failures` 表示从上次成功请求或任意一次不计入熔断的失败之后开始，连续的、
输出前且有资格计入瞬态熔断的失败数。因此 `SERVER, SERVER, INVALID_REQUEST, SERVER`
和 `SERVER, SERVER, RATE_LIMIT, SERVER` 在最后都为 1，阈值为 3 时都不会打开熔断。
部分流永远不会新增熔断失败。

`ConfigurationError` 保持独立。当前输出前行为是候选项级别：Provider dialect 或工具
配置错误可以在 failover 链中跳过当前候选项，但不会重试，也不会计入 health。全局配置
错误应在候选项执行前失败关闭，不重新解释为 Provider health 证据。

## 影响

Provider resilience 不再依赖错误文本措辞。适配器可以调整面向用户的 detail，而不会
改变重试或路由行为。health 与事件投影保持有界和脱敏，同时能区分请求、歧义配额、传输、
local runtime、服务端和协议事故。离线 fixtures 只证明已列出的 envelope；live/付费调用
和真实 Provider benchmark 尚未运行。

该分类是进程内语义，不宣称持久化健康评分、可见输出后的自动模型替换、Provider 特定
计费语义或 live-provider benchmark。目录发现和 hosted web-search sidecar 错误继续使用
各自独立的契约。

## 不变量

1. 永久请求/配置失败或不计入熔断的 unknown 失败不能打开瞬态 Provider 熔断。
2. 可观察模型输出后不得 retry 或 failover。
3. retry 必须来自类型化事实和规范策略，不得新增基于错误文本片段的启发式。
4. 公开失败 detail 必须有界并脱敏；原始 cause 和凭据不得进入 health、事件、UI 或日志。
