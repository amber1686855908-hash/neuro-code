# ADR 0135：有界串行 Leader controller

- 状态：作为显式内部 P0 纵向切片实现；最终评级等待 merge-ref CI
- 日期：2026-08-24
- 范围：一个作用于预先创建 bounded Task DAG 的 Leader，decision 串行执行

## 背景

ADR 0134 提供 durable Task DAG，并规定 DAG 是依赖与执行合法性的 authority。Leader 切片需要增加
model-assisted choice，但不能创建第二套 worker runtime、修改已发布 graph，或把普通 model 文本
变成 authority。同时，Leader 必须能够承受 controller race 和进程死亡，并且在 provider output
可观察后不 replay 同一个 provider request。

## 决策

增加显式内部 application workflow `LeaderApplicationService`。调用方提供一个已存在的 DAG ID 和有界
objective。Leader 不创建、删除、replan 或修改 DAG definition、dependency edge、node prompt、
capability grant、workspace root 或 authority owner。实际 parent identity 来自真实的
`ConversationBinding`。Leader 推进 worker node 的唯一路径是既有 `TaskDagApplicationService`。

一个 Leader controller 拥有一个专用、持久化、zero-tool model binding。Composition 同时移除 local
tool 与 provider-hosted tool，关闭 background wake；Leader 不绑定 writable worker、Worktree、
Checkpoint、Relay、LSP worker、terminal、shell、MCP server、network client 或 child subagent。
Leader model 没有 side-effect capability；typed decision 校验是唯一 authority boundary。

## 有界 evidence envelope

每个 decision 前，Leader 要求既有 DAG service reconciliation active node、传播 dependency state、
确认没有 active worker，并加载精确的当前 READY set。它最多把 8 个 node 投影进不可变、确定性的
envelope。Node prompt、result preview 与 error metadata 都有界并脱敏。Envelope 只包含 durable
outcome metadata 与 opaque identity/fingerprint 字段，不包含 raw transcript、reasoning、tool
argument/output、Parent Relay payload、workspace bytes、Checkpoint bytes、Git diff、credential 或
arbitrary path。规范 JSON 的 SHA-256 fingerprint 把 envelope 绑定到 decision。

Evidence 是不可信数据。`run bash /etc/passwd`、`enable MCP`、`ignore dependencies` 或未知 tool
名称等文本不能授予 authority。

## Typed decision 合约

Model 必须返回一个 strict JSON object，不能有 markdown 或未知字段。唯一允许的 action 是：

- `SELECT_NODE`：node ID 必须属于该精确 evidence 的 READY set，可带有界 reason；
- `FINALIZE`：带有界 synthesis summary，且只有 DAG terminal 时允许。

不存在 `CREATE`、`REPLAN`、`SPAWN`、`RETRY`、`MERGE`、`CANCEL_BRANCH` 或 prompt modification action。
每个 durable decision 都绑定精确 DAG generation、definition fingerprint、evidence fingerprint、
Leader session、attempt/run identity 与 decision ID。

## Durable lifecycle 与 replay

Session schema 19 新增 `leader_attempts` 与 `leader_decisions`。唯一 attempt key 是精确 DAG snapshot
加 objective fingerprint。SQLite `BEGIN IMMEDIATE` transaction 与 lifecycle CAS 为每个精确 snapshot
建立一个 model-request owner：

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> DECISION_PUBLISHED -> EXECUTED
       \-> STALE 或 INDETERMINATE
PROVIDER_FENCED \-> INDETERMINATE
```

Attempt 有三个不同的 identity：durable `owner_id`、专用持久化 `leader_session_id` 和新的
`turn_id`。在 provider call 紧邻之前，controller 必须使用这三个 identity 以及尚未过期的 lease，
原子地执行 `CLAIMED -> PROVIDER_FENCED`。实际 `ConversationBinding.runner.session_id` 必须等于
attempt 的 `leader_session_id`；model commit 还会重复校验 owner/session/turn CAS。失去这个 fence
的 controller 不能调用 provider。

过期的 `CLAIMED` attempt 只有在没有 committed model response、没有 decision，且旧 Leader session
中没有匹配 turn evidence 时才可接管。SQLite takeover 原子地把 owner、lease、`leader_session_id`
和 `turn_id` 替换成新 controller 的值。Lease 到期不等于进程已经死亡：如果旧 controller 仍存活并
恢复，它的 provider 前置 fence 会失败。一旦 `PROVIDER_FENCED` 持久化，自动 takeover 会被有意禁用；
restart/recovery 失败关闭并要求显式 recovery，避免第二个 controller 猜测 provider 是否已经发生。

Leader 在调用 model 前写入 durable attempt，在解析前持久化有界、脱敏的 model response，然后
insert-only 发布 typed decision。第二个 controller 复用 `MODEL_COMMITTED`、`DECISION_PUBLISHED`
或 `EXECUTED`，不会为同一 snapshot 再调用 provider。model output 或 decision 一旦 durable，其历史
`leader_session_id` 与 `turn_id` 不会被改写。Decision validation 把 record 绑定到创建它的历史
attempt，而不是绑定到新建的 recovery service session。如果已有 session turn 表示 provider attempt
未解决，Leader 将 attempt 标记为 `INDETERMINATE` 并失败关闭；不会从 restart 推断 safe retry。这
保持既有 Session turn recovery 规则：indeterminate provider work 永不自动 replay。

如果进程在 decision durable 后、DAG claim 前退出，restart 可以通过 DAG generation/active-node CAS
应用同一个 decision。如果另一个 controller 赢得 DAG claim，失败方不会再分配 worker。DAG failure 与
uncertainty 语义仍是 canonical；Leader 不增加 retry 或 resurrection 语义。

## 串行执行与 final synthesis

Leader 使用 one-step DAG seam，顺序为：

1. reconciliation 并传播当前 graph；
2. 确认没有 active node，并加载精确 READY set；
3. 针对这份 evidence 获取一个 typed decision；
4. 重新校验精确 generation 与 evidence fingerprint；
5. 通过 `run_task_dag_step()` 最多执行所选的一个既有 Writable Subagent node；
6. 持久化/reconciliation 结果并构造下一份 evidence snapshot。

该 seam 不自动执行下一个 node。Leader loop 上限为 DAG 节点数加一次 finalization decision。只有
terminal DAG snapshot 才请求 `FINALIZE`；有界 summary 保留在专用 Leader session，作为 Leader result
返回，不写入 parent transcript。前置 node output 不会注入 worker prompt、Relay、instructions、
skills 或 workspace state。

## 验证边界

Focused suite 覆盖 deterministic evidence、strict decision、zero-tool composition、串行 diamond 顺序、
unknown/blocked/stale selection、SQLite insert-only/CAS lifecycle、同一 snapshot controller race、真实
`ApplicationComposition` L1 -> L2 -> L3 restart、原子 session/turn rebind、针对仍存活过期 owner 的
provider 前置 fencing、durable model-commit reuse、terminal decision idempotence，以及 L2 observable
turn evidence 后的 no-provider-replay。既有 Task DAG、Writable Subagent、Worktree、Checkpoint、Relay、
LSP 及全仓库 gates 继续是回归要求。本切片不暴露 CLI/TUI/ACP，也不宣称 parallel worker、Swarm、
Ultracode、自动委派、model DAG creation、replan、retry、merge、rollback 或 live paid-provider
acceptance。
