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
  HTTP 状态码、有界 `Retry-After`、已知时的 Provider/模型身份和可选生命周期 phase。
  它不包含 retry、circuit 或 failover 决策，不包含请求体、请求头、原始 cause 或凭据。
  异常链仍通过 `__cause__` 保留。
- Runtime 分类为 `authentication`、`authorization`、`rate_limit`、`invalid_request`、
  `model_not_found`、`context_overflow`、`server`、`timeout`、`network`、`protocol` 和
  `unknown`。配置仍保留现有 `ConfigurationError` 层次，而不是变成 Provider kind。
  `asyncio.CancelledError` 原样传播。
- HTTP 状态和结构化 Provider 错误字段在适配器边界分类；传输异常类型区分 timeout 与
  network；损坏的流/协议载荷分类为 protocol。只有有效的 `Retry-After` 才会被解析，
  并在进入本地调度器前限制上界。
- `ProviderFailurePolicy` 拥有三个相互独立的决策。认证、授权、模型不存在和上下文
  超限不重试、不计入瞬态熔断，但可在输出前隔离候选项。限流可重试、可故障转移，且
  不计为不健康。server、timeout 和 network 可重试、计入熔断并可故障转移。invalid
  request 不执行这些动作；protocol 不重试、不计熔断但可故障转移；unknown 采取保守
  策略，不重试、计入熔断且可在输出前故障转移。配置错误跳过候选项但不污染熔断。
- 一旦观察到任意模型事件，就同时禁止 retry 和 failover。部分流也不新增熔断失败，
  因为无法安全重放，且它不代表一次干净的请求尝试。
- `ProviderHealth.last_failure_kind` 是稳定的类型化观测；原有 `last_error_type` 作为
  兼容字段保留。失败尝试事件保留原字段，并追加可选的类型化 kind/status 字段。

## 影响

Provider resilience 不再依赖错误文本措辞。适配器可以调整面向用户的 detail，而不会
改变重试或路由行为。health 与事件投影保持有界和脱敏，同时能区分请求、配额、传输、
服务端和协议事故。

该分类是进程内语义，不宣称持久化健康评分、可见输出后的自动模型替换、Provider 特定
计费语义或 live-provider benchmark。目录发现和 hosted web-search sidecar 错误继续使用
各自独立的契约。

## 不变量

1. 永久请求/配置失败不能打开瞬态 Provider 熔断。
2. 可观察模型输出后不得 retry 或 failover。
3. retry 必须来自类型化事实和规范策略，不得新增基于错误文本片段的启发式。
4. 公开失败 detail 必须有界并脱敏；原始 cause 和凭据不得进入 health、事件、UI 或日志。
