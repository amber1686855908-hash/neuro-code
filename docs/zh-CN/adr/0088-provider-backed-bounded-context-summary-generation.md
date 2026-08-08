# ADR 0088：Provider 驱动的有界上下文摘要生成

- 状态：接受 Stage5DH 垂直切片
- 日期：2026-08-08
- 范围：application memory 与现有 ModelProvider 端口

## 背景

Stage5DD–Stage5DG 已建立确定性的压缩评估、Provider 感知的摘要请求、脱敏输入投影和可持久化恢复记录。
下一步需要提供一个显式的 Provider 摘要请求边界，但不能改变 AgentRuntime 主循环，也不能信任原始会话数据。

## 决策

`ProviderContextSummaryGenerator` 位于 canonical
`neuro_code.application.memory.compaction` 模块。它只接收经过校验的
`ContextSummaryInput`，从有界投影构建临时的两条消息 `ModelContext`，并且恰好使用
`tools=()` 与 `ModelToolPolicy.DISABLED` 发起一次请求。

提示词只包含固定指导、有界 Provider 标签、计数和已经投影的条目文本。它不会接收源上下文、工具参数、推理正文、
Provider 保留载荷、凭据或源指纹。生成器在返回内存中的
`ContextSummaryGenerationResult` 之前，会再次对 Provider 输出脱敏并执行 UTF-8/token 限制；摘要不会进入 `repr`。

文本增量会先缓冲；如果存在 `ModelCompleted.response_text`，优先使用它，避免重复拼接。缺少完成事件、空响应、多个完成事件
或 `ModelToolCall` 都会作为 `ProviderError` 处理。`ProviderError` 和取消原样传播；不执行工具，不进行重试，不写持久化、不发运行时事件，
也不启用自动压缩。

生成器会把请求中的 Provider/model 身份与注入的 Provider 对比。未提供的上下文亲和标识保持兼容；显式提供的亲和标识必须匹配。后续 Runtime
切片只有在确定自己的事务和过期源策略后才能调用该生成器。

## 影响

- 在不扩大 model port 或改变普通请求的前提下，摘要生成拥有可测试的 Provider 接缝。
- 生成的摘要只有在调用方明确传给 `build_durable_compaction_item()` 和 storage port 后才会持久化。
- Provider 专用 tokenizer、重试、Runtime 接入、压缩事件、UI 行为、导入/导出和整轮原子性仍留待后续阶段。
