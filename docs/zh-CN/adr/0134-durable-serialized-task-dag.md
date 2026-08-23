# ADR 0134：持久化串行 Task DAG

- 状态：作为显式内部纵向切片实现；最终评级等待 merge-ref CI
- 日期：2026-08-24
- 范围：一个由调用方定义、节点复用既有 Writable Subagent pipeline 的有界 DAG

## 背景

ADR 0129-0133 建立了受管 Git worktree、workspace checkpoint、writable child、
worker-scoped 只读 LSP 与有界 Parent Context Relay 合约，但有意没有提供编排能力。第一版
DAG 切片必须增加持久化依赖控制，同时不能创建第二套 worker runtime、转移权限，或暗示并行和
自主委派。

## 决策

增加 typed 内部 `TaskDag` domain contract 与显式 application service。调用方提供完整节点定义；
parent session 永远从真实的 `ConversationBinding` 取得。第一切片最多允许 8 个节点、16 条依赖
边、每节点 4 个依赖，并且只允许 `WRITABLE_SUBAGENT` 节点。节点 ID、prompt、定义和诊断元数据
均有界且安全。发布前拒绝未知引用、重复边、自依赖、重复 node ID 和环。

拓扑顺序与 ready 节点选择按声明 ordinal 和 node ID 确定。依赖只用于控制。前置节点不会向后置
节点转发 prompt、transcript、reasoning、tool output、response 或 workspace 内容。

## 既有 owner 继续作为权威

每个可执行节点都复用既有 `SessionTask` 和 `WritableSubagentApplicationService`。DAG 只增加
内部 execution identity，包含 DAG ID、node ID 和生成的 parent task ID。节点在调用 worker 前
先保存这一精确 task ID。既有 Writable service 继续拥有 capability 求交、managed Worktree、
baseline Checkpoint、child session、SubagentLink、Parent Relay、model/runtime、workspace
保留和 worker-scoped LSP。

DAG service 不使用 `SubagentScheduler.run_many()`，不创建第二套 writable 实现，也不暴露 Bash、
terminal、network、MCP、Git、checkpoint、rollback 或递归 subagent 权限。

## 持久化发布与串行 claim

Schema 18 新增 `task_dags` 和 `task_dag_nodes`。定义是 insert-only；已有 DAG ID 只有在不可变
definition fingerprint 相等时才接受。Graph 和 node 生命周期变更使用 generation CAS。

worker 启动前，service 原子地把一个 `READY` 节点改为 `RUNNING`，持久化精确 parent task ID，
并设置 graph 的 `active_node_id`；同一事务还更新 graph generation。完成 active node 时，原子
写入终态投影、清除 `active_node_id` 并推进 graph generation。active-node claim 是跨进程串行
闸门：两个 scheduler 不能同时 claim 不同 ready 节点。不使用 timestamp、latest row、prompt
或 lease 猜测。

节点投影只记录有界的生命周期和 workspace identity：parent task、child session、lease、Worktree、
baseline Checkpoint、Relay、fingerprint、changed-file count 和有界 response preview。成功节点
必须有精确的 lease 与 Relay 证据；成功关联缺失或不一致时为 `INDETERMINATE`。

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

Reconciliation 先运行既有 Writable reconciliation，再按 active node 精确查询 parent session/task
与精确 lease。`SessionTask` 已完成、失败或取消时，映射为 DAG 节点相同语义。task/lease 证据
缺失、lease 为 orphan/不确定，或关联无效时，映射为 `INDETERMINATE`。不会自动重跑 worker，也
不会删除、rollback、merge、copy-back 或 cleanup workspace。

取消时先持久化 active node 的终态，再将剩余 pending/ready 节点标记为 cancelled，最后重新抛出
取消。如果进程在 worker 完成与 DAG node finish 之间退出，恢复会利用既有 worker durable evidence
收敛，而不是 replay。

## 未实现

本 ADR 不增加 model 生成的 DAG 分解、Leader、Swarm、Ultracode、自动委派、并行执行、dataflow/
result relay、前置 transcript 共享、共享 worktree、merge/integration、commit、rollback、cleanup、
retry、自动崩溃重跑、CLI/TUI/ACP 暴露或新的 public orchestration protocol。

## 验证边界

验收要求 domain bound/cycle 测试、带已填充 Parent Relay 的 schema 17→18 迁移、insert-only 与
过期 generation 测试、跨进程 claim 和双 scheduler 竞态证据、确定性的串行 diamond 失败传播、
精确 worker correlation、completed/failed/cancelled/uncertain 恢复、真实 Relay-before-model、
真实 Writable/LSP 回归、独立 managed Worktree、parent dirty state 不变、完整本地门禁以及 stacked
PR merge-ref 平台矩阵。
