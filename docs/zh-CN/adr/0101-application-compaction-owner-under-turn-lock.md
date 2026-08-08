# ADR 0101：在回合锁下由应用层拥有压缩最终化

- 状态：已接受
- 日期：2026-08-08
- 范围：`AgentConversation` 与 `ConversationRunner`

## 背景

Stage5DS 和 Stage5DT 已定义从显式压缩门控到回合最终化的类型化交接，但应用调用方仍需自行协调门控与最终化。若分别调用这两个操作，普通回合可能在二者之间启动，或者无操作投影可能被传给期待终态值的回调。

## 决策

`AgentConversation.run_context_compaction_with_owner()` 是显式且可选的应用层接缝：

- 请求校验和门控调用都在现有会话 `_turn_lock` 下执行；
- 调用方提供完整的不可变上下文快照、边界、过期源元数据和会话身份；
- 成功的门控结果在调用所有者回调前转换为 `ContextCompactionTurnProjection`；
- 有界超时转换为已有的可恢复 `BUDGET_LIMITED/WALL_TIME_BUDGET` 投影；
- 无操作投影在调用所有者之前失败关闭；
- 取消、Provider、存储和未知失败保留原始异常，且不调用所有者；
- 所有者回调仍负责 `TurnEventRecorder` 及任何最终化事务；会话方法本身不发事件、不修改 transcript 条目，也不持久化回合。

泛型 `ConversationRunner` 协议向应用消费者暴露相同的类型化回调形状。普通 Agent loop、自动阈值触发和面向用户的 UI 保持不变。

## 后果

门控和所有者现在共享一个明确的并发边界，未来调用方可以证明只有成功条目或受控超时会被交给回合最终化。这不会让 Provider 摘要生成与 SQLite 持久化成为同一事务：现有压缩服务仍会在所有者回调前保存条目，而所有者可以为回合最终化选择独立的回合加条目原子存储契约。
