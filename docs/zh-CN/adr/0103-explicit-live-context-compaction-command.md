# ADR 0103：显式实时上下文压缩命令

- 状态：已接受
- 日期：2026-08-08
- 范围：`AgentRuntime` 与 `AgentConversation`

## 背景

阶段 5DU 和 5DV 已经提供了带会话锁的 owner 接缝，以及确定性的 usage 与
过期源构建器，但应用调用方仍需自行重新构建实时模型上下文并提供持久化
元数据。重复组装容易压缩过期快照，或遗漏当前请求指引。

自动压缩仍然明确不在本阶段范围内。命令必须显式、有界，并与普通回合使用
同一个会话锁串行化。

## 决策

新增窄的显式应用命令：
`AgentConversation.run_explicit_context_compaction_with_owner()`。

该命令：

- 要求已有持久化会话，并由调用方提供 Provider 上下文窗口；
- 先获取现有会话 `_turn_lock`，再构建实时快照；
- 通过 `AgentRuntime.build_context_snapshot()` 应用模型请求使用的相同推理、
  交互、指令和技能指引；
- 委托已配置的 `ContextCompactionRuntimeGate` 构建请求，复用 usage 快照和
  过期源保护构建器；
- 调用方未提供时，生成有界的压缩身份和时间元数据；
- 在同一把锁内复用现有 owner 投影路径；
- 不追加 transcript 条目、不发事件、不启动普通模型回合，也不启用自动阈值检查。

该命令面向可执行 owner：不可执行的评估仍通过现有 owner 契约安全失败，
不会调用 Provider 或存储适配器。Provider 生成和持久化继续保持既有的独立事务边界。

## 后果

应用调用方现在拥有一个不会与普通回合竞争、也不会静默复用过期摘要的实时
上下文入口。Runtime façade 仍保持精简，ModelProvider 协议不变；未来 CLI 或
TUI 命令可以调用该接缝，而不必直接访问 SQLite。后续真正的用户命令仍需定义
无操作展示方式和成功投影的最终化 owner；本 ADR 不新增该界面。
