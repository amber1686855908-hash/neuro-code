# ADR 0073：有界只读子 Agent 结果投影

[English](../../en/adr/0073-bounded-read-only-subagent-result-projection.md) · **简体中文**

## 状态

Stage5CS 已接受。

## 背景

Stage5CR 提供了明确的隔离只读运行时，但内部的 `SubagentRunResult` 仍然包含完整的
`AgentRunResult`。该值适合应用工作流内部使用，却不适合作为调用方的应用入口，因为它会带出消息、
会话项、事件以及未经投影的子响应。

## 决策

在应用工作流中增加 `ReadOnlySubagentApplicationService` 和
`SubagentResultProjection`：

- 调用方明确提交现有的有界 `RunSubagentRequest`。
- facade 只委托一次隔离运行；不调度、不重试，也不追加父 transcript。
- 结果必须包含持久化的父子 `SubagentLink`，且 child session ID 必须匹配。
- 返回投影只包含父 session ID、任务 ID、child session ID、任务终态、有限步数、可选的类型化执行结果以及脱敏响应。
- 不通过该边界返回消息、会话项、事件、工具参数、凭据、快照或原始模型上下文。
- 先脱敏，再按 UTF-8 字节数有界截断；截断结果明确且确定性。

组合根提供配置中的脱敏值，并暴露该应用服务的工厂。本阶段不增加 CLI、TUI、ACP、AgentRuntime、
自动调度、可写工具或父上下文接入。

## 拒绝的方案

- 直接返回 `AgentRunResult` 会向每个调用方泄漏过宽的子会话 transcript 投影。
- 将投影写入父会话会把子输出混入父对话，并产生新的 transcript 所有权契约。
- 静默接受缺失或不匹配的父子链接会让重启后的结果无法审计。
