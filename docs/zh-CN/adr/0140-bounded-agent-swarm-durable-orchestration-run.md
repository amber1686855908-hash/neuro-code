# ADR 0140：有界 Agent Swarm / 持久化编排运行

- 状态：作为显式内部 P0 vertical slice 已实现；最终验证记录在 PR #67 CI 中，live/paid provider validation 仍不在范围内
- 日期：2026-08-27
- 范围：一个有界 Planner → Leader → Task DAG → Writable worker 编排运行，最多使用一个既有 DAG Replan successor
- 依赖：ADR 0131、ADR 0132、ADR 0133、ADR 0134、ADR 0135、ADR 0136、ADR 0137、ADR 0138 与 ADR 0139

## 背景

当前仓库已经分别验证了 Planner、parallel-aware Leader、Task DAG、predecessor-result Relay、Writable Subagent、worker-scoped LSP、Worktree、Checkpoint 和 bounded Replan。这些能力有意保持为独立的 authority owner。一个有界 multi-agent 用户能力需要一个 durable orchestration identity，但不能变成这些 service 的第二套实现或通用 scheduler。

## 决策

在 `ApplicationComposition` 增加一个内部 `BoundedAgentSwarmApplicationService`。它只拥有 parent-bound Swarm run identity、生命周期摘要和精确 Planner/DAG lineage。authority chain 为：

```text
真实 parent ConversationBinding
        -> durable Swarm run
        -> 既有 zero-tool model Planner
        -> 既有 TaskDagApplicationService
        -> 既有 parallel-aware Leader
        -> 既有 Writable worker、Relay、Worktree、Checkpoint 与 LSP
        -> 既有 bounded DAG Replan，最多一次
        -> 既有 Leader final synthesis projection
```

组合根通过既有 factory 创建每个 lower-layer service。Swarm 不直接创建 tool、session、worktree、checkpoint、LSP manager、relay 或 worker runtime。它没有连接 CLI、TUI、ACP、Ultracode、自动委派或 public orchestration protocol。

## Durable identity 与 lifecycle

Schema 27 增加 insert-once 的 `orchestration_swarm_runs` projection。一条 row 绑定 bounded run ID、真实 parent session、脱敏 objective fingerprint、确定性的 Planner ID、owner PID/token/lease、generation、Planner session/turn 与 proposal fingerprint、root/current DAG identity 与 generation、可选 Replan revision/successor，以及有界 terminal response/result fingerprint。Parent、Planner、root/current DAG 与 successor reference 使用 foreign-key `RESTRICT`，因此 recovery history 不能绕过 run 被删除。

Swarm lifecycle 为：

```text
CLAIMED -> PLANNING -> PLANNED -> EXECUTING
                         |            |
                         |            +-> REPLANNING -> EXECUTING（一次）
                         |            +-> FINALIZING -> COMPLETED
                         |            +-> FAILED / INDETERMINATE
                         +-> INDETERMINATE
```

`FAILED` 只保留给 single Replan 已消耗后 successor DAG 的失败。Provider、storage、ownership、cancellation 或 lower-layer uncertainty 都进入 `INDETERMINATE`，不会变成可重试的 failed source。状态更新使用 generation CAS 以及既有 durable controller 的 live owner/process-liveness 规则。Live 或未被证明死亡的 owner 不会被抢占；被证明死亡的 owner 才能以新 generation 和 exact identity 接管一次。

Terminal response 复用既有 Leader final response，经脱敏并限制为 16 KiB。其 fingerprint 覆盖 Swarm ID、current DAG ID 与 generation、不可变 DAG definition fingerprint 和 response。Fresh controller 在 `COMPLETED` 后返回已存储 result，不创建 Planner、Leader、worker 或 provider call。

## 正常并行路径

Production-shaped acceptance path 使用一个 `max_parallel=2` 的 model-generated bounded graph：

```text
    A
   / \
  B   C
   \ /
    D
```

既有 Leader 发布 `SELECT_NODE(A)`，再发布 `SELECT_NODES(B,C)`，然后 `SELECT_NODE(D)`，最后 `FINALIZE`。B 和 C 是独立的 Writable execution，各自拥有独立的 managed Worktree、baseline Checkpoint、child session、Parent Relay、predecessor-result Relay entry 和 worker-scoped LSP manager。只有两个声明的 predecessor 都完成后，D 才会被 claim，并且只收到既有的确定性 predecessor-result projection。Parent checkout 不作为共享 writable workspace 使用且保持不变。

