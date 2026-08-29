# ADR 0144：Automatic Ultracode 结果采纳集成

- 状态：Accepted
- 日期：2026-08-29
- 范围：Neuro Code v1 有界本地 vertical slice

## Context

ADR 0141 定义了显式的 `ULTRACODE` application entry。它只选择一个有界本地分支：既有普通
`MAIN_MAX` 路径，或既有 `BOUNDED_SWARM` 组合。ADR 0143 定义了内部 Result Adoption
核心，但有意没有接通组合层 seam。

本 ADR 只收口这个 seam。不重新设计 Ultracode、Agent Swarm、Task DAG、Leader、Writable
Subagent、Worktree、Checkpoint、Permission 或 Sandbox，也不增加通用 merge 或 copy-back engine。

## Decision

只有 reasoning effort 为 `ULTRACODE` 的显式用户回合可以进入 automatic integration。现有普通 effort（包括
`max`）继续使用普通 `ConversationRunner` 路径。

### `MAIN_MAX`

`MAIN_MAX` 调用既有 parent conversation runner，并执行零次 Result Adoption 构造、读取、Plan、target 写入或
provider 调用。既有 parent finalization contract 保持不变。

### `BOUNDED_SWARM`

有界分支复用既有 Planner、Task DAG、Leader、Writable Subagent、Worktree、Checkpoint 与 Agent Swarm service。
Swarm 持久化产生一个 canonical terminal `AgentSwarmResult` 后，application 执行：

1. 从 durable Ultracode execution identity 与精确 Swarm run identity 推导一个确定性的 adoption identity。
2. 把精确的 `AgentSwarmResult` 与实际 parent `ConversationBinding` mutation authority 传给 typed internal
   Result Adoption service。
3. 要求 Result Adoption 先达到 `COMPLETED`，之后才能发布 parent success。
4. 使用 bounded final response 持久化 Ultracode `FINALIZING`。
5. 通过既有 exactly-once conversation contract 提交 parent external turn。
6. 只有 parent commit 成功后，才持久化 Ultracode `COMPLETED`。

Swarm result 作为 typed durable evidence 传递。Response text、model instruction、Leader text、worker text、
`git diff` 或 model 提供的 file list 都不能替代它。Adoption 是内部 application action，不是 model tool call，
也不是第二次 provider turn。

### Deterministic identity

Adoption ID 为：

```text
adopt- + SHA256(execution_id + NUL + swarm_run_id)[:48]
```

它不使用 model、Planner、Leader、Worker、timestamp、random UUID 或 latest-row lookup 输入。重新进入时复用同一
精确 identity 与精确 Swarm result。

### Adoption non-success

`CONFLICT`、`FAILED` 与 `INDETERMINATE` 都是 parent 可见的有界结果。Response 包含 adoption ID、terminal state、
applied/unresolved/conflict count，以及是否可能发生 parent partial mutation。集成永远不会 fallback 到 `MAIN_MAX`、
重跑 provider 或 Swarm、让 model merge、覆盖冲突 image，或静默宣称成功。

### Process-death recovery

集成保留以下 fresh-process boundary：

- A：lower Swarm 已为 `COMPLETED`，而 Ultracode 仍为 `BOUNDED_SWARM_RUNNING`；恢复读取精确 result，继续同一个
  adoption，不重放 Planner、Leader、Worker、Swarm 或 provider work。
- B：adoption 已为 `COMPLETED`，但 parent turn 尚未提交；恢复复用 terminal adoption，只提交一次 parent，不产生
  adoption 写入或新 Plan。
- C：adoption 为 `INDETERMINATE`；恢复暴露该有界状态，不覆盖、不 retry、不重放 Swarm/provider。
- D：adoption 在 mutation 前为 `CONFLICT`；恢复暴露 conflict，不产生 mutation、新 adoption 或 rerun。

Parent commit crash recovery 仍由既有 conversation finalization contract 负责。已提交的 parent turn 通过精确 identity
复用。

### Permissions and progress

Adoption 使用活动 parent binding 既有的 workspace mutation、permission/scoped approval、workspace/instruction、
sandbox 与 exact-file pipeline。Fresh process 不重建进程内 permission grant；它重新评估当前 binding，没有 approval
时 fail closed。

既有 `ULTRACODE_DELEGATION_PROGRESS` projection 暴露安全 stage，例如 `swarm_completed`、`adoption_preparing`、
`adoption_applying`、`adoption_completed`、`adoption_conflict`、`adoption_failed` 与 `adoption_indeterminate`，并
携带有界 identity 和 count。不暴露原始 workspace bytes、patch、secret、Plan 或 transcript。

`SessionTurnService` 继续是长生命周期 service，动态路由普通 `max` 回合与显式 `ULTRACODE` 回合。不需要重建 service
或修改全局 mode。

## Consequences

Automatic Ultracode delegation 现在拥有一个由 application 管理的 success path，可以根据精确完成的 Swarm result 安全更新
实际 parent workspace。Parent success ordering 明确，adoption identity 可在 restart 后稳定复用，non-success state 保持可见且
有界。Worker Worktree、lease、Checkpoint、DAG row 与 Swarm resource 不由本切片清理。

## Non-goals

本 ADR 不增加 semantic merge 或 conflict repair、generic retry、rollback、cleanup、commit 或 push、remote/cloud execution、
persistent permission grant、public ACP/TUI adoption control、recursive orchestration 或通用 merge/copy-back engine。不改变
`MAIN_MAX` 或既有 Result Adoption algorithm。

## Validation

验证包括 focused Ultracode、Result Adoption、Agent Swarm、Task DAG、Writable、permission、crash/conversation recovery 与
dynamic TUI tests；真实 temporary-Git production-shaped A/B/C/D fresh-process recovery；schema 29 检查；以及仓库完整 quality gates。
