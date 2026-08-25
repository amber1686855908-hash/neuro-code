# ADR 0137：Parallel-aware Leader / 有界 wave scheduling

- 状态：作为显式内部 P0 vertical slice 实现；最终评级等待 merge-ref CI
- 日期：2026-08-26
- 范围：一个作用于已发布 bounded Task DAG 的 zero-tool Leader
- 取代范围：ADR 0135 中的串行执行部分；ADR 0135 仍保留为历史 decision contract

## 背景

ADR 0134 已提供不可变 static Task DAG definition、持久化 capacity、逐节点
execution owner、generation CAS 和独立 Writable worker service。ADR 0135 提供了
持久化 zero-tool Leader，但当 DAG 声明 `max_parallel > 1` 时，Leader 的执行 seam
仍然是串行的。本切片需要让 Leader 选择一个有界 wave，同时不把 worker、workspace、
capability 或 dependency authority 移入模型。

本切片保持内部性质：不增加 public CLI/TUI/ACP surface，不增加 dynamic graph、
replan、retry、merge、rollback，也不增加第二套 orchestration hierarchy。

## 决策

保持以下 authority hierarchy：

```text
zero-tool Leader decision -> Task DAG claim/CAS -> Writable worker -> Worktree/session resources
```

Leader 只拥有 typed decision。Task DAG 继续是 dependency legality、持久化
`RUNNING` capacity、node generation、execution owner identity 和 worker invocation
的唯一 owner。Writable Subagent 继续拥有 child binding、capability intersection、
worktree、lease、checkpoint、relay 和 worker-scoped LSP resource。Leader 不获得 tools
或 worker binding。

### Typed decision 与 canonical selection

严格 JSON contract 只能是以下之一：

```json
{"action":"SELECT_NODE","node_id":"<ready id>","reason":"<bounded text>"}
{"action":"SELECT_NODES","node_ids":["<ready id>","<ready id>"],"reason":"<bounded text>"}
{"action":"FINALIZE","summary":"<bounded text>"}
```

`SELECT_NODE` 保留串行 compatibility path。`SELECT_NODES` 的列表必须非空、无重复、
不超过 `max_parallel`，并按 canonical `(ordinal, node_id)` 顺序排列。非 canonical
列表直接拒绝；Leader 不会静默重排 model output。每个 selected ID 必须属于精确的
READY set；超过 durable free capacity 的 decision 拒绝。`FINALIZE` 只有在 DAG terminal
且不存在 `RUNNING` node 时才接受。

Leader 在调用 Task DAG wave seam 前，针对 exact evidence snapshot 校验 decision。
Unknown/duplicate ID、terminal node selection、过期 graph/node generation、capacity
overflow、malformed 或 unknown JSON 都 fail closed。Typed invalid output 作为 durable
历史保留，绝不 replay provider request。

### Evidence contract

有界 evidence envelope 包含 parent session、DAG ID 与 definition fingerprint、graph
generation、不可变 `max_parallel`、durable `running_node_ids`、计算得到的
`available_capacity`、canonical READY ID 以及每个 node 的有界 projection。每个 node
包含 ordinal、generation、dependencies、带界限且脱敏的 outcome metadata，以及 opaque
worker identity/fingerprint 字段。

为便于确定性检查，payload 还显式提供 state buckets：`completed_node_ids`、
`failed_node_ids`、`cancelled_node_ids`、`skipped_node_ids` 和 `indeterminate_node_ids`。
这些 bucket 从 node projection 派生，只是 evidence，不授予 authority。原有 byte、node
count、text bound 和脱敏限制继续有效。Raw transcript、reasoning、tool argument/result、
relay payload、workspace bytes、credential、任意 path 和 capability grant 继续排除。

### Wave 执行与 capacity

内部 typed seam `RunTaskDagWaveRequest` 携带 selected IDs、expected graph generation，
以及按相同 canonical 顺序排列的每个 selected node expected generation。Task DAG service：

1. reconciliation 当前 graph 并传播 dependency state；
2. 校验 exact graph generation、selected READY generation、不可变 parallel bound 和
   当前 durable capacity；
3. 通过既有 SQLite `BEGIN IMMEDIATE` capacity check 与 graph/node generation CAS，
   只 claim selected node；
4. 为每个 claimed node 创建一个独立 Writable service；
5. 在结构化 `TaskGroup` 中执行这些 claimed worker。

Durable capacity 始终是 `max_parallel - RUNNING rows`；process-local semaphore 不是
authority。Decision 发布后，race 或 recovery 可能减少 capacity；此时 service 只能 claim
仍有空间的 selected canonical prefix，绝不替换成未选择的 READY node。下一次 evidence
refresh 决定剩余 selected node 是否可以应用。`max_parallel=1` 继续走串行 one-node path。

### 持久化、恢复与竞态

Session schema 24 增加 parallel decision projection。Leader attempt 与 decision 保留
parent session、selected node IDs 和 selected node generations。23→24 migration 增加这些
列，从 `task_dags` 回填 parent identity，回填旧 `SELECT_NODE` list；当旧 decision table
的 `CHECK` 不接受 `SELECT_NODES` 时重建该表。Migration 保留已填充的 schema-23
attempt/decision rows。

实际 node execution owner 仍在 Task DAG row 中。Process crash 后，只有当每个 selected node
仍处于其记录的 READY generation，或已从该 generation durable advanced 到 `RUNNING` 或
terminal node 时，durable `SELECT_NODES` decision 才可复用。Recovery 不再调用 Leader
provider，也不创建未选择的 worker。因此 partial claim 可以完成剩余 selected prefix，
已 claim node 继续受既有 Task DAG/Writable recovery semantics 保护。

两个 controller 继续通过既有 durable Leader attempt fence 与 Task DAG CAS 竞态。一个
controller 可以 publish/execute wave；另一个必须复用可观察的 durable decision 或 fail
closed。live stale provider owner 会被 fence；未解决 provider turn 保持 `INDETERMINATE`，
不会从 process death 推断 provider replay 安全。

Failure、cancellation、skipped descendant 和 indeterminate node 继续使用 Task DAG 既有
语义。Indeterminate branch 不阻止无关 READY branch 调度，但 terminal `INDETERMINATE`
DAG 不能被当作成功结果 finalize。不增加 automatic retry、rerun、merge、rollback、cleanup
或 graph mutation。

## 验证边界

只有在以下 focused 与 production-shaped evidence 完备时才接受本切片：

- serialized `max_parallel=1` compatibility；
- 真实 `A -> (B,C) -> D` wave，B/C 实际 overlap 且 D 等待二者；
- 一个 running node 加三个 READY node、两个 free slot 的 evidence；
- canonical order、duplicate、overflow、stale-generation 与 running-node `FINALIZE`
  rejection；
- 独立 worker/resource identity 与不替换未选择 node；
- durable decision reuse 且不 replay provider；
- partial-claim recovery、spawned-process death 与 concurrent controller；
- failure、cancellation、skipped descendant 与 unrelated-branch progress；
- populated schema-23→schema-24 migration；
- 完整 repository formatting、typing、coverage、build 与 docs-parity gates。

本切片仍是内部且有界的。只有 exact-head merge-ref CI 全绿后，才可将能力评级为
`PROVEN within current vertical-slice scope`。
