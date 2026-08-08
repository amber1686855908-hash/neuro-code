# ADR 0072：隔离的只读子代理运行时

[English](../../en/adr/0072-isolated-read-only-subagent-runtime.md) · **简体中文**

## 状态

Stage5CR 接受.

## 背景

Stage5CQ 围绕注入的 `SubagentExecutor` 定义了明确的应用生命周期，但还没有提供具体的子运行时.
真正的第一版子代理切片需要能够用于仓库研究，同时严格窄于父 Agent. 它还必须能在重启后被检查，
且不能把提示词、凭据、工具参数或原始模型输出放进归属元数据.

## 决策

新增 `IsolatedSubagentExecutionService` 和由组合根拥有的
`CompositionReadOnlySubagentRuntimeFactory`，用于一次明确、同步的只读运行：

- 每次请求创建全新的子会话和只含元数据的父 `SUBAGENT` 任务.
- 持久 `SubagentLink` 只保存父会话/任务 ID、子会话 ID 和带时区的创建时间. 链接在子运行时启动前写入.
- 子 Agent 使用全新的 `AgentConversation` 绑定. Provider profile 会被复制并移除 Provider 内置工具，
  工具注册表限制为 `read_file`、`list_dir`、`grep` 和 `skill`.
- 可写文件系统工具、Bash、客户端终端、后台任务、自动唤醒、调度和递归创建不提供给该组合根工厂.
- 子 Agent 只接收新的提示词. 父消息和可变的父上下文不会复制到子运行时.
- 子模型步数由 `RunSubagentRequest.max_steps` 限制，执行还具有由
  `MAX_SUBAGENT_TIMEOUT_SECONDS` 限制的有限墙钟时间.
- 取消会在受 shield 保护的运行时清理和持久化父任务 `CANCELLED` 后继续传播. 超时转换为类型化的
  `SubagentTimeoutError`；Provider/运行时故障仍然是失败.
- 删除父会话时，外键关联的 `subagent_links` 会递归删除关联子会话.

该服务仍然是显式、由调用方驱动的. 它不会接入普通 `AgentRuntime` 循环、CLI、TUI、ACP、自动调度器，
也不会把结果投影到父 transcript.

## 持久化与事务边界

schema version 12 新增 `subagent_links`，使用复合父会话/任务键和唯一子会话 ID. 保存一条链接是一个
SQLite 写事务，并校验父任务类型/状态和子会话存在性.
这不表示子会话创建、链接持久化、模型执行、任务完成和会话事件属于一个跨进程事务；运行时只保证在
子执行前尝试持久化链接.

## 能力与安全边界

子 Agent 能力集是父组合中可用基础设施的固定子集. 工厂不会授予新的权限或沙箱绕过，且在 Provider
构造前移除 Provider 内置工具. 只读注册表过滤是组合约束，不能替代工具、工作区、权限或沙箱检查.

## 放弃的方案

- 复用父会话会混合 transcript、预算和 Provider 上下文.
- 传递完整父工具注册表会使只读契约变成偶然行为，并允许可写工具.
- 创建第二套 Provider 或权限协议会重复现有基础设施边界.
- 自动启动子任务、重试，或暴露 CLI/TUI/ACP 命令，会在生命周期尚未验证前扩大为调度和产品策略.
