# ADR 0107：有界长任务 Runtime 指引、压缩与分段

## 状态

已接受。

## 背景

普通 Agent 预算与批量仓库工具已经保持一致，但长回合仍需要三项相互衔接的能力：在预算耗尽前给出提示、
安全地自动缩减上下文，以及提供不会重置全局安全上限的可观察续段点。

仓库已经拥有所需的压缩规划器、严格无工具摘要生成器、持久化服务、Runtime 门控和持久化压缩条目。
再实现第二套压缩或第二套执行预算会造成所有权冲突。

## 决策

增加有界的 `ExecutionBudgetUsage` 投影，它只从现有 `ExecutionBudget` 和监督器实时计数推导。
70%、85% 和 95% 压力等级驱动仅请求可见的 `SyntheticReason.RUNTIME_BUDGET` 指引，以及安全的
`EXECUTION_BUDGET_UPDATED` 事件。TUI 只渲染 typed event，不计算或修改预算。

在具有持久化会话、已注入压缩门控和明确 Provider 上下文容量的生产 `FINALIZE_TERMINAL` binding 中，
`AgentLoopRunner` 只会在 `BEFORE_MODEL_REQUEST` 与 `AFTER_TOOL_BATCH` 评估自动压缩。第一次普通模型
完成前、模型请求或工具批次进行中、取消后以及容量未知时都不会压缩。摘要请求仍然只是一次有界的
`ModelToolPolicy.DISABLED` 调用，不消耗普通模型或工具计数。

规范 transcript 保持不变。最新且兼容的 `DurableCompactionItem` 只是请求投影：它仅替换经过指纹校验的
中间区间，并保留后续追加条目。候选边界绝不会拆开 assistant 工具调用与对应工具结果。Provider 来源变化
会使摘要请求失败，而不会把摘要保存到错误的上下文窗口下。已经压缩过的相同区间不会重复生成摘要；如果
该投影仍越过 hard threshold，回合会进入受控的
`BUDGET_LIMITED/CONTEXT_WINDOW_BUDGET` 最终化。

`ExecutionSegmentPolicy` 从全局 `ExecutionBudget` 推导观察阈值，但绝不替换或重置全局预算。在完整工具
批次边界，若已确认产生进展且全局预算仍有剩余，可发出一个有界的
`EXECUTION_SEGMENT_CHECKPOINTED` 事件，并为下一次请求注入一次
`SyntheticReason.RUNTIME_CHECKPOINT`。事件只包含计数、进展类别和计划步骤数量。它是可审计的回合内续段
标记，不是工作区 checkpoint，也不是进程崩溃恢复记录。

## 事务与失败边界

Provider 摘要生成和 `save_compaction_item()` 仍与之后的回合最终化事务分离。因此即使回合随后失败，已保存
压缩条目仍可能存在；未来投影会通过过期源校验安全失败。Provider 错误、存储错误和 `CancelledError` 继续
进入现有回合失败处理。已有的有界压缩超时会映射到受控的墙钟时间最终化。

## 影响

持续产生证据的长回合可以看到剩余预算、安全压缩并跨越有界观察 segment，同时不削弱唯一的全局硬上限。
合成指引、摘要、原始证据、指纹和工具参数都不会加入规范会话历史或预算事件。工具调用继续按顺序执行；
显式隔离的只读子代理继续保持调用方驱动的生命周期，本决策不增加自动委派。
