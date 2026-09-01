# ADR 0132：Worker-scoped LSP Runtime 集成

- 状态：已实现为显式内部纵向切片；最终评级等待 exact-head CI
- 日期：2026-08-23
- 范围：串行 writable worker、managed child worktree 和临时只读 LSP runtime

## 背景

ADR 0128 已建立只读且受 workspace 过滤的 LSP capability；ADR 0129-0131 已建立 managed
worktree、持久 baseline checkpoint，以及从真实 parent `ConversationBinding` 获得 authority
的串行 writable child。剩余集成风险不在 protocol 支持，而在于：writable worker 的语义服务
必须使用同一个 managed child identity，不与 parent 或 sibling 共享状态，能够观察显式 child
写入，并随 worker 而不是整个 application 一起结束。

## 决策

扩展现有 Writable Subagent runtime seam，不引入第二套 Worker hierarchy。Runtime 仍是一个
串行 child，其构造链为：

```text
真实 parent ConversationBinding
  -> parent capability 交 global policy 交 bounded worker request
  -> managed worktree
  -> READY baseline checkpoint
  -> 全新 child session 与 ConversationBinding
  -> 可选只读 lsp tool
  -> 全新且以 child 为 root 的 LanguageServerManager
```

### Authority 与 workspace identity

`lsp` 是可选的有界 read tool，只有真实 parent binding 与 composition-owned global policy
同时暴露它时才会出现。通用 capability subset 检查保持严格；布尔开关或调用方报告的 manifest
不能绕过该检查。Write set 仍严格只有 `search_replace` 与 `apply_patch`。Bash、terminal、
background task、MCP、network、Git、worktree、checkpoint、rollback 与递归 subagent tool
仍不存在。

`ManagedChildWorkspaceGrant` 从不可变 managed `WorktreeHandle` 派生
`WorktreeWorkspaceBinding`。除非以下 identity 全部相等，否则 runtime 构造失败关闭：

```text
binding cwd
  == effective capability cwd
  == WorktreeWorkspaceBinding.primary_root
  == LanguageServerManager.workspace_root
  == canonical managed child root
```

Binding 与 manager 都没有 additional workspace root；不会继承 parent root、sibling worktree
或 controller state directory。

### 只读 process 与 path boundary

现有 `LspTool`、canonical `FilesystemAccessPlan`、URI projection 与 permission visibility
boundary 原样复用。输入路径和 server 返回的 file URI 都会规范化，并针对 child root 校验；
parent、sibling、state directory、词法 escape 与 link-like alias 都会被过滤。
`workspace/applyEdit` 仍显式拒绝，也没有新增 rename、format、code-action mutation 或
execute-command mutation。

每个 LSP server 都通过 child binding 的 `LocalProcessSandbox` 惰性启动，使用 child
cwd/profile、唯一只读 child filesystem root、argv-safe execution 和现有有界 process
lifecycle；它不使用 parent sandbox object，也不使用 shell。

### 隔离与同步

每个 binding 已经获得全新的 `LanguageServerManager`，因此 parent 与 child、以及具有相同
相对文件名的两个 worker，都拥有不同的 manager、client、route、document version、
diagnostics 与 restart counter。本切片不引入 singleton 或按 absolute path 共享的 document cache。

Manager 会在每次语义 operation 前重新读取 canonical document bytes；fingerprint 改变时发送
`didOpen` 或带版本的 `didChange`。因此真实 `search_replace` 之后调用 `lsp` 能观察 post-write
child bytes，而 parent LSP 继续观察 parent bytes。

### Binding-owned 临时生命周期

`ConversationBindingResourceScope` 拥有 binding 的临时 LSP manager 与 background-task scope。
它使用一个共享 close task，因此异步 close 幂等，单个 waiter 被取消时清理仍会继续。Writable
与只读 subagent runtime 都关闭自己的 binding。Worker 成功、Provider 失败、取消或超时后，
LSP client/process 会立即关闭，route/document cache 会释放；关闭 idle 或已经失败的 manager
也能安全收敛。Application shutdown 仍是未关闭 binding 的兜底 owner。

该 close path 不会移除或 rollback 持久证据。Managed worktree、baseline checkpoint、lease
与 child session 继续遵守现有 preservation/classification 语义。LSP process、route、document、
diagnostics 与 restart state 不写入 SQLite；controller 重启后由未来 binding 重新构造。

### Instruction 与 Skill 隔离

现有每 binding 一个的 `InstructionTracker` 与 `SkillTracker` 都从 selected child cwd 创建。
由于 worker binding 只有 managed child 一个 root，discovery 只观察 child 中的 committed copy，
不会读取 dirty parent `AGENTS.md`、dirty parent `SKILL.md` 或 parent additional root。本切片不新增
parent transcript/context reuse。

## 未实现

Parallel worker、DAG/Leader/Swarm/Ultracode orchestration、Bash 或 terminal worker、
writable LSP operation、自动委派、commit/merge/cherry-pick/patch integration、冲突处理、
workspace retirement 和自动 worktree/checkpoint cleanup 仍是未来能力。有界 Parent Context
Relay 是 ADR 0133 定义的后续独立层，不改变本 LSP 契约。CLI、TUI、ACP 与 `/subagent`
暴露保持不变。

## 验证边界

验收要求：authority boundary test、双 worker 与 parent/child LSP 隔离、真实 Tool 到 stdio LSP
同步、server-returned escape matrix、显式拒绝 `workspace/applyEdit`、成功/失败/取消/超时后的
process 清理与 workspace 保留、完整本地质量门，以及 exact PR merge-ref 的
Linux/macOS/Windows/package matrix。
