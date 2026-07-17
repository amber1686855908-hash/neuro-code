# ADR 0006 — 思考模式工具调用连续性

**简体中文** · [English](../../en/adr/0006-thinking-tool-continuity.md)

## 状态

已接受。

## 背景

规范化供应商流已经把推理增量暴露为运行时事件，但代理在构造对应 assistant 消息时
会丢弃这些增量。对于把思考模式推理视作 assistant 轮次组成部分的供应商，这种行为
并不充分。DeepSeek V4 Chat Completions 会在 `reasoning_content` 中返回该状态，并
要求下一次请求随 assistant 工具调用消息完整带回该值。丢失它会使原本有效的多步骤
工具循环失败。

从 Rust 会话导入的不透明推理面临另一个问题：当前记录的供应商、模型和线上格式亲和
信息还不足以让它安全发送给任意活动供应商。因此，它必须与当前规范化运行时新生成的
推理保持独立。

## 决策

`Message` 新增可选 `reasoning_content` 字段；该字段只允许用于 assistant 角色，且
不得为空。`AgentRuntime` 会拼接一次模型步骤中的全部 `ModelReasoningDelta`，并把
结果保存在该步骤的 assistant 消息中。SQLite 持久化和 JSON 导出继续使用现有消息
序列化路径，因此恢复工具循环时可以保留该值，无需迁移数据库 schema。

OpenAI 兼容适配器只会在序列化同时包含工具调用的 assistant 消息时加入
`reasoning_content`，不会在后续请求中回传已经完成且没有工具调用的推理。配置项
`max_output_tokens` 会作为 Chat Completions 的 `max_tokens` 发送，使该适配器获得与
Anthropic 和 Gemini 原生适配器相同的显式响应上限。

`PreservedContextItem` 默认不会投影到该字段。[ADR 0007](0007-provider-affine-context-replay.md)
只允许在严格的 xAI/来源亲和契约下使用可见的导入推理；Responses 原生加密状态仍保持
独立。真实凭据探针保持选择性启用，并位于仓库和 CI 之外；应用程序只读取指定的进程
环境变量，绝不会自动解析项目 `.env` 文件。

## 影响

新生成的思考模式工具调用及本地恢复会话现在可以通过规范化代理运行时完成所需的推理
往返。应用控制流仍把推理视为不透明数据，但它现在会存入本地会话并出现在 JSON 导出
中，因此会话文件与导出文件必须按可能包含敏感数据来保护。

该变更不主张导入的 Rust 推理具备可移植性，也不会把推理回放扩展为所有供应商的通用
行为。一次 DeepSeek V4 Flash 手动探针已经验证当前 OpenAI 兼容流式行为，以及只读
`AgentRuntime`/SQLite 往返。xAI 原生本地无状态回放现在由独立的
[ADR 0008](0008-xai-responses-native-replay.md) 路径实现；可长期保留的选择性集成夹具仍
属于后续工作。
