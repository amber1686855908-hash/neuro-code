# ADR 0143 — 有界持久化结果采纳核心

**简体中文** · [English](../../en/adr/0143-bounded-durable-result-adoption.md)

## 状态

已接受，适用于有界内部纵向切片。

## 背景

已完成的 writable worker 只产生关于自身 managed worktree 的证据。这个结果本身并不等于
修改 parent checkout 的权限。Parent 可能存在无关 dirty file，另一个 controller 可能正在恢复
同一个采纳，或者 worker 在 result 保留后又发生变化。若把 worker 文本、response summary、
`git diff` 或 latest-row lookup 当作采纳 authority，这些情况就会变得不明确，并可能覆盖 parent
工作。

现有 Task DAG、Swarm、Writable Subagent、Worktree、Checkpoint、scoped permission、sandbox 与
filesystem boundary 已经分别拥有各自的 capability。本切片只增加一个显式 application service，
消费这些 durable projection，并通过既有 runtime write boundary 转发精确的 parent mutation。
它不是 Ultracode feature，也不提供通用 merge engine。

## 决策

`ResultAdoptionApplicationService` 是由 application 组合的内部 capability。调用方只提供采纳
identity 与已完成的 Swarm identity。Service 生成不可变的 `ResultAdoptionPlan`；调用方、Provider、
worker、response text 与 relay 不能伪造它的 target path、image、authority 或 parent identity。

Plan 绑定：

- 活动 parent `ConversationBinding` 的 session、规范 workspace root、repository identity 与当前
  committed HEAD；
- 精确的 completed Swarm 与 Task DAG generation/definition fingerprint；
- 声明顺序的 completed source node，以及它们的 child session、lease、managed Worktree、READY
  baseline Checkpoint、base commit、final workspace fingerprint、capability fingerprint 与 grant
  fingerprint；
- 有序的精确 target set、operation、baseline image、desired image、pre-image fingerprint、desired
  fingerprint 与 plan fingerprint。

只有 completed Swarm 与 completed writable DAG 才有资格参与。每个 source node 必须拥有 preserved
terminal lease、READY baseline Checkpoint、managed READY Worktree，并且 durable identity 全部匹配。
Plan 生成前会检查 live preserved worker projection；其 canonical fingerprint 必须同时等于 node 和
lease 的 final fingerprint。Parent identity 从活动 binding 及实际 projection 读取，绝不采信 model 或
worker path。

## 三方校验与路径策略

对每个发生变化的 regular file，Plan 记录以下一种 operation：

- `CREATE`：baseline 不存在、desired 存在且 parent 不存在；
- `UPDATE`：baseline 存在、desired 不同且 parent 等于 baseline；
- `DELETE`：baseline 存在、desired 不存在且 parent 等于 baseline。

任何 parent 同路径差异都会在 `APPLYING` 之前持久化为 `CONFLICT`，并保证 parent 零写入。符合条件
的多个 worker 之间只要 changed relative path 有重叠，就在 Plan 发布前拒绝。Parent 无关 dirty path
会保留。Symlink、link-like traversal、special file、仅 mode 变化、Neuro 受保护状态、checkpoint/
worktree storage、credential 与 root 外路径在本切片中都 fail closed。

保守的第一版上限为：最多 8 个 source worker、64 个 target file、target image 总计 32 MiB、单个
file image 8 MiB、relative path 4 KiB。采纳 ownership lease 最长五分钟。这些上限是 application
常量，durable state 加载时还会由不可变 domain value 再次校验。

## Parent mutation authority

Service 不调用 `shutil`、raw file replacement、Git checkout/apply/cherry-pick、shell 或面向 model
的 public tool。它为每个 target 创建一个 typed `WorkspaceMutationRequest`，并调用从活动 parent
binding 捕获的 mutation port。Runtime 路径保持为：

```text
canonical filesystem target
  -> PermissionManager / scoped approval
  -> workspace boundary 与 instruction checks
  -> sandbox/profile check
  -> exact regular-file executor
```

`CREATE` 与 `UPDATE` 只有在 ordinary canonical target rules 生成候选时才可使用既有
`WORKSPACE_EDITS` candidate。`DELETE` 绝不继承这个 broad workspace candidate，只能走 exact-action-or-
deny。显式 `DENY`、foreign parent session/root、model 提供的 scope 或 worker capability 都不能授权
采纳。Approval memory 仍只存在于进程内，不会持久化，也不会在重启后自动重建。

## 持久化生命周期与恢复

Session Store schema 29 增加 insert-only 的 `result_adoptions` 与逐 target 的
`result_adoption_targets` row。Parent 与 target transition 使用 owner liveness、`BEGIN IMMEDIATE`、
不可变 identity 校验与 generation CAS。Plan 生命周期为：

```text
CLAIMED -> VERIFIED -> APPLYING -> VERIFYING -> COMPLETED
    \-> CONFLICT / FAILED / INDETERMINATE
```

每个 target 记录 `NOT_STARTED`、`APPLYING`、`RETRYABLE`、`APPLIED`、`CONFLICT`、`FAILED` 或
`INDETERMINATE`。在 target 产生可观察 mutation 之前，operation、path、expected pre-image、desired
image 与 fingerprints 已经 durable。恢复时检查 parent 的实际 image：

- expected pre-image：只有重新通过 permission/write boundary 才能 retry；
- desired image：标记为 `APPLIED`，绝不重写；
- 两者都不是：mutation 前标记 `CONFLICT`，尝试产生 effect 后标记 `INDETERMINATE`，绝不覆盖第三种
  image。

Filesystem mutation 不是多文件 atomic operation。Partial application 按 target 逐个 forward recovery；
如果后续 target 被外部修改，采纳变为 `INDETERMINATE`，不回滚已完成的前置 mutation。使用完全相同
identity 重复已完成的采纳只执行零次写入，并返回相同 durable result。Live owner 不会被抢占；只有
通过 durable owner/CAS 规则证明死亡的 owner 才能接管。

Worker Worktree、READY Checkpoint、lease、DAG row 与 Swarm resource 永远不会被此 service 移除、
rollback、merge、commit、copy back 或 cleanup。

## 后果与非目标

这个核心可以在保留 parent 无关 dirty work 与 durable worker evidence 的前提下，把有界的精确
regular-file result 安全应用到实际 parent checkout。它刻意不增加 automatic Ultracode integration、
model merge、conflict resolution UI、TUI/ACP entrypoint、checkpoint rollback、cleanup、commit/push、
remote execution、recursive Swarm 或通用 writable merge/copy-back engine。Worker result completion 与
parent mutation completion 仍然是两个不同状态。

## 验证

Focused tests 覆盖 three-way create/update/delete identity、parent/stale/overlap conflict、target-level
recovery、duplicate adoption、permission boundary、schema 28→29 migration 与 spawned-process A/B/C/D
recovery。一个 production-shaped composition 在临时 Git repository 中使用真实 SQLite、managed Worktree、
READY Checkpoint、parallel Task DAG worker 与 canonical parent mutation port；它验证 parent A/C 变化、B 与
无关 dirty U 保留、HEAD 不变、child evidence 保留、`INDETERMINATE` 时第三方 bytes 保留，以及 completed
重入时没有新的 filesystem 或 durable resource row。在把本切片评级为 proven 前，完整的 lock、文档 parity、
lint、format、mypy、coverage 与 build gate 仍然是必要条件。
