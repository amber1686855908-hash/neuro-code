# ADR 0099：类型化上下文压缩回合投影

- 状态：已接受
- 日期：2026-08-08
- 范围：`application.memory.compaction_runtime`

## 背景

Stage5DR 为 `TurnEventRecorder` 提供了可选路径，可以把经过校验的持久化压缩条目与已完成回合一起原子提交。Runtime 门控也已经分类超时、取消、Provider 和存储失败。但调用方仍需要一个小而明确的类型化边界，只转移它实际拥有的安全值，而不是让门控发事件或静默吞掉失败。

## 决策

`ContextCompactionTurnProjection` 及其两个投影函数作为显式转移边界：

- `project_context_compaction_result()` 只从成功触发结果中转移已经持久化且校验过的 `DurableCompactionItem`；
- `project_context_compaction_failure()` 只转移有界的 `ContextCompactionRuntimeFailureProjection` 及其可选类型化 outcome；
- 超时可以交给拥有回合最终化事务的调用方，形成可恢复的 `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome；
- 取消、Provider 和存储失败仍然只能传播；
- 未知异常保持未分类，不猜测成某种结果。

投影的表示中不保存异常、提示词、原始摘要、工具数据或工作区数据。它不执行持久化、不发事件、不调用 Provider，也不调用 `TurnEventRecorder`。未来的回合所有者仍必须显式决定完成事件数据并调用记录器。普通 Agent loop 和自动压缩继续关闭。

## 后果

成功路径可以把条目传给 `TurnEventRecorder.finalize_turn_completion(..., compaction_item=item)`，无需重新构建或保存。超时只有在回合所有者掌握最终化事务时才能被消费。传播型失败不会被误报为空回合或成功回合。Provider 生成和独立压缩保存仍在 SQLite 事务之外；本投影不扩大原子性声明。
