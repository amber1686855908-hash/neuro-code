# ADR 0136：有界 Task DAG predecessor-result Relay

- 状态：作为显式内部 P0 纵向切片实现；最终评级等待 merge-ref CI
- 日期：2026-08-24
- 范围：一个串行 bounded Task DAG 的直接 completed-predecessor result projection

## 背景

ADR 0134 有意把 dependency edge 定义为 control-only。这样可以防止 worker 从 predecessor
隐式接收 transcript、tool history、workspace state 或 authority grant，但也使 dependent worker
缺少有界 DAG workflow 所需的少量 completed-result context。这个能力不能变成第二套 parent-context
系统、prompt-to-authority channel 或 parallel orchestration 设计。

现有系统已经有三个必须保持分离的 owner：

1. Parent Context Relay 把 parent session 的有界 snapshot 带入 child worker。
2. Task DAG predecessor-result Relay 把 completed direct-predecessor result evidence 带入
   dependent worker。
3. Leader evidence 把有界 DAG state 带入 zero-tool Leader。

## 决策

为已认领的 dependent node 增加 application-owned `TaskDagDependencyResultRelay`。只有在 target
node 完成精确的 `RUNNING` graph/node generation claim 之后、创建 child runtime 或 provider request
之前才能创建 Relay。root node 不接收 Relay。Dependent node 只接收其声明的 direct dependency，且按
声明顺序排列；transitive ancestor 只能通过自己的 direct chain 逐层可见。

Relay 是 immutable、insert-only projection。每个 entry 包含 predecessor node/generation、精确的
worker task/session/lease/worktree/checkpoint/Parent Relay identity、最终 workspace fingerprint、
changed-file count 和有界脱敏 result preview。只有 predecessor 已 durable `COMPLETED`、worker
evidence 一致、writable lease 为 `PRESERVED`，且 Parent Relay 与 workspace/checkpoint identity
和 DAG node projection 匹配时，entry 才会被接受。

## 边界与内容约束

Application 在持久化前和渲染消息前执行以下限制：

- 最多 4 个 predecessor entry；
- 每个 entry 的 UTF-8 result text 最多 4 KiB；
- source result text 合计最多 16 KiB；
- 渲染后的 Relay message 最多 24 KiB；
- ID 与 fingerprint 必须有界；不允许无界 error 或 response text。

跨 edge 的内容只有脱敏 result text 与 evidence metadata。Relay 不包含也不授予 transcript history、
reasoning、tool call/result、workspace bytes、Git data、checkpoint bytes、arbitrary path、capability、
sandbox root、network access、LSP authority 或 instruction。`ContextBuilder` 在 Parent Relay 之后、
真实 child history 之前注入一条独立的 `SyntheticReason.DAG_PREDECESSOR_RESULTS` USER message。
它不会写入 child history，也不会被解析为 authority source。

## Durable identity、race 与失败行为

Session schema 20 增加 `task_dag_dependency_relays`；schema 21 增加独立的
`task_dag_recovery_claims` ownership fence。Relay row 绑定精确 DAG definition、target node
definition/generation、direct dependency IDs、entry fingerprint、source/content fingerprint、byte
count 和 integrity fingerprint。Target-generation uniqueness key 使精确重复发布幂等。内容或 identity
不同的重复发布会被拒绝；直接修改 database 后，在 reload 时完整性校验失败。

Recovery claim 不是 node-generation lock，也不复用 Leader attempt。它的不可变 execution identity 绑定
parent session、DAG/node definition fingerprint、精确 node generation、parent task ID，以及 Relay 的
ID 与 source/content/integrity fingerprint。只有 owner PID/token 可以变化，且只能通过精确 version CAS
更新。`(dag_id, node_id, node_generation)` 唯一键是跨进程 durable fence。

Store 复用既有 SQLite transaction boundary，并在 commit 前 reload 已发布 row。并发 scheduler 不能为
同一 target generation 发布冲突 Relay。对已经 claim 的 active node，recovery 在不启动 worker 的前提下
进行分类：

- `ACTIVE_WORKER`：存在精确的非 terminal `SessionTask` 与 writable lease ownership evidence；继续由既有
  Writable reconciliation 负责恢复。
