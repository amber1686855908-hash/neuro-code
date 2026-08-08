# ADR 0074：只读父子子 Agent 关系投影

[English](../../en/adr/0074-read-only-parent-child-subagent-relationship-projection.md) · **简体中文**

## 状态

Stage5CT 已接受。

## 背景

Stage5CR 在父会话任务与全新子会话之间持久化了窄的 `SubagentLink`。Stage5CS 可以投影一次明确的子
Agent 运行结果，但调用方还需要安全地查看某个父会话拥有的子会话，以及当前有哪些生命周期操作可用。
查询边界不能成为第二个执行或变更所有者，也不能因为存在关系就暴露子会话 transcript。

## 决策

在 application sessions 层增加带类型请求和投影值的 `SubagentRelationshipQueryService`：

- 列表读取限定在一个父会话内，并具有有界 limit。结果按持久关系时间戳和任务 ID 排序。
- 投影只包含父/任务/子会话 ID、规范任务状态、子会话供应商和模型标签、子会话摘要时间戳以及有界的能力标签。
- 活动中的子任务（`queued` 或 `running`）不暴露任何生命周期操作标签。终态任务只暴露 `resume`、`fork` 和
  `delete` 标签作为能力说明；实际变更仍由既有会话生命周期服务独占负责。
- 查询会校验父任务确实是 `SUBAGENT` 任务，并校验子会话摘要存在。损坏的归属关系会报告错误，不会静默投影。
- 查询只读取关系、任务元数据和子会话摘要，不读取消息、事件、工具输出、提示词、凭据、参数或原始子上下文。
- SQLite 复用现有 `subagent_links` 表，执行有界有序读取。不增加 schema 或新的持久化记录。
- 不增加 CLI、TUI、ACP、调度器、重放、自动恢复或执行调用。未来接口可以通过 application 边界消费该投影。

## 拒绝的方案

- 直接返回 `SubagentLink` 会暴露 storage/domain 值，而不是接口安全的查询契约，也会让生命周期能力语义保持隐式。
- 返回完整 `SessionTask`、`SessionSummary` 或会话项会泄漏关系检查不需要的字段，并可能带出敏感数据或 transcript 内容。
- 让查询直接执行 `resume`、`fork` 或 `delete` 会复制既有生命周期所有者，并把读取变成意外副作用。
- 为活动任务暴露操作会引入与任务完成并发的竞态，因此在任务进入终态前不暴露操作标签。

## 影响

调用方可以在不依赖 SQLite、也不读取子 transcript 的情况下渲染或审计父子关系。该投影本阶段刻意不提供
用户可见命令；后续需要另一个拥有明确授权、并发和协议测试的纵向切片。