Swarm 不扩大 worker scheduler 或 authority。既有 capability intersection、sandbox、filesystem、Worktree、Checkpoint、Parent Relay、result Relay 与 LSP boundary 继续是 authority。Swarm projection 或 Swarm context 不保存或传递 raw transcript、hidden reasoning、provider request、tool argument、environment、credential、checkpoint blob、workspace content 或 authority instruction。

## Replan 路径

当 current source DAG 是 `FAILED`、quiescent、全部 terminal 且没有 indeterminate node 时，Swarm 进入 `REPLANNING`，使用一个确定性的 revision ID 调用 ADR 0139 既有 service。调用前后都会检查 source definition 与 runtime projection，source 保持不可变。既有 Replan service 强制 `MAX_DAG_REPLAN_DEPTH=1`，发布一个新的 immutable successor identity，并保留自身的 no-provider-replay contract。Swarm 在回到 `EXECUTING` 前验证 exact source、evidence、proposal、revision、successor、parent 与 depth lineage。

Successor 失败后，Swarm 进入 terminal `FAILED`。它不会对 `INDETERMINATE` 或 cancelled DAG 进行 replan，不会复活 source node，不会重试 provider 或 worker，也不会创建第二个 successor。

## Crash recovery 与 controller race

在调用 Planner 前，先持久化 Swarm row。Recovery 始终委托给既有 Planner、Leader、Task DAG、Writable、Relay、Worktree、Checkpoint、LSP 与 Replan contract；Swarm 自身只 reconciliation durable phase 与 exact identity。Focused composition tests 覆盖有意义的边界：provider 执行前 identity、Planner/DAG publication、活动并行 wave、DAG 完成但尚未 finalization、failed source 但尚未 Replan、successor execution、terminal result reuse，以及 spawned controller 在首次 durable claim 后退出。Fresh finalization recovery 还证明 terminal result 已待处理时不会调用 lower factory。

`tests/test_agent_swarm_process_recovery.py` 中的代表性 fresh-process recovery matrix 使用 `multiprocessing` spawn，并在四个 Swarm handoff 写入明确的 durable marker。它证明：Planner attempt/proposal/DAG 已完成但 Swarm 尚未进入 `PLANNED` 时可以恢复；lower Leader/DAG 已终态但 Swarm 尚未进入 `FINALIZING` 时可以恢复；Replan successor 已完成发布但 Swarm 尚未切换 current DAG 时可以恢复且失败 source 保持不可变；以及 `FINALIZING` result 已持久化但尚未 `COMPLETED` 时可以恢复。每个 L1 process 都通过 `os._exit` 退出，fresh `ApplicationComposition` L2 校验精确的 run、Planner、DAG、Replan、result、Provider-call 与 managed-resource identity，且不 replay。该矩阵是有界的代表性证明，不声称覆盖任意 kill timing，也不声称支持 live/paid provider。

两个 controller 争抢同一个 Swarm ID 时，使用 SQLite `BEGIN IMMEDIATE`、insert-once identity、process-liveness ownership 与 generation CAS。一个 active row 只能由一个 controller 持有。Loser 不进行 provider call、DAG publication、worker allocation 或 terminal-result mutation。可观察的 provider-turn uncertainty 会 fail closed，永远不会 replay。

## Cancellation 与 bounds

Swarm phase 被当前 owner 持有时发生 cancellation，会通过 shielded durable transition 记录为 `INDETERMINATE`。如果 lower component 的 side effect 不确定，Swarm 不能把该 uncertainty 解释为安全的 Replan。复用既有 Task DAG node、parallelism、relay、worker 与 Replan limits；Swarm 不增加无界 queue、recursive graph、recursive Replan、generic retry 或隐藏 orchestration step counter。

## 非目标

本 ADR 不增加 automatic Ultracode delegation、user-facing Ultracode behavior、recursive Swarm、无界 agent、generic retry、共享 writable worktree、merge、cherry-pick、copy-back、patch adoption、public CLI/TUI/ACP orchestration、remote/cloud execution、marketplace integration 或新的 Checkpoint/Rollback 实现。Checkpoint 与 Rollback 继续使用既有能力与 authority owner。

## 验证

本切片为冻结的 Planner race preflight 增加确定性 event/barrier synchronization，并增加 production-shaped 正常路径和 Replan 路径、Swarm domain/store durable test、SQLite migration/FK/tamper/CAS test、四个边界的 fresh-process recovery matrix、active-controller race，以及组合层 authoritative `INDETERMINATE` no-Replan test。完整 repository quality gates 与 PR #67 merge-ref CI 是发布证据；本 ADR 不声明任意 crash point 覆盖、live-provider 或 public-interface support。
