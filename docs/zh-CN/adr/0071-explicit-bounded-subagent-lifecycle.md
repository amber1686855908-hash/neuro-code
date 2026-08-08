# ADR 0071：显式且有界的子代理生命周期

[English](../../en/adr/0071-explicit-bounded-subagent-lifecycle.md) · **简体中文**

## 状态

Stage5CQ 接受。

## 背景

持久会话任务生命周期已经预留了 `SessionTaskKind.SUBAGENT`，但这个预留不能被误认为
可执行的子代理运行时. 在没有明确用户契约前，自动调度、继承父上下文以及第二套权限或
沙箱路径都不安全.

## 决策

新增 `SubagentExecutionService` 作为一次明确请求的子代理运行应用工作流边界. 该服务：

- 接受只包含父会话 ID、有界提示词和有界步数的 `RunSubagentRequest`；
- 在调用注入的 `SubagentExecutor` 前创建只含元数据的 `SUBAGENT` `SessionTask`；
- 恰好一次将任务标记为 `COMPLETED`、`FAILED` 或 `CANCELLED`；
- 原样传播执行器的结果、异常和取消，不做转换；
- 将请求和可选事件 sink 传给执行器，但不把提示词或输出写入任务记录.

执行器必须创建新的子运行时和上下文，不得隐式复用父会话. 能力选择、Provider、工具、
权限、沙箱和事件投影仍由执行器/组合根负责，本服务不会推断这些内容.

本服务是显式、由调用方驱动的. 本切片不包含队列、重试策略、自动调度、父上下文投影、
ACP 方法、CLI 命令或 TUI 命令. 同一个服务实例使用进程内锁串行调用；跨进程协调需要
后续存储契约.

## 边界

- 不修改 AgentRuntime 主循环或 ModelProvider 契约；
- 因为 `subagent` 已是规范任务类型且现有生命周期列足够，Stage5CQ 本身不需要 schema 迁移. 后续
  Stage5CR 的隔离运行时切片在 schema version 12 中单独增加父子链接表；该迁移不属于本生命周期决策；
- 新应用服务不持久化提示词、工具参数、凭据、原始输出或父 transcript；
- 执行器创建失败不会留下运行中的任务记录；
- 执行器失败不会被报告为成功的子代理完成.

## 放弃的方案

- 自动启动排队的 `SUBAGENT` 任务：这会在没有用户确认和资源策略的情况下把持久元数据
  变成隐式调度器；
- 复用父 `AgentConversation`：这会把子代理输出和预算混入父 transcript，破坏隔离；
- 增加第二套 Provider/工具协议：生命周期边界不应复制或弱化现有 Runtime 和 Provider 契约.
