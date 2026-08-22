# ADR 0129：由应用拥有的受管 Git Worktree 能力

- 状态：首个本地生命周期切片已接受
- 日期：2026-08-22
- 范围：本地 Git worktree 创建、ownership、检查、协调和安全移除

## 背景

Neuro Code 已有规范文件系统身份、工作区 target 解析、按 binding 创建
sandbox、仓库指令发现和按工作区管理的 LSP ownership，但还没有 Git 仓库的类型化
身份或 linked worktree 的持久 owner。裸路径不足以承担该职责：main checkout 与
linked worktree 有不同的工作区根目录，却共享 Git common metadata；而且 `.git`
可能是文件而非目录。

源 checkout 也可能是 dirty 的。创建 worker worktree 不得 stash、commit、clean、
复制或以其他方式改写这些变更。

## 决策

新增一个由应用拥有的能力边界：

```text
ApplicationComposition
        |
WorktreeApplicationService
        |
类型化 GitWorktreePort + ManagedWorktreeStore
        |
LocalGitWorktreeAdapter + SQLite worktrees.db
        |
隔离的受管 worktree
```

该服务是显式能力，不作为任意 Git command tool 暴露。
`ApplicationComposition.create_worktree_service()` 将服务绑定到配置的 state directory；
调用方必须先初始化再使用。同一个配置的 state directory 确定性地拥有 managed root 和
`worktrees.db`，因此不同的 `ApplicationComposition` 实例会重新打开同一份 ownership history。
session cleanup 不拥有也不会删除这个数据库。

### 领域与 ownership

领域层暴露不可变的 `WorktreeId`、`WorktreeRepositoryIdentity`、
`WorktreeCreateRequest`、`WorktreeSnapshot`、`WorktreeHandle`、生命周期状态和
`WorktreeWorkspaceBinding`。snapshot 记录规范 Git common directory、源 worktree、
Git directory、创建时观察到的 repository HEAD、精确 base commit、managed path、
branch mode、ownership、状态和创建元数据。原始 Git porcelain dictionary 不会越过
适配器边界。

managed root 为 `<state_dir>/worktrees/<repository-id>/<worktree-id>`。服务拒绝与源
checkout 重叠、option-like/非法 branch、已存在的 ID、已存在的 target path 和已存在的
branch。默认 managed branch namespace 为 `neuro/worktree/<id>`。

### Git 契约

适配器将 argv-safe 的 `SandboxedProcessRequest` 提交给规范的 `LocalProcessSandbox` 端口；
它不直接创建子进程，也不使用 shell。关闭终端提示，只执行本地命令，并对 stdout/stderr、
时间、取消和子进程终止设置边界。它用 `git rev-parse` 获取仓库和不可变 base commit 身份，
用 `git check-ref-format` 校验 branch，并用 `git worktree list --porcelain -z` 进行 NUL-safe
类型化解析。它不调用 fetch、pull、push、clone 或 prune。由于 revision resolution 使用
`rev-parse --end-of-options`，要求 Git 2.30 或更高版本；更低版本失败关闭。

### 创建

创建先把 `base_revision^{commit}` 解析为不可变 commit SHA 并持久化 `CREATING`，再以
该 SHA 创建 exact detached worktree 或新 managed branch。服务在持久化 `READY` 前校验
path、Git common directory、HEAD 和 branch identity。dirty source checkout 不作为
patch 读取，也不会被修改。

### 移除

移除要求同时具备 durable managed record、匹配的 repository identity、规范 path、期望
HEAD、期望 branch/ref 和实际 Git record。使用不带 `--force` 的 `git worktree remove`。
dirty 或 locked worktree 返回类型化失败并继续归 Neuro Code 所有。未知、移动、缺失或
不匹配的 path 不会使用 `rm -rf` 删除。移除 worktree 后保留 managed branch；删除 branch
属于未来独立能力。

### 持久化与协调

`worktrees.db` 使用独立版本化 schema，不与 session turn recovery 混合。重新打开时会检查
schema version，未知版本失败关闭；schema upgrade 仍属于未来显式 migration。SQLite 与 Git
metadata 不是同一个事务：

| Durable state | 实际 Git state | 分类 | 动作 |
| --- | --- | --- | --- |
| `CREATING` | exact worktree 存在 | ready | 校验后提升为 `READY` |
| `CREATING` | 不存在 | failed | 保留为 `FAILED` |
| `CREATING` | 该 path 有无关目录 | orphaned | 保留并失败关闭 |
| `READY` | exact worktree 存在 | ready | 刷新有界 status |
| `READY` | 缺失或不匹配 | orphaned | 保留 ownership record，不删除 |
| `REMOVING` | remove intent 后已缺失 | removed | 提升为 `REMOVED` |
| `REMOVING` | exact worktree 仍存在 | ready | 协调一次失败的移除 |
| 任一 active state | 仓库缺失或 common-dir 不匹配 | orphaned | 不执行文件系统清理 |

协调是显式操作，managed list/inspect 也会使用它。因此进程可能在 durable intent、Git
action 和最终化之间退出时，仍可在不声称跨系统 ACID 的前提下恢复状态。

### Workspace、sandbox 与 LSP 接缝

`WorktreeWorkspaceBinding` 从 ready 的不可变 handle 得到一个规范 primary root，且不继承
additional roots。该 binding 可以交给现有 filesystem target resolver、sandbox factory
以及未来按工作区管理的 LSP manager。本切片不创建 writable subagent，不共享 source 的
document cache，也不实现 integration。

## 不变量

| 不变量 | 状态 |
| --- | --- |
| Neuro Code 只移除能够证明 ownership 的 worktree | PROVEN：服务 guard 与移除测试 |
| 源 checkout dirty state 保持不变 | PROVEN：真实 Git 集成测试 |
| repository/path/base identity 不可变且会校验 | PROVEN：创建/移除路径 |
| Git 执行 argv-safe、有界且 local-only | PROVEN：适配器实现与 parser 测试 |
| SQLite intent 与 Git state 可在进程退出后协调 | PROVEN：真实 child `os._exit()` 测试 |
| dirty、locked、mismatch、unmanaged 不会被强删 | PROVEN：dirty/locked/path-reuse；mismatch 失败关闭 |
| Worktree 是独立 workspace root | PROVEN：规范 filesystem binding 集成 |
| 不产生隐式网络 Git 操作 | PROVEN：本地命令 allowlist |

## 未实现

Checkpoint/rollback、patch 或 commit integration、merge/cherry-pick/rebase、冲突解决、
自动删除 branch、dirty-state 复制、面向模型的 Git/worktree tool、writable subagent、
relay/DAG/leader/swarm 以及 automatic Ultracode delegation 都不属于本 ADR。

## 验证

focused real-Git suite 覆盖 porcelain parser、类型化领域校验、SQLite reopen round trip、detached
与 managed-branch 创建、dirty source preservation、branch collision、locked/dirty 移除拒绝、
规范 workspace binding、path reuse、remove failure、timeout/output bound，以及创建和移除的
进程退出后 reconciliation。发布前仍需完成完整本地仓库验证。
