# ADR 0008 — xAI Responses 原生回放

**简体中文** · [English](../../en/adr/0008-xai-responses-native-replay.md)

## 状态

已接受。

[ADR 0010](0010-provider-profiles-and-cc-switch.md) 对本决策作了扩展：适配器现已泛化为
通用 Responses，xAI 作为可选方言；本 ADR 继续约束 xAI 专属的原生回放行为。

## 背景

ADR 0007 为导入的 xAI 上下文提供了有用但有损的 Chat Completions 投影。Chat 无法
安全携带加密推理或完整的服务端工具项。固定 Rust 实现会改为通过 Responses API 回传
有序的推理和后端工具同级项，并把终态响应的 `output` 数组作为规范模型轮次。

xAI 把 Responses API 记录为首选 REST 接口。只有请求中包含
`include: ["reasoning.encrypted_content"]` 时才会返回加密推理，而且可以把它原样回传
以维持连续性。函数定义使用扁平 Responses 工具，函数结果是
`function_call_output` 输入项，流式文本使用 `response.output_text.delta`。不透明内容仅对
xAI 有意义，所以供应商标签或模型名不足以证明可以安全回放。

## 决策

新增显式供应商类型 `xai-responses`。它通过 `/v1/responses` 使用：

- `stream: true` 和有界 `max_output_tokens`；
- `store: false`，因为 SQLite 仍是规范的本地历史；
- `include: ["reasoning.encrypted_content"]`；
- 简洁推理摘要；
- 扁平函数工具 schema。

消息按原顺序投影为 Responses 消息、函数调用和函数调用结果输入项。经过校验的用户图片
使用原生 `input_image` 块；不支持的引用使用既有可见占位符。存在原生推理同级项时，
不会重复发送仅供显示的 assistant `reasoning_content`。

只有所有亲和检查均通过时才接纳原生保留项：

- 端点使用 HTTPS，不包含非默认端口、URL 凭据、查询或片段，并且主机名严格等于
  `api.x.ai`；
- 来源是固定 Rust 导入（`upstream-rust-import`）或此前的 `xai-responses` 会话。

推理输入保留 ID、摘要、可见内容和加密内容，但剥离仅供输出使用的 `status`。受支持的
网页搜索、X 搜索/自定义工具和代码解释器项保留其原生 JSON 与相对顺序。格式错误、未知、
类型不匹配或非亲和的保留项会被省略；普通消息仍可继续工作。自定义端点绝不会持久化
不透明终态项目，从而防止以后通过官方端点恢复时把不可信状态“洗入”xAI。

流式增量继续作为交互事件表面。终态 `response.completed` 或 `response.incomplete` 对象是
函数调用、用量、停止原因、规范 assistant 文本和持久化原生输出项的真值。
`ModelCompleted` 携带规范文本与有序 `PreservedContextItem`；`AgentRuntime` 把这些项目
插入 assistant 之前，并通过 `SessionStore` 提交完整序列。如果可见推理只出现在流式
增量中，它会修复仅含加密内容的推理项，或者创建合成的可见推理同级项，以匹配固定 Rust
回退规则。HTTP、协议、终态和函数参数错误都会限制长度并脱敏凭据。

## 影响

导入及新生成的 xAI 官方上下文现在可以跨本地工具循环、SQLite 往返、进程重启和
后续原生 Responses 请求继续存在，同时不会把不透明状态复制给 DeepSeek、Anthropic、
Gemini、自定义网关、不安全 URL 或仿冒主机。面向模型和持久化的真值改用终态文本，而
不是可能有损的 SSE 分块重建；UI 仍能即时获得增量。

该切片有意不使用 `previous_response_id`；服务端存储被禁用，每次都重新发送完整本地
上下文。压缩项、MCP/文件搜索输出保存、重试策略，以及需要凭据才启用的 xAI 在线夹具
仍属于后续切片。xAI 内置工具配置与生命周期归属由
[ADR 0009](0009-xai-hosted-tools.md) 单独规定。

## 参考

- [xAI Responses 文本生成](https://docs.x.ai/developers/model-capabilities/text/generate-text)
- [xAI 推理与加密内容](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [xAI 函数调用](https://docs.x.ai/developers/tools/function-calling)
- [xAI 多轮提示缓存](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/multi-turn)
- [xAI 上下文压缩](https://docs.x.ai/developers/advanced-api-usage/context-compaction)
