# ADR 0084：确定性上下文压缩评估契约

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DD

## 背景

Neuro Code 已经保存有序会话条目，并能够报告 Provider 用量或有界的本地估算。
但当前还没有 Provider 感知的上下文窗口契约、可持久化摘要条目或 Runtime 压缩循环。
在边界尚未定义前，直接把这些职责加入 `AgentRuntime` 会混合评估、总结、持久化和
Provider 回放。

## 决策

新增 `neuro_code.application.memory.compaction` 作为确定性的评估边界。
`ContextCompactionPlanner` 接收不可变用量快照和有序条目序列，返回只包含类型化计数、
token 阈值以及半开候选索引区间的 `ContextCompactionPlan`。

默认策略把已知容量的 80% 标为软阈值，把 95% 标为硬阈值。受保护前缀和可配置的近期后缀
绝不会进入候选范围。未知容量返回 `UNAVAILABLE`，不提出压缩建议。估算用量会作为元数据
保留，但不会被当成 Provider 精确用量。

## 边界

本切片不会总结或修改 `SessionItem`，不会在计划中保存提示词或工具输出，也不会写 SQLite
或会话条目。它不改变 `ModelProvider`、`ModelContext`、`AgentRuntime`、`Finalizer`、TUI、
CLI、ACP 或 Provider payload。后续切片必须先定义 Provider 专用摘要生成、系统/项目指令与
未解决工具状态的保留、Provider 亲和规则、可持久化压缩条目以及 Runtime 事务边界，之后才
能启用自动压缩。

## 验证

单元测试覆盖未知容量、软/硬阈值决策、受保护和近期保留、候选范围不足、输入不可变、计划
表示有界，以及用量/策略/计划不变量。架构和导入契约测试要求该 memory 模块继续作为这些
类型的唯一 canonical owner。
