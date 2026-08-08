# ADR 0094：Runtime 压缩失败投影

- 状态：已接受
- 日期：2026-08-08
- 范围：未来 Runtime 接入使用的 application memory 契约

## 背景

Stage5DM 为显式启用的压缩请求强制执行有限的墙钟时间，同时保留 Provider
失败、存储失败和 `asyncio.CancelledError` 的原语义。该门控有意不属于普通
`AgentRuntime` 主循环，因此未来接入需要稳定的失败消费方式，既不能让门控写入
执行记录，也不能静默转换取消和基础设施失败。

## 决策

`neuro_code.application.memory.compaction_runtime` 现在暴露类型化的
`classify_context_compaction_failure()` 投影。它不保存异常消息、prompt、上下文、
Provider 载荷或存储细节。

- `ContextCompactionTimeoutError` 是唯一的受控终态投影。未来 Runtime 可以把它映射为
  带 `WALL_TIME_BUDGET` 原因、可恢复的 `BUDGET_LIMITED` 结果；由于压缩不是最终回答，
  该结果不会标记为 `finalized`。
- 超时投影的记录策略是 `TURN_FINALIZATION`。这表示只有拥有回合最终化事务的调用方才可以
  将对应的 `SessionExecutionRecord` 与该回合终态事件一起持久化。压缩门控本身绝不写执行记录。
- `asyncio.CancelledError`、`ProviderError` 和 `SessionError` 都是传播投影，不携带终态结果，
  也不请求执行记录。原始异常仍由调用方负责处理。
- 未知异常不会被分类。不得猜测其为预算、取消或存储状态。

该投影只是策略契约，不会捕获异常、修改 `AgentRuntime`、发出事件、触发自动压缩，或声称
Provider/SQLite 事务原子性。未来 Runtime 必须在安全边界显式决定是否消费超时投影；任何执行
记录都必须使用现有回合最终化事务。

## 后果

超时行为可以脱离主循环独立测试，同时普通 Provider、存储和取消行为保持不变。未来接入不能
意外持久化独立压缩失败，也不能用伪造的终态结果隐藏未知异常。
