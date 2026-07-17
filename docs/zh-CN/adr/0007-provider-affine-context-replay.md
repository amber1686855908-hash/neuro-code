# ADR 0007 — 供应商亲和的保留上下文回放

**简体中文** · [English](../../en/adr/0007-provider-affine-context-replay.md)

## 状态

已接受。

## 背景

Rust 会话导入器和 SQLite 存储已经按原顺序保留 `Reasoning` 与
`BackendToolCall` 项，但恢复会话此前只把普通消息投影载入 `AgentRuntime`。因此，这些
数据虽然能够经过导入和导出，却不会进入后续模型请求。

这些载荷并非供应商中立。固定 Rust 源码会把 Responses API 推理和后端工具作为原生
输入项往返；旧 Chat Completions 转换则把可见推理折叠到后续 assistant，并把后端工具
替换为人类可读的 assistant 摘要。xAI 文档同样把加密推理定义为可回传以维持对话连续性
的不透明状态，并说明供应商生成的加密内容只对 xAI API 有意义。因此，把这些载荷发送
给其他供应商、网关或模型家族并不安全。

## 决策

模型端口接收 `ModelContext`，其中包含完整有序 `SessionItem` 序列以及源会话供应商和
模型。CLI 恢复会话时加载该规范序列；`AgentRuntime` 在其后追加新消息，并把它原样
传入每个模型步骤。应用结果视图继续保留独立的普通消息投影。

最终投影由适配器负责。Anthropic、Gemini 和非亲和 OpenAI 兼容目标只使用普通消息。
Chat Completions 适配器只有在下列条件全部满足时，才启用导入的可见上下文回放：

- 源供应商标记为 `upstream-rust-import`；
- 目标 URL 使用 HTTPS，不含非默认端口、URL 凭据、查询或片段，并且主机名严格等于
  `api.x.ai`。

对于亲和请求，连续的可见推理项会按源顺序折叠到后续 assistant。后端网页搜索、X
搜索和代码解释器调用会转换为有界的人类可读 assistant 摘要，并且不会中断推理折叠。
中间出现 system、user 或工具结果时会清除待处理推理；孤立和格式错误的项目会被省略。

加密推理、原始后端工具载荷、ID、状态字段和输出绝不会通过 Chat Completions 发送。
精确的原生回放交给专用 Responses API 适配器，现由
[ADR 0008](0008-xai-responses-native-replay.md) 规定。自定义网关默认属于非亲和目标，
因为本地无法证明端点所有权。

## 影响

导入的上游会话恢复到 xAI 官方 Chat 端点时，可以重新获得有用的可见上下文，
同时不会把不透明供应商状态暴露给 DeepSeek、Anthropic、Gemini、仿冒主机、不安全
端点或自定义网关。SQLite 只追加前缀保护和 schema-v2 导出保持不变。

该降级投影改善语义连续性，但不主张实现字节稳定的 Responses 回放或完整提示缓存
对齐。独立的 `xai-responses` 适配器现在覆盖加密推理和受支持服务端工具项的本地无状态
回放；有状态 response ID 和压缩项仍属于后续工作。

## 参考

- [xAI 推理与加密内容](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [xAI 多轮提示缓存](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/multi-turn)
- [xAI 上下文压缩](https://docs.x.ai/developers/advanced-api-usage/context-compaction)
