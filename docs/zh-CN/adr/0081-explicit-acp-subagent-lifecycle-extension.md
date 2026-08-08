# ADR 0081：显式 ACP 子代理生命周期扩展

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DA

## 背景

Stage5CZ 已通过 CLI 暴露由父会话拥有的子代理生命周期服务。ACP 客户端也需要一种有界方式请求
同一个明确的 `resume`、`fork` 或 `delete` 动作,同时不能接收内部 SQLite 标识符或子会话正文。

## 决策

Stage5DA 增加私有扩展 `_neuro-code/session/subagents`。请求严格只包含：

- `sessionId`：父会话的外部 ACP session alias；
- `taskId`：有界的父 `SUBAGENT` 任务标识符；
- `action`：`resume`、`fork` 或 `delete`。

ACP 适配器通过已有 alias 和工作区边界解析父会话，然后把类型化请求委托给
`SubagentRelationshipLifecycleService`。`resume` 和 `fork` 只返回新分配的外部 ACP alias。
`delete` 返回 `{action, deleted}`，不暴露被删除的子会话 ID。内部会话 ID、提示词、子消息、事件、工具参数、
凭据、Provider 状态和文件系统路径绝不会写入 wire。

## 边界

该扩展不作为标准 ACP capability 宣告。它不会启动模型回合、重放工具、重建子上下文、调度或重试工作、
创建递归或并行子会话，也不会增加可写工具。现有 application 生命周期 owner 继续负责关系和任务终态校验。
ACP alias 分配是独立且有界的存储操作,不宣称与生命周期动作构成原子事务。

格式错误和不支持的字段会以稳定的请求错误失败关闭。取消仍保持传播,不会转换为协议成功响应。

## 结果

ACP、CLI 和 TUI 可以共享同一个类型化生命周期 owner,同时保持有意精简的 wire 投影。本阶段不改变 schema、
Provider、Runtime Kernel、Finalizer、模型循环或普通会话行为。
