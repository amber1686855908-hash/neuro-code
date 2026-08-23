# ADR 0131：串行受管 Writable Subagent 工作区

- 状态：已实现为显式内部纵向切片；最终评级等待 exact-head CI
- 日期：2026-08-23
- 范围：一个有界 writable child、一个 Neuro Code 拥有的 managed worktree 和一个保留的 baseline

## 背景

现有 `/subagent` 工作流、CLI、TUI 和 ACP 路径有意保持只读。Writable child
不能把 `allow_writes` 之类的布尔值或裸路径当成 authority，不能继承 parent
conversation，也不能修改 dirty source checkout。因此首个 writable 能力需要一个窄的、
显式的边界，使成功、失败、取消和进程退出后的状态都能检查。

## 决策

新增由 `ApplicationComposition.create_writable_subagent_service()` 构造的内部
`WritableSubagentApplicationService`。它不接入 `/subagent`、CLI、TUI、ACP、自动调度、
LSP worker 或任何 checkpoint/rollback orchestration。调用在进程内串行化，SQLite lease
提供跨进程 active-parent 与 active-worktree 唯一性边界。

Service 只有在以下步骤完成后，才从类型化的
`ManagedChildWorkspaceGrant` 派生 child authority：

1. 读取 parent repository identity 和 exact committed HEAD；
2. 插入 `ALLOCATING` lease；
3. 从 exact SHA 在所有 parent workspace roots 之外创建 `MANAGED_BRANCH` worktree；
4. 捕获一个 `READY` baseline workspace checkpoint。

Grant 绑定 grant ID、parent capability fingerprint、parent root 与 repository identity、
exact base SHA、不可变 `WorktreeHandle`、managed worktree ID、canonical child root、创建时间
和 baseline checkpoint ID。Child 使用全新的 session 和全新的 binding，其 cwd 与唯一 workspace
root 都必须是该 managed worktree。

### Child capability

Effective child tool set 是显式 parent 与 global policy 的安全交集，范围只有：

- read：`read_file`、`read_files`、`list_dir`、`list_tree`、`glob`、`grep`、`grep_many`、`skill`；
- write：`search_replace`、`apply_patch`。

不授予 Bash、terminal、background task、MCP、network、Git/worktree/checkpoint/rollback、
subagent 或 LSP authority。Parent 与 global policy 都必须暴露两个 write tool、filesystem
write authority 和 writable sandbox profile。通用
`SubagentCapabilitySet.is_subset_of()` 语义保持不变；只有 typed grant 可以替换继承式
workspace-root authority。

Child 每一次写入仍然执行 Permission → canonical filesystem target → execution → sandbox
的正常 pipeline。Tool 不会把裸 grant path 当成这些检查的替代物。

### Lifecycle 与保留

Durable lease 使用 `ALLOCATING`、`WORKTREE_READY`、`BASELINE_READY`、`ACTIVE`、`PRESERVED`、
`ORPHANED` 和 `FAILED`。Lease identity 不可变；transition 使用 insert-only/CAS 与 generation。
Child 成功或失败后不会自动移除 worktree、恢复 baseline、merge、commit、copy-back 或删除
保留的 checkpoint。无法证明 final workspace 时记录为 `ORPHANED`，不会伪造干净成功。

Reconciliation 检查 managed worktree 和 baseline checkpoint，验证 identity/state，并在 owner
死亡或证据缺失时分类；不删除不确定数据。它与 `session_turn_attempts` 分离，不从 execution
attempt 推断 workspace recovery。

### Result projection

调用方只接收有界且脱敏的 projection：parent task 与 child session ID、终态、response、
steps/outcome、worktree ID、baseline checkpoint ID、exact base SHA、capability/grant
fingerprint、final workspace fingerprint、changed/count metadata 和 truncation。它不包含完整
diff、transcript、raw tool arguments、凭据或 raw file contents。

## 未实现

Writable parallel workers、递归 writable subagent、自动委派、CLI/TUI/ACP 暴露、自动
checkpoint/rollback policy、child 完成后的自动 rollback、merge/commit/patch integration、
copy-back、branch 删除，以及 checkpoint/worktree cleanup，仍是未来独立能力。

该能力继承现有 Worktree filter preflight contract，要求 Git 2.40.0 或更高版本；更低版本在
初始化时失败关闭。

## 验证边界

只有 focused writable tests、完整本地验证和 exact-head Linux/macOS/Windows/package CI 全部
绿色时，才可接受该切片。此前必须称为 partial，不能称为产品级完整 writable orchestration
能力。
