# ADR 0086：Provider 感知的脱敏摘要输入

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DF

## 背景

Stage5DE 定义了绑定 Provider 的 `ContextSummaryRequest`，但其中仍没有源内容投影。下一条
接缝必须能够把输入安全地交给未来的总结器，同时不能把原始会话载荷复制进请求。消息可能
包含工具参数、推理、图片或凭据，保留的 Provider 状态也可能包含不透明的后端载荷。

## 决策

将 `ContextSummaryInputBuilder` 保留在 canonical application memory 模块中。它接受不可变的
`ModelContext` 和 `ContextSummaryRequest`，只投影选定的候选区间。每个
`ContextSummaryItem` 记录源索引、源类型、适用时的角色和有界文本投影。工具参数、保留载荷
与推理内容不被序列化，而是替换为固定标记。

Builder 先执行显式值与形状识别脱敏，再进行控制字符清理和 UTF-8 字节截断。此接缝注入与
Provider 无关的 token 估算器；最终输入受 `capacity_tokens - max_summary_tokens` 限制，并且
最多保留 128 个源条目、每条最多 4 KiB。结果只保存计数以及脱敏/截断标记；条目文本不会进入
结果对象的 repr。

## 边界

本切片不调用 Provider、不选择 tokenizer、不构建模型提示词、不修改 `ModelContext`、不持久化
压缩条目，也不接入 `AgentRuntime`。注入的 token 估算器只是明确的本地契约，不声称提供商精确
的 token 记账。Provider 摘要生成、可持久化压缩条目和 resume 重建仍属于后续能力。

## 验证

测试覆盖 secret 脱敏、工具/推理/保留状态投影、控制字符和字节上限、token 预算截断与省略、
估算器校验、上下文不可变性、repr 安全性以及类型化输入不变量。架构和导入契约测试确保这些
输入类型仍由 canonical memory 模块拥有。
