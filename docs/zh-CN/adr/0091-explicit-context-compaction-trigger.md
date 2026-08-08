# ADR 0091：显式且默认关闭的上下文压缩触发

- 状态：已接受
- 日期：2026-08-08
- 范围：应用层记忆与未来 Runtime 接入边界

## 背景

仓库现在已经分别具备压缩评估、有界脱敏摘要输入、单次 Provider 生成、持久化条目保存以及恢复重建契约。
这些契约仍然刻意不从 `AgentRuntime` 调用。未来 Runtime 需要一个窄边界：可以无副作用地评估回合，并且只在
到达安全模型回合边界后请求持久化。

## 决策

新增 `neuro_code.application.memory.compaction_trigger`，包含：

- 默认的 `ContextCompactionTriggerMode.DISABLED`，只返回确定性评估，从不调用 Provider 或存储适配器；
- `ContextCompactionTriggerMode.EXPLICIT`，可以把一个可执行的 `RECOMMENDED` 或 `REQUIRED` 计划委托给现有的
  `ContextCompactionApplicationService`；
- 不可变的请求、评估和结果值，只暴露有界计划元数据；源上下文、标识符和过期源指纹不会进入表示；
- 无状态的 `ContextCompactionTriggerService`，在执行可操作的持久化请求前要求会话 ID、压缩 ID、带时区的创建时间
  以及由调用方持有的预期源指纹。

触发服务从传入的不可变 `ModelContext` 重新计算计划。未知容量、不可执行计划和关闭模式都是空操作。过期源会在
现有持久化服务联系 Provider 前失败。Provider、取消和存储错误继续传播；没有 fallback、重试、事件或部分成功结果。

压缩是独立的应用操作。它不会增加 `AgentRunResult.steps`，不会复用普通回合的模型/工具预算，不会发出事件，也不会
保留尝试状态。未来 Runtime 接入必须明确安全边界和事务语义，不能从本服务推断它们。

## 后果

- Runtime 调用方可以评估每个回合而不启用自动压缩。
- 显式调用方获得一条可复用且带过期源保护的摘要和存储契约路径。
- 生成与持久化仍是两个操作；本 ADR 不声称整轮 SQLite 原子性。
- 本阶段不改变 Provider、SessionStore schema、会话条目、导入/导出、事件、CLI、TUI 或 ACP 行为。
