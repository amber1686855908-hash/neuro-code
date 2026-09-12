# ADR 0158：回合工作区 Checkpoint 与 Undo

- Status：Accepted
- Date：2026-09-11
- Scope：B1 仅保留最新普通回合工作区 undo

## Context

现有 checkpoint engine 可以安全捕获和恢复 Neuro Code 自有 managed worktree 的 Git-visible
projection，但普通用户运行使用的是 source checkout。Raw path 不能作为 checkpoint authority，source
checkout 也不能被插入 managed-worktree ownership store。B1 需要为普通回合中符合条件的 mutation 提供一个
durable、仅保留最新目标的 undo，同时不改变 Git history 或 turn recovery。

## Decision

Bootstrap 只有在通过既有 Git port 证明 canonical repository identity、source path、当前 HEAD 以及 branch
或 detached 状态后，才签发类型化的 `SourceWorkspaceCheckpointGrant`。Checkpoint application service 在
capture 和 rollback 前重新证明该 grant；managed worktree handle 继续使用既有 ownership proof。

`TurnWorkspaceCheckpointCoordinator` 在权限批准后、第一次 bounded primary-workspace mutation 之前创建一个
checkpoint。同一回合后续符合条件的 mutation 复用它；Read-only 和 denied operation 不创建 checkpoint。
Unbounded、ignored、unsupported 或无法证明的 mutation 必须先追加 durable `UNAVAILABLE` association；若
写入失败，则拒绝该 mutation。Association 是有界的 `WORKSPACE_UNDO_STATE` session event，只保留
`AVAILABLE`、`ROLLING_BACK`、`UNAVAILABLE` 和 `ROLLED_BACK` 四种最新状态。`ROLLING_BACK` 是内部 durable
transition，绝不会被作为用户可用的 checkpoint 展示。不增加 checkpoint stack，也不在用户界面暴露 checkpoint ID。

受保护的 image 使用现有 projection：tracked 与 non-ignored untracked 文件内容、staged/index 字节、binary
file、平台支持的 symlink 和 mode。Ignored file、工作区外副作用、nested repository、submodule、special
file、empty directory 以及任意外部进程修改不在范围内。Undo 是 idle-only 的用户操作，通过 TUI `/undo` 和
CLI `sessions undo <SESSION_ID>` 暴露；它不调用模型，也不会终止运行中的 mutator。成功 rollback 由现有
fingerprint state machine 校验，并为后续普通回合产生一次 verification mutation handoff；它不改写历史
turn recovery，也不创建第二个 verification generation owner。

## Recovery 与兼容性

进程重启后，`AVAILABLE` association 只有在 source grant、Git identity、HEAD 和 projection safety checks
均精确通过后才有资格使用。Rollback 开始前，association 会记录 expected post-turn projection fingerprint
以及精确的 rollback-attempt identity。Idle claim 与 `ROLLING_BACK` transition 在 durable 层原子完成，因此新
回合不能与 destructive operation 竞争。`UNAVAILABLE` 与 `ROLLED_BACK` 保持终态。

如果 source rollback 中断，并且当前 projection 既不匹配 expected post-turn fingerprint，也不匹配 checkpoint
source，coordinator 不会 restore 或猜测。它要求 checkpoint service 重新证明类型化 source grant、确认没有
live owner，并把精确的 `STARTED` 或 `INDETERMINATE` attempt 通过 compare-and-swap 转换到既有的非活动
`FAILED` 状态。Retirement 只记录被放弃的 attempt 供审计，不表示工作区已经恢复。Live owner 或无法证明的
source identity 会阻止 retirement。对于未知 source state，不执行自动 destructive reconciliation。

`MODEL_OUTPUT_STARTED`、turn recovery 与 committed assistant history 仍是彼此独立的事实。既有 managed checkpoint
API、provider/tool contract、SQLite session schema 和 ACP protocol 不变；不增加 model-visible undo tool。

## Validation

Focused tests 覆盖 typed source authority、dirty/index/untracked/binary/symlink checkpoint 行为、latest-only
association 与 invalidation、第一次 mutation 的并发准备、持久化失败、重启、rollback guard、CLI/TUI projection、
live-mutator refusal，以及既有 managed checkpoint/recovery 回归。
