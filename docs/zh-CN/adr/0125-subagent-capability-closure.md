# ADR 0125 — 子代理 capability 闭包

[English](../../en/adr/0125-subagent-capability-closure.md) · **简体中文**

## 状态

已接受，适用于当前 pre-alpha Runtime。

## 背景

Neuro Code 当前有两类 child runtime 工作流：scope-aware scheduler 和显式只读子代理服务。
Scheduler 已经会把 child request 与 parent/global capability manifest 解析；但显式服务过去会从
root composition 重新构造只读 manifest，因此无法从架构上证明嵌套 child 始终不超过其实际 parent binding。

持久化子代理关系的 `resume`、`fork` 和 `delete` 是 session lifecycle 操作，不会重新构造 child
`AgentRuntime`。旧版任意注入 `SubagentExecutor` 的接缝仍可供既有测试使用，但它不是
capability-aware 的生产边界。

## 决策

- 生产 child 创建的 canonical parent authority 是 `ConversationBinding.capabilities`。无头 CLI 命令会
  创建 parent binding；TUI 读取活动 binding；ACP 要求活动 parent binding。缺少 metadata 时失败关闭。
- 两条 child workflow 都使用由 composition 拥有的 global capability ceiling。显式服务先把固定只读工具名
  转换为 request，然后在创建 child task 或 binding 前调用
  `SubagentCapabilitySet.resolve_child(parent, requested, global_policy)`。
- 解析得到的不可变 manifest 同时传给 child factory 和
  `ApplicationComposition.create_binding(capabilities=...)`。Binding 构造会重新计算实际 manifest 并拒绝
  不一致；执行前还会校验 runtime fingerprint。
- `READ_ONLY_SUBAGENT_TOOL_NAMES` 只是 request policy，不是 authority。它会与 parent manifest 求交，不能
  恢复工具、工作区根、沙箱强度、MCP、terminal 或 network 能力。
- `SubagentExecutionService` 及其任意 executor factory 作为显式标记的测试/内部兼容接缝保留。组合根拒绝
  普通生产绑定该接缝。
- 子代理关系的 resume/fork/delete 继续只负责 lifecycle。普通 ACP session fork 是独立 session binding，
  不是递归子代理构造。

## 结果

本决策证明的 invariant 仅是：

> 任意生产 child runtime capability 都不宽于其实际 parent capability。

这不等于完整证明 Permission、Workspace、操作系统 Sandbox、MCP transport、Provider transport 或整个
Agent security system。显式 child workflow 仍是同步只读；自动委派和不受限 child 工具不属于本决策。
