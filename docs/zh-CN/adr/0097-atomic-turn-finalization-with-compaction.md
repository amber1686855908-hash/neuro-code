# ADR 0097：带压缩记录的回合最终化原子事务

- 状态：已接受
- 日期：2026-08-08
- 范围：`SessionStore` 与 SQLite 持久化

## 背景

持久化上下文压缩目前刻意作为独立操作。`save_compaction_item()` 拥有一个短事务，
而 `finalize_turn()` 拥有 `TURN_COMPLETED` 事件、会话条目、搜索投影和可选执行记录。
依次调用这两个方法无法让 Provider 请求和两次存储操作共享一个事务，也无法保护回合免受
两次写入之间失败的影响。

下一步接入需要一个精确的存储契约，但不能改变普通回合或隐式扩大现有压缩方法的语义。

## 决策

`SessionStore` 暴露一个显式选择的方法：
`finalize_turn_with_compaction(session_id, event, items, record, compaction_item)`。
SQLite 实现在同一个 `BEGIN IMMEDIATE` 事务中写入以下投影：

- `TURN_COMPLETED` 事件；
- 追加安全会话条目前缀和搜索投影；
- 可选的 `SessionExecutionRecord`；
- 一个 `DurableCompactionItem`。

只有所有投影都成功后才提交；校验、唯一性、搜索索引或存储失败会回滚整个事务。
完全相同的已有压缩 ID 具有幂等性；所有者或载荷冲突会被拒绝。重复完成事件仍然是错误。

`save_compaction_item()` 继续保持独立短操作。新方法不会把 Provider 生成纳入 SQLite 原子性，
不会改变 `finalize_turn()`，也不会被普通 Agent loop 或当前显式压缩 gate 调用。未来拥有回合最终化
事务的调用方可以显式选择该方法，但仍必须单独定义超时和取消所有权。

## 结果

现在可以区分两种声明：

1. 独立压缩写入自身是原子的；
2. 显式回合最终化调用可以原子提交事件、条目、执行记录、搜索投影和压缩行。

两种声明都不包含 Provider 或网络工作。现有普通回合以及默认关闭的压缩行为保持不变。
