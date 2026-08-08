# ADR 0079：显式子代理生命周期操作

- 状态：已接受
- 日期：2026-08-07
- 范围：Stage5CY

## 背景

Stage5CX 为父会话及只读子代理增加了只读 TUI 关系视图。该视图只展示安全
能力标签，并不执行生命周期操作；后续动作不能把这些标签变成由 UI 直接拥有
的存储或运行时行为。

## 决策

Stage5CY 在 application sessions 层增加 `SubagentRelationshipLifecycleService`。
它接受一个类型化的父任务关系动作：

- `resume` 校验父会话拥有的关系和已终止的 `SUBAGENT` 任务，然后只返回经过校验的
  子会话 ID；不会运行模型、重放工具或复用 Finalizer 临时上下文。
- `fork` 委托现有 `SessionLifecycleController` 对子会话执行分叉，只返回新会话 ID；
  不会自动打开分叉会话，也不会把它登记为新的子任务。
- `delete` 委托删除关联的子会话，绝不会删除父会话。

关系、父任务或子会话缺失，父任务不是 `SUBAGENT`，或者任务仍在运行时，服务都会
失败关闭。标识符有界且不包含控制字符；自指向关系会被拒绝。

TUI 通过显式命令 `/subagents ACTION TASK_ID` 暴露这些操作。它调用 application
controller，只渲染有界结果，不访问 SQLite、transcript、工具、权限或 Provider 状态。
Resume 通过既有会话选择边界选中子会话；fork 只报告新 ID 而不自动打开；delete 报告
已删除的子会话 ID。

校验和委托变更是分开的调用。本阶段不声明跨进程原子性；既有会话生命周期 owner
继续负责其既定的锁和持久化语义。

## 非目标

本阶段不增加自动调度、递归创建、并行子会话、父上下文复用、可写工具、checkpoint、
worktree 或新的会话 schema。

## 结果

生命周期控制现在有一个精简的 application seam，后续其他入站接口可以复用，而不必
复制归属校验。TUI 仍然只是适配器，生命周期操作保持显式且有界。
