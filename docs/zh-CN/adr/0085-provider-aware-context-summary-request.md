# ADR 0085：Provider 感知的上下文窗口与摘要请求

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DE

## 背景

Stage5DD 建立了 Provider 无关的压缩评估，但后续摘要服务需要知道计划属于哪个
Provider/模型窗口。现有 Provider port 有意不增加新的上下文窗口请求参数，而已配置的
Provider profile 和已选 Provider 事件已经携带有界的本地容量元数据。

## 决策

将 Provider 感知的压缩元数据保留在 application memory 接缝中。不可变的
`ProviderContextWindow` 只标识 Provider 和模型标签、可选的上下文亲和标识以及正的
token 容量。`CompactionContextUsage` 可以绑定到该窗口，planner 会把有界摘要 token
预算限制在已知容量以内。

可执行的计划可以投影为 `ContextSummaryRequest`。该请求包含计数、半开候选区间、目标
token 数、有界摘要预算和 Provider 窗口；绝不包含源条目、提示词、工具输出、凭据或
Provider 载荷。未知容量、不可执行的计划以及空候选区间不能生成摘要请求。

## 边界

本切片不改变 `ModelProvider`、Provider payload、`ModelContext`、`AgentRuntime`、
`Finalizer`、会话持久化或接口行为。不进行文本 token 化、不构建脱敏提示词、不调用模型、
也不持久化摘要。Provider 专用 token 化、摘要生成、可持久化压缩条目和 resume 重建
需要后续独立纵向切片。

## 验证

测试覆盖 Provider 窗口身份校验、用量/容量绑定、小容量预算裁剪、可执行计划投影、
未知容量和空候选拒绝以及有界摘要预算。架构和导入契约测试确保所有公共类型仍由
canonical memory 模块拥有。
