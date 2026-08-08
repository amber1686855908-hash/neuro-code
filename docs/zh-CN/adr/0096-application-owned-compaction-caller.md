# ADR 0096：由应用层拥有的显式压缩调用方

- 状态：已接受
- 日期：2026-08-08
- 范围：`ApplicationComposition` 与 `AgentConversation`

## 背景

Stage5DO 暴露了窄化的 `AgentRuntime.trigger_context_compaction()` 接缝，
但把生产 gate 的组装和并发所有权留给了后续调用方。压缩不能与普通回合竞争，
也不能假装重建或持久化并不由它拥有的上下文。

## 决策

`ApplicationComposition.create_binding()` 现在为每个 binding 使用现有 Provider、
SessionStore、配置的脱敏值以及既有压缩持久化/触发服务构造一个压缩 gate。gate
会注入 `AgentRuntime`，但不会被自动调用，普通 Agent loop 也不会检查阈值。

`AgentConversation.trigger_context_compaction()` 是显式的应用调用方。它：

- 在会话已有的 `_turn_lock` 下串行化请求，因此同一会话的普通回合和显式压缩不会重叠；
- 接收完整不可变的 `ContextCompactionRuntimeRequest` 作为调用方拥有的上下文快照，
  不重建或修改上下文；
- 对 `EXPLICIT` 请求要求已有会话且 `session_id` 匹配；没有持久化会话时仍可进行关闭模式评估；
- 委托给注入的 Runtime 接缝，保持 Provider、取消、超时、过期源和存储错误语义；
- 不追加会话条目、不重新加载 transcript、不发事件，也不声称与普通回合具有事务原子性。

请求中的源指纹继续作为过期快照保护。压缩持久化仍是独立的短存储操作；本决策不声称它与
`SessionStore.finalize_turn()` 原子地绑定。

## 结果

生产组合现在拥有真实、显式且可测试的 gate，同时普通回合行为不变，压缩仍然只能通过显式调用启用。
后续可以增加用户显式命令或回合最终化接入，但必须保留会话锁，并单独定义任何跨操作事务契约。
