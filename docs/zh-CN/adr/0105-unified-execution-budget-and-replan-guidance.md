# ADR 0105：统一普通执行预算与临时 REPLAN 指引

## 背景

公开的 `--max-steps` 选项与 Runtime 硬模型步骤上限使用同一个值，但默认监督器仍保留
独立且固定的工具轮次与工具调用上限。因此，即使提高 `--max-steps`，更低的隐藏工具预算
仍可能提前终止任务。监督器也已经能够产生类型化 `REPLAN` 决策，但 Agent loop 只记录
该决策，下一次模型请求不会收到改变策略的原因。

## 决策

- `ExecutionBudget` 继续作为唯一的领域预算值。
- `neuro_code.application.execution_policy` 负责具名的 `normal` 与 `deep` 产品档位，分别
  解析为 48/48/192 和 96/96/384 的模型调用/工具轮次/工具调用上限。只读工具继承较宽的
  单工具上限；已知副作用工具和状态转换工具使用更严格的单工具上限。
- `ApplicationSettings`、CLI/TUI 启动、ACP 启动、Composition、`AgentRuntime`、
  `AgentLoopRunner` 与 `AgentExecutionSupervisor` 使用同一个解析结果。`--max-steps N`
  继续作为兼容选项，但现在映射为 N 次模型调用、N 个工具轮次和 4N 次总工具调用，不再
  只替换模型上限。
- Finalizer 的 `max_attempts` 继续独立管理，绝不从普通执行预算中预留。
- 在 `FINALIZE_TERMINAL` 模式下，工具批次结束时的 `REPLAN` 决策会启用一条标记为
  `SyntheticReason.RUNTIME_SUPERVISION` 的请求范围合成消息。该消息只在内存中重建，绝不
  持久化或投影为真实用户回合；监督器继续要求重规划时它保持生效，产生新进展或回合结束
  时即清除。`OBSERVE_ONLY` 仍然不执行任何决策。
- `ContextBuilder` 为每次模型请求加入与 Provider 无关的 batch-first 指引，鼓励批量请求
  互不依赖的只读证据，同时明确允许存在数据依赖的顺序操作。

为了内部兼容，直接构造且不提供预算的 `AgentRuntime` 仍使用历史 24 步默认值，但该值也
会映射为完整的 24/24/96 普通预算。正式产品入口默认使用 `normal` 档位。

## 影响

产品预算默认值只有一个 owner；提高 `--max-steps` 时不再存在隐藏的固定工具上限。重复
动作、重复错误、周期循环与无进展检测保持不变。工具调用仍按顺序执行；本决策不新增批量
文件系统工具、预算 telemetry、自动上下文压缩、分段续跑或子代理调度。
