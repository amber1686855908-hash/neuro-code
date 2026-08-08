# ADR 0095：显式 Runtime 压缩接缝

- 状态：已接受
- 日期：2026-08-08
- 范围：`AgentRuntime` application facade

## 背景

Stage5DN 定义了未来 Runtime 如何消费压缩超时，同时不改变取消、Provider 或存储错误的语义。
现有门控已经要求调用方提供安全边界快照，但 Runtime 还没有面向调用方的接缝。在压缩事务和事件
契约准备好之前，把阈值检查自动加入 `AgentRuntime.run()` 会改变普通回合行为。

## 决策

`AgentRuntime` 接受可选的
`compaction_runtime_gate: ContextCompactionRuntimeGate | None`，默认值为 `None`，并暴露
`trigger_context_compaction()` 处理调用方显式提供的 `ContextCompactionRuntimeRequest`。

- 没有 gate 时以 `ConfigurationError` 失败关闭；不会退回 Provider 请求或普通 Agent 回合。
- 调用方必须提供完整的不可变请求，包括触发上下文、源保护值、安全位置、活动操作标志、取消状态和独立压缩预算。
- facade 只校验请求类型并委托给注入的 gate，不推导阈值、不修改上下文、不增加回合 steps、不发事件，也不写 execution record。
- 门控已有的超时、取消、Provider 和存储语义保持不变。未来回合所有者可以显式消费 Stage5DN 的超时投影，且只能通过回合最终化保存记录。
- `AgentRuntime.run()` 保持不变；不会启用自动或默认压缩，ApplicationComposition 也暂不注入 gate。

## 后果

测试和未来 application 调用方可以在已经证明安全的边界运行压缩，而不会把普通 Agent loop 与阈值或持久化策略耦合。
该接缝刻意保持窄小：生产 gate 组装、事件和整轮原子性都需要后续独立纵向切片。
