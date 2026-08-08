# ADR 0092：显式 Runtime 压缩安全边界

- 状态：已接受
- 日期：2026-08-08
- 范围：未来 Runtime 接入的 application memory 边界

## 背景

Stage5DK 提供了默认关闭的显式压缩触发器，但没有定义 Runtime 何时可以安全调用它。在模型请求或工具批次仍在进行时调用，
可能生成与已持久化会话不一致的上下文快照；取消之后继续调用 Provider，也会把已取消的回合变成额外工作。

现有摘要生成器已经恰好执行一次严格无工具 Provider 请求。普通回合预算和取消生命周期不能被静默复用到该操作。

## 决策

新增 `neuro_code.application.memory.compaction_runtime`，包含：

- `ContextCompactionSafePoint.BEFORE_MODEL_REQUEST` 与
  `ContextCompactionSafePoint.AFTER_TOOL_BATCH` 两个当前唯一建模的安全位置；
- 类型化的 `ContextCompactionRuntimeBoundary`，记录模型步骤，以及模型请求、工具批次或取消是否处于活动状态；
- `ContextCompactionBoundaryDecision`，区分关闭、不可操作、不安全、已取消和允许执行；
- `ContextCompactionRuntimeBudget`，将当前压缩契约固定为一次模型请求、零次工具调用且不继承普通回合预算；
- 无状态的 `ContextCompactionRuntimeGate`，先评估，只有显式启用、计划可执行且处于安全边界时才委托
  `ContextCompactionTriggerService`。

不安全或已取消的请求返回有界空操作结果，绝不联系 Provider 或存储适配器。允许执行的请求如果发生 Provider、取消或存储错误，
继续沿用现有触发服务的异常传播语义。

## 后果

这只是 application 契约和测试接缝，不修改 `AgentRuntime`、不新增事件、不启用自动阈值触发、不实现墙钟超时，也不宣称 Provider 生成与
SQLite 持久化之间具有原子性。未来 Runtime 接入必须在真正获得安全边界快照后调用该门控；在接受超时语义前，还必须增加实际执行超时的契约。
