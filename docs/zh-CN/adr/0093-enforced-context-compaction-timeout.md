# ADR 0093：强制执行上下文压缩墙钟超时

- 状态：已接受
- 日期：2026-08-08
- 范围：未来 Runtime 接入的应用层记忆边界

## 背景

Stage5DL 定义了显式压缩请求何时安全，但当时的预算还没有真正执行墙钟时间限制。Provider 或持久化适配器如果无限等待，不能把未来的压缩调用变成无界的 Runtime 操作。

## 决策

扩展 `neuro_code.application.memory.compaction_runtime`：

- 在 `ContextCompactionRuntimeBudget` 中加入有限的 `max_wall_time_seconds`；
- 默认值为 30 秒，硬上限为 300 秒；
- 为被该限制取消的操作提供 `ContextCompactionTimeoutError`；
- 只有请求通过安全边界门控后，才在现有显式触发器外层使用 `asyncio.timeout`。

门控只捕获由自身截止时间产生的超时，并将其转换为类型化的压缩超时异常。Provider 错误、存储错误和 `asyncio.CancelledError` 继续原样传播。关闭、不可操作、不安全和已取消的请求仍然是无副作用空操作，也不会启动超时上下文。

超时覆盖摘要生成以及随后的持久化调用。它不宣称 Provider 生成与存储之间存在跨操作事务。如果存储适配器在截止时间被取消，门控不会返回成功结果；适配器级回滚仍由存储契约负责。

## 后果

上下文压缩现在有了真正执行的有限操作边界，同时不改变 `AgentRuntime`、Provider 接口、事件、普通回合预算或自动压缩。未来 Runtime 接入仍需决定如何报告超时，并且不得把超时视为已经成功持久化的压缩。
