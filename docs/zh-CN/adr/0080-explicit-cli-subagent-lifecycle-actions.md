# ADR 0080：显式 CLI 子代理生命周期操作

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5CZ

## 背景

Stage5CY 已为已关联的只读子会话增加显式 TUI 控制。CLI 也应复用同一个
application 生命周期 owner，同时不能让 CLI 负责存储、模型执行或子上下文重建。

## 决策

Stage5CZ 增加 `neuro subagents ACTION TASK_ID --parent-session SESSION_ID`
命令，其中 `ACTION` 只能是 `resume`、`fork` 或 `delete`。命令通过现有组合边界打开应用、
校验父会话工作区，然后委托 `SubagentRelationshipLifecycleService`。

- `resume` 返回有界的子会话选择结果，不启动模型回合，也不重放工具。
- `fork` 委托现有会话生命周期 fork，并报告新会话 ID；不会自动打开或重新登记 fork。
- `delete` 只在 application 层关系和任务终态校验后删除关联子会话。

普通输出只包含简短生命周期消息。`--json` 使用 typed CLI 序列化器，只输出父会话、任务、
子会话 ID、规范动作以及可选的 fork 会话 ID。提示词、会话项、事件、工具参数、凭据、Provider
状态和子上下文不会被序列化。

## 边界

该命令是显式且有界的，不调度子会话、不运行模型、不创建递归或并行子会话、不增加可写工具，
也不宣称超出现有会话生命周期 owner 能力的跨进程原子性。`resume` 仍然只是选择操作；用户必须
通过后续命令显式启动子会话回合。

## 结果

CLI、TUI 和未来的入站适配器可以共享同一个 application 生命周期契约。CLI 仍是适配器，不直接
读取 SQLite。本阶段不改变 schema、Provider、Runtime Kernel、Finalizer 或普通 Agent 行为。