- `RECOVERY_OWNED`：精确 recovery claim 由仍存活或未被证明死亡的 owner 持有。Reconciliation 在该状态
  保持只读：不启动 Writable、不抢占 claim、不失败 node，也不标记为 `INDETERMINATE`。
- `SAFE_NOT_STARTED`：只有在精确 `RUNNING` node 与 `parent_task_id` 已 durable、通过
  `(dag_id, target_node_id, target_generation)` 读取到既有 READY Relay 且 definition、direct dependency
  与 fingerprint 全部匹配，并且对应 `SessionTask`、writable lease（以及 subagent link）均不存在且没有 live
  recovery owner 时才允许。Read-only reconciliation 只做分类；后续 DAG step 先取得精确 durable recovery
  claim，只有 winner 可以调用 Writable。Live loser 返回 canonical active state，不产生 provider、resource、
  lease、task 或 node terminal side effect。
- `INDETERMINATE`：Relay 缺失或无法验证、证据不完整、存在 link 或其他 worker ownership evidence、identity
  过期或任何状态不确定。对可能已经开始的 worker 永不自动 rerun。只要精确 recovery owner 仍存活或未被
  证明死亡，只有 lease 的 partial window 不能被标记为 `INDETERMINATE`。

如果 recovery owner 在第一次 Writable lease insert 之前死亡，fresh controller 只有在证明旧 owner 已死亡后，
才能通过 version CAS takeover 同一个 claim，并保留同一 node generation、parent task 与 Relay identity。
如果 lease insert 已开始，recovery 永不自动 rerun worker；由既有 Writable reconciliation 决定是否可以收敛，
否则保持 fail-closed。

这个边界来自生产 Writable 的真实顺序：repository identity 检查是只读的；第一个 durable side-effecting
allocation 是插入 lease，随后才是 `SessionTask`、worktree、checkpoint、child session、subagent link、
Parent Relay、runtime creation 与 model execution。因此，精确 active node 加 READY Relay 且对应 task 与
lease 均不存在，能够证明没有进入这段 Writable allocation。Durable recovery claim 关闭了这个 proof 与
第一次 lease insert 之间的跨进程窗口。真实 `spawn` acceptance 覆盖两个 controller 从同一 pre-claim
snapshot 竞态、live-owner partial lease window 和 dead-owner-before-lease takeover；在 ownership evidence
已存在后崩溃则继续 fail-closed，不能 replay。

如果 target generation、predecessor state、lease、Parent Relay、workspace/checkpoint evidence 或任何 identity
缺失、过期、不确定或不匹配，application 会把 target 标为 `INDETERMINATE`，不会构造 worker/provider request。
若 Relay 已 durable 发布而 worker 在 model execution 前崩溃，recovery 复用精确 publication，不重新生成另一份
result，也不 replay predecessor。

## 非目标

本 ADR 不增加 parallel DAG execution、dynamic/model-generated graph construction、超出 direct edge 的
transitive aggregation、retry、rerun、merge/copy-back、rollback、cleanup、shared live context、UI/ACP
暴露、Swarm、Ultracode 或通用 inter-agent message bus。`max_parallel` 仍为 1，
`TaskDagApplicationService` 仍是唯一 worker execution seam。

## 验证边界

Focused implementation tests 覆盖有界 rendering 与 redaction、synthetic message replacement、schema
migration 与 row integrity、精确重复幂等、冲突发布拒绝、direct-dependency selection、声明顺序、
dependency chain、失败或不确定 evidence 的 fail-closed 行为、read-only recovery classification、durable
recovery-claim insert/CAS 与 schema-20-to-21 migration、真实 safe-not-started process death 与 exact-once
continuation、two-controller cross-process ownership 与 partial-window 行为、dead-owner-before-lease
takeover、post-allocation ownership 歧义 process death 不 rerun，以及通过既有 Writable Subagent composition path 的注入。既有 Task DAG、Leader、Writable Subagent、Parent Relay、
Worktree、Checkpoint、worker-scoped LSP、crash recovery 和全仓库 gates 仍是必需项。本切片不宣称
parallel/dataflow scheduling 或 live paid-provider acceptance。
