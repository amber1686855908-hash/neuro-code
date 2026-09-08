# ADR 0156：Ultracode parent 工作区 freshness projection

- 状态：Accepted
- 日期：2026-09-08
- 范围：VF-4b result-adoption 工作区 projection

## 背景

Worker Worktree 与 parent checkout 是不同的 verification 边界。Result Adoption 已经记录了 durable target
lifecycle，但 parent verification layer 还需要一个有界事实，说明一次 logical adoption 是否到达过期望的 parent
image。这个事实必须在 controller recovery 和 target-level diagnostic state 变化后仍然保留，同时不能增加第二个
verification tracker 或新的数据库列。

## 决策

`ResultAdoptionRecord.parent_workspace_changed` 是唯一 canonical projection。当至少一个 target durable 到达
`APPLIED` 时它为 true。如果 target 已经 applied、随后在 final verification 中发生变化，则以精确的 canonical
`post_apply_concurrent_modification` error kind 持久化为 `INDETERMINATE`；这个 state 保留历史 applied fact。若
`INDETERMINATE` 是在观察到任何 desired image 之前从 `APPLYING` 到达，则不设置该 projection。因此 no-target adoption、
pre-apply conflict 以及任何 target 到达 `APPLIED` 之前的 failure 都保持 false；若其他 target 已到达 `APPLIED`，partial
terminal outcome 则保持 true。

`APPLIED` 表示 desired image 已被观察并 durable acknowledge，并不证明由哪个 process 完成写入。这个 projection 是
parent-workspace fact，不是因果写入回执。一次 multi-target adoption 是一个 logical mutation boundary。未来调用方即使在
recovery 中再次观察 completed record，也只能按稳定的 `adoption_id` 让现有 `VerificationTracker` 最多推进一次。

Projection 不检查 worker response text、command、summary、filesystem prose 或 worker `VerificationReport`。Worker
verification evidence 不导入 parent tracker。

## 恢复与兼容性

该 property 从已有 durable target row 推导，因此 fresh controller 无需写入或创建第二次 adoption 即可得到相同值。crash
后观察到 desired image 会成为 `APPLIED`；pre-apply image 不确定时成为 `INDETERMINATE`，不宣称 parent 已变化；final-verification
阶段的 post-apply race 保留 applied fact。Schema 30 不变，旧 adoption row 继续可读。

`MAIN_MAX` verification snapshot 语义不变。结构化 `BOUNDED_SWARM` verification 仍 fail-closed；VF-4b 不增加 parent
verification、worker evidence import 或 public UI/protocol 行为。这些内容仍属于后续 VF-4c。

## 验证

测试覆盖 completed、no-target、conflict、crash recovery、desired-image recovery、pre-apply indeterminate recovery、
post-apply final-verification race，以及每个 logical adoption 只投影一次 mutation 的行为。

[English](../../en/adr/0156-ultracode-parent-workspace-freshness.md) · **简体中文**
