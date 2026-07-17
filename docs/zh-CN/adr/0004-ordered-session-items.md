# ADR 0004 — 保留有序会话项

**简体中文** · [English](../../en/adr/0004-ordered-session-items.md)

## 状态

已接受。

## 背景

Grok Build v1 JSONL 并不是简单的聊天消息列表。它在普通消息之间穿插 Responses API
推理记录和后端已执行的工具调用记录；用户消息和工具结果也可以包含有序图片内容项。
把这些值压平成字符串会丢失供应商上下文、记录顺序和图片身份，而让代理循环直接处理
供应商载荷又会把运行时耦合到单一 API。

## 决策

持久化会话采用名为 `SessionItem` 的有序联合类型：

- `Message` 继续作为独立于供应商的运行时契约，并可包含有序文本/图片 `ContentPart`；
- `PreservedContextItem` 保存经过校验、深度不可变的推理或后端工具 JSON 载荷，不在
  领域层解释供应商专属字段。

`SessionSnapshot` 持有完整会话项序列，并通过 `messages` 属性提供过滤后的普通消息。
`SessionStore.load_session_items` 服务于导出和迁移路径，`load_messages` 服务于代理运行时。
现有 SQLite 纯消息 JSON 仍可读取。存在保留上下文时，`save_messages` 只接受已有消息
前缀和新增消息，防止恢复运行静默重排或删除导入项。

JSON 会话导出升级为格式版本 2，同时包含 `messages` 和 `conversation_items`。供应商
原生图片回放作为独立适配器决策记录在
[ADR 0005](0005-provider-native-image-replay.md) 中；保留的供应商上下文记录仍不进入
规范代理消息投影。

在 Rust 导入边界，assistant `raw_output`、单体 `reasoning` 或 v0
`reasoning_content` 中的旧上下文会被提升到 assistant 之前。读取流中此前出现的独立
后端工具 ID 会抑制对应内嵌副本，而推理项绝不会被合并。无效内嵌条目与无效 JSONL
整行会分别统计，未知类型也会与损坏载荷分开报告。

## 影响

Rust 推理、后端工具载荷、图片 URL 及其相对顺序可以跨导入、SQLite 往返、恢复和导出
保留下来。无头运行时和现有供应商适配器继续使用规范 `Message` 接口。不透明上下文
必须始终是经过校验的 JSON；需要编辑或回放它的功能必须新增显式类型适配器，而不能
让应用层任意解析字段。
