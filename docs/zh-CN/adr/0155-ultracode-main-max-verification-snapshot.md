# ADR 0155：持久化 MAIN_MAX 验证 snapshot

- 状态：Accepted
- 日期：2026-09-07
- 范围：VF-4a 有界本地 Ultracode 集成

## 背景

VF-3b 已经为普通 Agent 的 retry 与 recovery 在 `TurnInput` 中持久化可选的不可变
`VerificationRequirementsSnapshot`。此前显式 Ultracode entry 拒绝所有结构化 request，导致
`MAIN_MAX` 分支无法保持同一个 verification contract。`BOUNDED_SWARM` 在本切片仍不接入 worker-level
verification，因此也不能接收结构化 snapshot。

## 决策

`MAIN_MAX` 在创建 parent `TurnInput` 或 durable Ultracode execution 之前冻结一个 effective parent
verification snapshot。当 request 为 `None` 时，只调用一次 `NormalTurnRequirementsPolicy.resolve(None)`。
显式非空 snapshot 和显式空 snapshot 已经是调用方拥有的决定，必须原样保留。

同一个 snapshot 同时由 `TurnInput` 和不可变 `UltracodeExecution` identity 携带。Retry 或 recovery 若提供不同
snapshot，则属于 identity conflict。新的结构化 `BOUNDED_SWARM` request 在创建 parent session 和 durable branch
claim 之前被拒绝；Legacy BOUNDED_SWARM request 继续使用 `None`。

## 持久化表示与迁移

Schema 30 在 `orchestration_ultracode_executions` 中增加两个 nullable column：

- `verification_requirements_json`
- `verification_requirements_fingerprint`

两个值均为 NULL 时永久表示 Legacy 模式。结构化 row 必须包含规范、有界的 snapshot 及匹配的 fingerprint。
Partial、损坏、超限或被篡改的 projection 都会 fail closed。Schema 29→30 migration 只增加这些 column，保留旧 row
与 Legacy 行为；不增加新表或其他数据库 schema。

## 恢复

既有普通 Agent runtime 继续是 verification 和 final-response authority。若 MAIN_MAX parent turn 已经 durable
提交，恢复通过专用 `replay_committed_turn` projection 发布精确的已提交 result。它不会调用 Provider、重新运行
verification 或 Finalizer、创建第二个 attempt，也不会使用 BOUNDED external-result commit boundary 制造结构化
verified completion。

## 非目标

本切片不增加 BOUNDED_SWARM worker/Leader verification、result adoption verification、requirement inference、自动发现、
TestRunner、generic scope algebra、public UI/protocol field 或新的 verification truth owner。`VerificationTracker` 与
`FinalResponseContract` 继续是现有 canonical owner。

## 验证

Focused Ultracode tests 覆盖 default/explicit/empty snapshot、identity 与 fingerprint 绑定、schema migration、损坏/篡改
row、Legacy recovery、无重复 execution 的结构化 MAIN_MAX recovery，以及结构化 BOUNDED_SWARM 的 fail-closed request。
仓库质量门禁和完整测试套件仍是本次变更的必要验证。
