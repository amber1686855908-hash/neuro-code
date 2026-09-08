# ADR 0157：结构化 BOUNDED_SWARM parent verification

- 状态：Accepted
- 日期：2026-09-08
- 范围：VF-4c 结构化 UltraCode parent verification

## 背景

VF-4a 为 `MAIN_MAX` 固定了 verification requirements，VF-4b 则从 Result Adoption 暴露了 durable
`parent_workspace_changed` fact。结构化 `BOUNDED_SWARM` path 在采纳 worker output 后仍然停止，不能把真实
parent workspace 交给普通 verification 与 final-response boundary。Lower Swarm result 只是 orchestration evidence，
不是 parent user turn 的已验证回答。

## 决策

结构化 `BOUNDED_SWARM` 使用既有 application-owned `NormalTurnRequirementsPolicy`、`VerificationTracker`、
`AgentRuntime` 和 VF-2 final-response contract。新的 request 缺少声明时只解析一次；显式非空和空 snapshot 保持
完全一致。Effective snapshot 存储在既有 schema-30 Ultracode projection 与 parent `TurnInput` 中。已持久化的结构化
execution 必须复用该精确 snapshot；Legacy NULL snapshot 继续保持 Legacy，冲突的 mode 或 snapshot identity fail closed。
不增加 schema-31 migration。

Durable Ultracode row 是 orchestration identity。与 Legacy path 不同，结构化 execution 不会预先创建 parent attempt，
也不会提交 lower Swarm response。它运行 canonical bounded Swarm，通过 Result Adoption durable 采纳结果，然后使用
原始 prompt、parent turn ID、execution ID 和精确 requirements snapshot 启动真实 parent `AgentRuntime`。如果 adoption
报告 `parent_workspace_changed`，就将稳定的 `adoption_id` 作为 seed 传入；parent tracker 在第一个 model step 前记录
一次 workspace mutation。Tracker 仍是 generation 唯一 owner；worker requirements 和 worker verification evidence 都不会
传播到 parent。

Adoption 完成后，parent runtime 拥有后续所有 tool、verification、finalization 和 committed response。Conflict 或
indeterminate adoption 是 orchestration failure，不是 verification `FAIL`；它只能通过 parent-owned、有界且 truth-safe
deterministic fallback 结束。Lower Swarm response 绝不会被当作 parent verified truth。只有 parent completion path 会更新
Ultracode final response 和 result fingerprint。

## 恢复与兼容性

Recovery 使用精确 durable Swarm、adoption、parent-attempt、TurnInput 和 committed-response identity。只恢复 safely
retryable 的 parent attempt；已提交 parent 直接 replay，不调用 Provider、verification 或 Finalizer，也不创建第二个 turn
或 assistant item。Parent attempt 之前发生 crash 时，durable orchestration state 仍可恢复；若已观察到 parent output 或存在
未解决且不可 retry 的 attempt，则 fail closed。既有 `FINALIZING` state 继续表示等待 parent completion 的边界。

带 NULL verification snapshot 的 Legacy `BOUNDED_SWARM` execution 继续使用既有 external-result 行为。`MAIN_MAX`、
Swarm/worker/Planner/Leader/DAG/Result Adoption owner、permission 与 sandbox 边界，以及 CLI/TUI/ACP projection 保持不变。
不增加 worker verification import、requirement inference、test discovery、TestRunner、framework detection、NLP inference
或 public verification UI。

本切片完成规划中的 Verification Foundation 序列。后续工作属于产品能力或 stabilization，不再建立新的
verification-foundation integration boundary。

## 验证

Focused unit、recovery 和 production-shaped composition tests 覆盖新的结构化 request、显式 snapshot mode、adoption 成功与
失败、parent mutation seed、精确 retry/recovery、已提交 parent replay、Legacy BOUNDED_SWARM 兼容性以及未改变的 MAIN_MAX 行为。
