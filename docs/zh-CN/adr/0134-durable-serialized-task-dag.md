# ADR 0134：持久化有界并行 Task DAG

- 状态：在当前纵向切片范围内 PROVEN
- 日期：2026-08-24
- 范围：一个由调用方定义、节点复用既有 Writable Subagent pipeline 的有界 DAG；`max_parallel` 为 1..4

## 背景

ADR 0129-0133 建立了受管 Git worktree、workspace checkpoint、writable child、
worker-scoped 只读 LSP 与有界 Parent Context Relay 合约，但有意没有提供编排能力。第一版
DAG 切片必须增加持久化依赖控制，同时不能创建第二套 worker runtime、转移权限，或暗示自主委派。
本切片只允许由独立 owner 隔离的 DAG worker 之间进行有界并行。

## 决策

增加 typed 内部 `TaskDag` domain contract 与显式 application service。调用方提供完整节点定义；
parent session 永远从真实的 `ConversationBinding` 取得。第一切片最多允许 8 个节点、16 条依赖
边、每节点 4 个依赖，并且只允许 `WRITABLE_SUBAGENT` 节点。节点 ID、prompt、定义和诊断元数据
均有界且安全。发布前拒绝未知引用、重复边、自依赖、重复 node ID 和环。

拓扑顺序与 ready 节点选择按声明 ordinal 和 node ID 确定。依赖只用于控制。前置节点不会向后置
节点转发 prompt、transcript、reasoning、tool output、response 或 workspace 内容。

`max_parallel` 是 application-internal 的不可变 DAG 定义字段，默认值为 1，并受共享的
`MAX_SUBAGENT_PARALLELISM=4` 上限约束。持久化运行集合由 node rows 中 `state == RUNNING` 的节点
推导；旧的 `active_node_id` 列只保留为串行 Leader 的兼容 projection，不是 capacity 或调度权威。
每个 claim 的节点还持久化 process owner PID/token。owner 存活时，其他 controller 不会把短暂的
`SessionTask`/lease 分配窗口误判为崩溃；owner 死亡后才进入既有的逐节点 fail-closed recovery 分类。

## 既有 owner 继续作为权威

每个可执行节点都复用既有 `SessionTask` 和 `WritableSubagentApplicationService`。DAG 只增加
内部 execution identity，包含 DAG ID、node ID 和生成的 parent task ID。节点在调用 worker 前
先保存这一精确 task ID。既有 Writable service 继续拥有 capability 求交、managed Worktree、
baseline Checkpoint、child session、SubagentLink、Parent Relay、model/runtime、workspace
保留和 worker-scoped LSP。

DAG service 不使用 `SubagentScheduler.run_many()`，不创建第二套 writable 实现，也不暴露 Bash、
terminal、network、MCP、Git、checkpoint、rollback 或递归 subagent 权限。

## 持久化发布与有界 claim

Schema 18 新增 `task_dags` 和 `task_dag_nodes`。定义是 insert-only；已有 DAG ID 只有在不可变
definition fingerprint 相等时才接受。Graph 和 node 生命周期变更使用 generation CAS。

worker 启动前，service 按 ordinal、node ID 确定性选择 ready slice。每个候选节点在一个
`BEGIN IMMEDIATE` 事务中重新加载 DAG，统计 canonical `RUNNING` node rows，检查不可变的
`max_parallel` capacity，校验精确 generation/state，并原子地将 `READY` 改为 `RUNNING`，同时
持久化 parent task 与 process owner identity；同一事务还推进 graph generation。因此跨进程
capacity race 由 SQLite 作为权威处理，process-local lock、semaphore、timestamp、latest row、
prompt 或 lease 猜测都不是权威。

完成节点时，事务写入终态 projection；只有在恰好一个节点仍为 `RUNNING` 时，才派生旧的
`active_node_id` projection，并推进 graph generation，不要求存在 scalar active node。canonical
active execution model 是持久化的 `RUNNING` node set，因此多个 controller 不能创建超过
`max_parallel` 的 worker。

application 使用结构化 `TaskGroup` 执行一个 claim batch。`TaskDagWritableWorkerFactory` 为每个
claimed node 提供全新的 Writable application service；既有的、每 worker 独占的 Writable lock 保持不变。

节点投影只记录有界的生命周期和 workspace identity：parent task、child session、lease、Worktree、
baseline Checkpoint、Relay、fingerprint、changed-file count 和有界 response preview。成功节点
必须有精确的 lease 与 Relay 证据；成功关联缺失或不一致时为 `INDETERMINATE`。

ADR 0136 在不改变这一 execution authority 的前提下增加有界 predecessor-result Relay。Session schema 23
保留 schema-20 Relay、schema-21 recovery fence，并增加有界 DAG capacity 与逐节点 execution-owner 字段。在 dependent node 完成
精确的 `RUNNING` generation claim 且 child runtime 启动之前，DAG service 发布一条 schema-20 insert-only
projection，只包含按声明顺序排列的 completed direct predecessor。Projection 经过脱敏，
每个 result 最多 4 KiB、source result 合计最多 16 KiB、渲染 context 最多 24 KiB；它绑定 predecessor
worker/lease/workspace/checkpoint/Parent Relay identity，不能携带 authority。Relay 是独立 context
channel，不改变 dependency state machine。

