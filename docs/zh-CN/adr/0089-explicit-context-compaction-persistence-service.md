# ADR 0089：显式上下文压缩持久化服务

- 状态：已接受
- 日期：2026-08-08
- 范围：application memory 与 SessionStore 边界

## 决策

新增 `ContextCompactionApplicationService` 作为显式应用用例。它从不可变的
`ModelContext` 构建脱敏的 `ContextSummaryInput`，在模型请求前校验调用方提供的源指纹，
调用已有的 `ProviderContextSummaryGenerator`，通过 `build_durable_compaction_item` 将有界结果转换为
持久化条目，并经由规范的 `SessionStore.save_compaction_item` 端口保存。

请求携带不透明的预期源指纹和由调用方选择的 compaction ID。源条目数量或指纹变化时，会在调用
Provider 之前失败。重复 ID 的语义由存储适配器负责：相同记录保持幂等，冲突记录失败关闭。

生成和持久化刻意保持为两个独立操作。Provider 请求不属于保存条目的 SQLite 事务；只有存储端口
成功返回后服务才报告成功，并且 Provider 错误、取消和存储错误都会原样传播，不进行重试或回退。

## 边界

- 不接入 `AgentRuntime`，不启用自动压缩。
- 不新增事件、session item、导入/导出记录或 UI 投影。
- 原始上下文、提示词、工具参数、凭据和源摘要不会发送给 Provider，也不会进入结果表示。
- 持久化条目保持与 Provider 无关；恢复时仍必须通过现有源范围和亲和性校验。

## 原因

该服务是窄的应用编排边界，而不是新的存储实现。它先明确过期源校验以及模型请求与写入之间
独立的事务边界，再考虑未来 Runtime 触发器。
