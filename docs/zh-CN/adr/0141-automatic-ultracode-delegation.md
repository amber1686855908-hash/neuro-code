# ADR 0141 — Automatic Ultracode 委派与编排入口

**简体中文** · [English](../../en/adr/0141-automatic-ultracode-delegation.md)

## 状态

已接受，适用于第一版有界纵向切片。

## 背景

`max` 是 Neuro Code 最深的普通单智能体 reasoning/review 策略。原有
`ultracode` 只会投影到该策略，并不拥有工作流。仓库现在已有冻结的有界 Agent Swarm，
但它的组合 service 明确是内部 application seam，不能复制成第二个或暴露成无界编排器。

因此第一版 Ultracode 需要增加一个显式的应用层入口，同时保持普通 effort 行为、供应商
中立性、精确回合恢复，以及既有 Conversation、Planner、Leader、Task DAG、Writable、
Worktree、Checkpoint、Relay 与 LSP service 的 authority boundary。

## 决策

只有请求 effort 为 `ULTRACODE` 的用户回合才进入 Ultracode 委派服务。普通的 `low`、
`medium`、`high`、`xhigh` 与 `max` 继续使用既有普通 ConversationRunner 路径。入口使用
一个小型确定性本地策略，只做一个 typed decision：

- `MAIN_MAX`：使用普通 `max` 语义执行既有 parent 单智能体 runtime；
- `BOUNDED_SWARM`：调用一次既有的
  `ApplicationComposition.create_agent_swarm_service()`。

该策略不调用 model classifier，不能选择 tool、worker、capability、root、sandbox、network、
MCP、DAG definition、retry、merge、checkpoint 行为或 provider credential。它是应用层策略，
不是供应商原生 reasoning level。

当前策略明确只是有界的确定性启发式：它匹配用户提示中的固定并行、拆分、跨文件和研究标记，
不是语义上的任务复杂度分类，也不声称具备 model-level routing intelligence。交互式 TUI 无论
初始 effort 是什么都会绑定一个 dormant application entry；`SessionTurnService` 在每个用户回合
检查 controller 当前 effort，因此运行时 `max` ↔ `ultracode` 切换不需要重建 service，也不会留下
过期的 entry seam。

有界 Swarm objective contract 只有一个由 domain 拥有的 canonical 边界：
`MAX_SWARM_OBJECTIVE_BYTES`，当前为按 UTF-8 字节计算的 4 KiB。Swarm request validation 与该
routing policy 使用同一个边界。超过该边界且包含 marker 的 Ultracode prompt 会在 durable
Ultracode branch claim 之前选择 `MAIN_MAX`，不会进入必然拒绝它的 Swarm request。这是决策前的
routing rule，不是 branch claim 后失败时的 fallback；恢复仍会复用已有的 durable decision。

## Durable identity 与生命周期

Session schema 28 增加 insert-once 的
`orchestration_ultracode_executions` projection。不可变 identity 绑定：

- 实际 parent session 与精确 parent turn；
- input 与 context fingerprint；
- provider、model 与 context-affinity provenance；
- 一个 `MAIN_MAX` 或 `BOUNDED_SWARM` decision；以及
- 一个下游 identity：`MAIN_MAX` 的精确 parent turn execution，或
  `BOUNDED_SWARM` 的确定性 `swarm_run_id`。

状态机为：

```text
DECIDED -> MAIN_MAX_RUNNING -> COMPLETED
DECIDED -> BOUNDED_SWARM_RUNNING -> FINALIZING -> COMPLETED
                                      \-> INDETERMINATE
                         任一已拥有 branch \-> INDETERMINATE
```

SQLite `BEGIN IMMEDIATE`、process-liveness ownership、不可变 identity 校验和 generation CAS
让 decision 与 owner 可恢复。新进程会复用精确 durable decision，不会再次分类同一 prompt，
也不会创建替代的下游 identity。

## Branch 与 result 边界

`MAIN_MAX` 使用精确的 `turn_id` 与 `ultracode_execution_id` 调用既有 parent
`ConversationRunner`。供应商投影在显式 adapter 支持时可以是原生 `max`，否则和之前一样
省略该字段；供应商永远不会收到编造的 `ultracode` 值。

`BOUNDED_SWARM` 使用一个 durable `swarm_run_id` 调用既有有界 Swarm。Router 不创建第二个
Planner、Leader、Task DAG、Writable worker、Worktree、Checkpoint、LSP manager 或 relay。
下层 progress 不复制进 parent transcript；parent 只可见有界委派 progress 与最终 Swarm result。

两个 branch 都使用既有 external-turn finalization contract 写入一个 parent 可见的 assistant
result。恢复时只依据精确的
`(session_id, turn_id, ultracode_execution_id)` event evidence 匹配已提交 result。精确已提交
result 是幂等的，不会追加第二条 assistant message。

## No double execution 与恢复

Router 永远不会同时启动两条 branch，也不会把失败或 indeterminate branch 静默切换到另一条。
已有可观察 output 的 parent attempt 会被复用，绝不 replay。如果下层 Swarm 已发布精确的
durable identity，恢复可以继续该同一个 Swarm；如果 parent attempt 开放但没有该下层 identity，
则 fail closed。若在 Ultracode bookkeeping 完成前观察到下层 terminal result 或 parent result，
恢复会经过 `FINALIZING` 并以幂等方式完成提交。

原始 durable-state fresh-process matrix 使用 `multiprocessing.get_context("spawn")` 与 `os._exit` 覆盖：

- A：下游 branch 启动前 decision 已持久化；
- B：Ultracode 完成前已经观察到 `MAIN_MAX` model output；
- C：Ultracode branch link 前精确的 durable Swarm run 已存在；
- D：parent assistant commit 前 Swarm 已 `COMPLETED`；以及
- E：Ultracode 终态 bookkeeping 前 parent result 已提交。

每个 case 都证明不会 replay decision、切换 branch、进行第二次 Provider execution、创建重复
Swarm identity 或写入重复的 parent-visible assistant result。该 matrix 直接测试 durable lifecycle
seam，本身不等同于完整 `ApplicationComposition` process-death proof；另有两个独立的 production-
composition acceptance，通过 fresh composition 和真实下游路径覆盖 MAIN_MAX 与 BOUNDED_SWARM 边界。

## 安全、兼容性与非目标

实际 parent `ConversationBinding` 继续作为 capability ceiling。入口不拥有 filesystem、Bash、
LSP、MCP、network、Worktree、Checkpoint 或 Writable authority。Routing text 只作为 evidence，
不能改变这些边界。CLI 与 TUI 可以进入这个内部 service；本切片不为 ACP 增加 effort surface。

本切片不增加普通 effort 的自动 Swarm、recursive Ultracode 或 Swarm、generic retry、result
adoption、merge/copy-back、cherry-pick、patch adoption、public Swarm dashboard、remote/cloud
execution、marketplace integration，也不重新实现 Checkpoint/Rollback。

## 验证

Focused 与 production-shaped tests 覆盖两个 branch、provider wire 中立性、insert-once/generation
fenced persistence、schema 27 到 28 migration、精确 replay、取消/失败不 fallback、原始
fresh-process A–E matrix、同一个长生命周期 turn service 的动态 effort 切换，以及两个代表性的
full-composition process-death acceptance：MAIN_MAX result handoff 与已完成 Swarm handoff。只有
完整 lint、类型、文档、coverage、build 与回归 gates 都通过后，才能把本切片评级为 proven。
