# ADR 0098：由回合记录器拥有可选的压缩最终化

- 状态：已接受
- 日期：2026-08-08
- 范围：`TurnEventRecorder`

## 背景

ADR 0097 引入了可将已完成回合与一个持久化压缩条目一起提交的原子存储操作。
仅有存储方法并不能定义哪个应用组件可以调用它。若摘要生成器或接口直接调用存储端口，
就会绕过回合事件顺序、会话所有权和现有完成路径。

## 决策

`TurnEventRecorder.finalize_turn_completion()` 接受一个可选、已经校验的
`DurableCompactionItem`。传入时，记录器要求存在持久化会话并调用
`SessionStore.finalize_turn_with_compaction()`；未传入时继续调用普通的 `finalize_turn()`。

记录器只拥有最终存储提交。它不会生成摘要、构建上下文、调用 Provider、改变普通 Agent loop，
也不会消费压缩失败投影。非法压缩输入会在完成事件加入记录器内存事件列表之前失败。

这是未来回合所有者使用的显式接缝。当前没有模型步骤或自动阈值调用它，后台自动唤醒完成仍保持原有执行记录策略。

## 结果

现在由一个应用层组件拥有事件与压缩条目的组合提交，并保持原有事件交付顺序：持久化完成后才交付
`TURN_COMPLETED`。Provider 生成和取消仍在 SQLite 事务之外，错误语义保持不变。未来 Runtime 接入时，
必须在调用该方法前明确决定 `ContextCompactionRuntimeFailureProjection` 如何转换为完成结果。
