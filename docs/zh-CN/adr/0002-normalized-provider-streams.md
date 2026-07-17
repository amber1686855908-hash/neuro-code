# ADR 0002：统一提供商流，并保留不透明的往返状态

**简体中文** · [English](../../en/adr/0002-normalized-provider-streams.md)

- 状态：已接受
- 日期：2026-07-17
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

OpenAI 兼容的 Chat Completions、Anthropic Messages 和 Gemini `streamGenerateContent` 使用不同的消息结构、流事件名称、工具调用标识、结束原因和 token 用量字段。如果这些载荷泄漏到代理循环中，每项应用能力都会与每个提供商耦合。部分 API 还会返回必须随相关函数调用原样传回的不透明值，例如 Gemini 的 thought signature。

## 决策

每个提供商拥有原生适配器：将统一的 `Message` 和 `ToolDefinition` 转换成对应线格式，并产生共享的 `ModelEvent` 联合类型。运行时只处理文本、推理、完整工具调用和完成事件。后续请求所需的提供商专用值，存入 `ToolCall.metadata` 中以提供商命名空间区分的键下，由规范会话存储持久化，并在其他位置视为不透明数据。

HTTP 失败、畸形流、提供商错误事件、不完整工具调用和被阻止的提示，都转换成不会泄露凭据的 `ProviderError`。未知流事件会被忽略，使协议的增量扩展不会中断当前轮次。

## 影响

- 代理循环和 UI 保持与提供商无关。
- 可以在适配器内部添加原生 API 能力，而无需修改端口。
- 会话持久化必须精确保留工具元数据。
- 跨提供商恢复会话时可以忽略外来的元数据，但不得修改或执行它。
- 适配器从 `partial` 升级为 `compatible` 之前，必须具备提供商契约夹具和选择性在线测试。
