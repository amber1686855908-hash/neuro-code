# ADR 0102：上下文用量快照与过期源请求构造器

- 状态：已接受
- 日期：2026-08-08
- 范围：`neuro_code.application.memory.compaction_runtime`

## 背景

显式压缩所有者已经拥有安全的会话锁边界, 但调用方仍需自行组装
`CompactionContextUsage` 并计算源指纹。重复组装既可能偏离当前模型用量约定, 也容易在计划可执行时忘记过期源保护。

## 决策

新增两个无副作用的应用层辅助函数：

- `build_context_usage_snapshot()` 接收不可变 `ModelContext`、可选的
  `ProviderContextWindow` 以及可选的 Provider 输入/输出用量。当两个值都存在时遵循现有
  `CONTEXT_USAGE_UPDATED` 约定并记录输入加输出。输出缺失时保留输入值但标记为估算；输入缺失时使用有界的领域上下文估算器。未知 Provider 容量保持
  `capacity_tokens=None`, 不从 Provider 对象臆测。
- `build_explicit_context_compaction_runtime_request()` 只进行确定性的触发评估。计划可执行时, 它要求调用方拥有的 session/compaction 身份和时间戳, 根据精确上下文及候选区间计算指纹, 并返回携带保护值的请求。计划不可执行时, 它不伪造摘要或持久化元数据。

两个辅助函数都不会调用 Provider 或存储适配器, 不修改上下文, 不启动回合, 也不会启用自动压缩。现有门控和持久化服务仍会在执行时重新校验指纹, 会话回合锁继续作为并发边界。

## 后果

应用调用方可以用一个类型化入口构造精确或估算的用量, 同时保留已报告值与估算值的区别以及过期源保护。Provider 上下文窗口仍是显式配置；本阶段不修改 `ModelProvider` 协议, 也不声称所有 Provider 都能自动发现上下文容量。
