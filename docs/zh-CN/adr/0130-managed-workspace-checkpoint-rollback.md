# ADR 0130：受管工作区 Checkpoint 与 Rollback

- 状态：首个内部纵向切片已接受
- 日期：2026-08-23
- 范围：一个 READY、由 Neuro Code 拥有的受管 worktree 的持久化源状态 checkpoint 与精确 rollback

## 背景

Neuro Code 中“checkpoint”有三个不同含义：

1. `EXECUTION_SEGMENT_CHECKPOINTED` 是有界长任务的进展/审计标记。
2. `session_turn_attempts` 记录请求/输出/工具耐久性，用于崩溃与
   `INDETERMINATE` 恢复。
3. Workspace checkpoint 是应用拥有的某个受管 worktree 源状态投影快照。

第三种能力不得复用前两种 store 或语义。Workspace rollback 具有破坏性，
不能从裸路径推断 authority，也不得改写 source checkout 或 Git history。

## 决策

新增一个显式的内部 application capability：

```text
WorktreeHandle
    |
WorkspaceCheckpointApplicationService
    |-- ManagedWorktreeStore       （ownership proof）
    |-- WorkspaceStatePort          （Git index + 有界源投影）
    |-- checkpoints.db              （不可变 target + rollback attempt）
    `-- state_dir/checkpoints/      （原子 content-addressed artifacts）
```

`ApplicationComposition.create_workspace_checkpoint_service()` 可以构造该能力，
但本 ADR 不启用面向模型的 tool、自动 policy、TUI command、LSP binding、
writable subagent、worker coordinator、Relay、DAG 或 integration operation。

### Target authority

`CheckpointCreateRequest` 接收 `WorktreeHandle` 或由 `WorktreeId` 得到的 handle，
从不接收裸 filesystem path。Capture 和 rollback 必须同时满足：

- durable worktree record 为 `MANAGED`、`managed=true`、`READY`；
- handle、canonical path、repository common directory、source worktree、Git
  directory、branch/detached identity 与 Git worktree record 匹配；
- managed worktree 仍存在且没有 external lock；
- rollback 观察到 checkpoint 的 exact HEAD；HEAD 改变返回类型化
  `HEAD_MISMATCH`，不会 rewind branch 或 detached history。

Rollback 只作用于现有 managed worktree。source checkout、external worktree、
`ORPHANED`、`REMOVED`、path reuse、替换后的 repository 或 branch mismatch 都在
filesystem mutation 之前拒绝。

### Captured projection

快照是 source-controlled workspace projection，不是任意 filesystem image。它包含：

- repository/worktree identity、HEAD 和 branch/detached 状态；
- exact per-worktree Git index bytes；
- 每个 tracked index path，包括 absent tracked path、staged 与 unstaged content、
  tracked deletion、binary bytes、executable mode 和 tracked symlink target；
- 每个 non-ignored untracked regular file 或 symlink。

Ignored files 有意不进入 projection，也绝不删除或恢复。Empty directory 不属于
rollback authority。Unmerged index stage、intent-to-add、sparse/split index、
submodule、nested repository、special file、link-like parent 以及不支持的平台
reparse form 都以 `UNSUPPORTED_WORKSPACE_STATE` 失败关闭。

实现选择 state directory 中的 content-addressed files 加 canonical metadata manifest。
Raw index 单独保存；regular file content 使用 SHA-256 命名 blob，symlink target 作为
有界 link data 保存且不跟随。Artifact path 只能由 opaque checkpoint ID 生成，内部只
使用 relative path。不使用 SQLite BLOB 保存 source content，也不使用普通 Git stash
lifecycle。

### Fingerprint 与完整性

确定性 fingerprint 对 repository identity、worktree identity、canonical target path、
HEAD、branch/detached 状态、index digest，以及按序排列的 tracked/non-ignored manifest
进行 hash；manifest 包含 mode、kind、presence、size 与 content/link digest。相同投影
得到相同 fingerprint；任一 in-scope content、mode、index、deletion、path 或 identity
变化都会改变 fingerprint。单独的 modification time 不参与 fingerprint。

Capture 先持久化 `CAPTURING` intent，再写有界 temporary artifact，按架构需要关闭并
fsync 文件，计算 metadata/index/blob hash，原子发布最终 checkpoint directory，最后以
CAS 转换为 `READY`。崩溃只留下可恢复的 `CAPTURING` record 或没有 final artifact；partial
directory 不会被提升为 `READY`。Rollback 前验证 manifest、index、blob、size、count 和
root integrity；corruption、truncation、replacement、unexpected artifact file 和
malformed database record 都失败关闭。

硬边界覆盖 file count、untracked count、single-file size、total source bytes、manifest
bytes、index bytes、artifact bytes 和 capture time。超界返回类型化 `CHECKPOINT_TOO_LARGE`；
日志、error 与 UI projection 不包含无界 content。

### Rollback 与崩溃恢复

`RollbackAttempt` 与不可变的 `WorkspaceCheckpoint` metadata 分离。Rollback 在破坏性
worktree 阶段前先持久化为 `STARTED`。Service 使用带 attempt identity 的唯一 Neuro Code
Git worktree lock；已有 external lock 不会被 unlock。该 lock 还使普通 Worktree capability
的 concurrent removal 失败关闭。只删除 current projection 中观察到且 target 中不存在的
exact path，按深度从深到浅处理。不使用 `git clean`、宽泛 recursive delete、reset、checkout、
restore、stash、branch-ref rewrite 或 history rewind。Symlink leaf 只作为 leaf unlink，
绝不 resolve 后删除 target。

Workspace adapter 恢复 tracked/untracked file 与 mode，再原子替换保存的 index。成功必须
满足实际 post-operation projection fingerprint 等于 target fingerprint；process exit code
为零不是成功条件。只有 verification 和 lock release 都完成后 attempt 才变为 `COMPLETED`。
partial restore、lock/index 结果不确定、start 后 artifact issue 或 final fingerprint 失败
都变为 `INDETERMINATE`，绝不伪造成功。

重启时检查 durable `STARTED`/`INDETERMINATE` attempt。存活 owner 仍受保护；死亡 owner 只有
在 worktree identity、exact HEAD 和 artifact integrity 仍然证明安全时，才可通过 CAS claim
并 retry。如果 target fingerprint 已经存在，恢复可以在不重写 source content 的情况下完成
attempt。并发 stale writer 由 SQLite generation CAS 失败；同一 worktree 的两个 rollback
attempt 只有一个确定性的 durable winner。

## 不实现

Automatic checkpoint policy、面向模型的 checkpoint tool、TUI/ACP exposure、checkpoint deletion/
retention、任意 filesystem snapshot、ignored-file rollback、Git history rewind、branch ref
reset、patch/commit/merge/cherry-pick/rebase integration、冲突解决、checkpoint merge 或 diff
UI、writable subagent、automatic LSP worker binding、Relay/DAG/Leader/Swarm/Ultracode orchestration
以及 source-checkout rollback 都不属于本切片。

## 兼容性与验证

最低 Git 版本仍为 2.40.0，因为继承的 filter preflight 依赖
`git check-attr --source=<tree-ish>`。Git 2.39 及更低版本在既有 Worktree capability 边界
失败关闭。Checkpoint capability 必须通过 focused real-Git tests、带 coverage 的完整 pytest、
docs parity、Ruff、format、mypy、lock/build 检查，以及 Linux/macOS/Windows exact-head CI，
然后才能超出本纵向切片评级。