## 依赖与失败语义

节点生命周期为：

```text
PENDING -> READY -> RUNNING -> COMPLETED
                              -> FAILED
                              -> CANCELLED
                              -> INDETERMINATE
PENDING/READY -> SKIPPED
```

所有依赖完成后进入 `READY`。失败、取消、跳过或不确定的依赖会使后置节点进入 `SKIPPED`，并记录
有界原因；独立分支仍可执行。只有所有节点完成时 graph 才是 `COMPLETED`；显式取消时为
`CANCELLED`；所有可达节点终止后仍有失败则为 `FAILED`。存在缺失的可达进展时为
`INDETERMINATE`。

## 崩溃与恢复

Reconciliation 先按 active node 精确查询 parent session/task 与精确 lease。对于没有 task 和 lease 的
dependent node，再只读加载精确 predecessor-result Relay 与 recovery claim。Live 或未被证明死亡的 claim
owner 分类为 `RECOVERY_OWNED`；reconciliation 不启动 Writable，也不变更 DAG。只有后续 execution step
可以插入精确 durable recovery claim，且只有 winner 可以开始 Writable。`SessionTask` 已完成、失败或取消时，
映射为 DAG 节点相同语义。没有精确 safe recovery boundary 的 task/lease 证据缺失、lease 为 orphan/不确定，
或关联无效时，映射为 `INDETERMINATE`。不会自动重跑 worker，也不会删除、rollback、merge、copy-back 或
cleanup workspace。

Recovery claim 绑定 parent session、DAG/node definition fingerprint、精确 node generation、parent task，
以及 Relay ID 与 source/content/integrity fingerprint。它的 execution key 独立于 node generation 更新。Live
owner 永不被抢占；如果 owner 在第一次 Writable lease insert 之前被证明死亡，fresh controller 通过 version
CAS takeover 同一 claim；lease ownership 开始后继续由既有 Writable reconciliation fail-closed，不允许自动 rerun。

如果 node 持久化的 execution owner PID 仍存活，reconciliation 只观察该节点，不分配资源、不失败节点、
也不 replay；这覆盖独立 controller 之间发生在 `SessionTask`/lease evidence 写入前的短暂窗口。只有 owner
已经死亡时，才进入逐节点的既有 crash 分类。

取消时先持久化 active node 的终态，再将剩余 pending/ready 节点标记为 cancelled，最后重新抛出
取消。如果进程在 worker 完成与 DAG node finish 之间退出，恢复会利用既有 worker durable evidence
收敛，而不是 replay。

真实进程死亡验收覆盖两个不同边界。如果 Writable `SessionTask` 已经是 `COMPLETED` 且 lease 已经是
`PRESERVED`，但进程在 DAG terminal CAS 前退出，重启会将精确 node reconciliation 为 `COMPLETED` 并释放
`active_node_id`，且不创建第二个 worker。如果 worker owner 在精确 `SessionTask` 仍为非 terminal 时退出，
Writable reconciliation 会将 lease 标记为 `ORPHANED`；DAG reconciliation 会记录 `INDETERMINATE`，保留
child session/worktree/checkpoint/relay identity，且不会重跑 worker。这些保证只覆盖真实
`spawn`/`os._exit` seam 已测试的边界。

## 未实现

本 ADR 不增加 model 生成的 DAG 分解，也不定义 Leader controller；有界 Leader 由独立的
[ADR 0135](0135-bounded-serialized-leader-controller.md) 规定。本 ADR 也不增加 Swarm、Ultracode、自动委派、无界并行、dynamic
dataflow scheduling、前置 transcript 共享、共享 worktree、merge/integration、commit、rollback、cleanup、
retry、自动崩溃重跑、CLI/TUI/ACP 暴露或新的 public orchestration protocol。有界 direct
predecessor-result Relay 由独立的 [ADR 0136](0136-bounded-task-dag-predecessor-result-relay.md) 规定。

## 验证边界

验收要求 domain bound/cycle 测试、带已填充 Parent Relay 的 schema 17→23 迁移、insert-only 与
过期 generation 测试、durable recovery-claim CAS 与 schema-20→21 migration、跨进程 claim 和双 scheduler
竞态证据、live-owner partial-window 与 dead-owner-before-lease takeover 证据、有界并行 diamond 的 fan-out/fan-in overlap、
精确 worker correlation、completed/failed/cancelled/uncertain 恢复、真实 `multiprocessing.spawn` 在
worker 完成后及 active ownership 期间的进程死亡、无重跑 allocation count、真实 Relay-before-model、
真实 Writable/LSP 回归、独立 managed Worktree、parent dirty state 不变、完整本地门禁以及 stacked PR
merge-ref 平台矩阵。
