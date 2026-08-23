# ADR 0132: Worker-scoped LSP runtime integration

- Status: implemented as an explicit internal vertical slice; final rating waits for exact-head CI
- Date: 2026-08-23
- Scope: serialized writable worker, managed child worktree, and ephemeral read-only LSP runtime

## Context

ADR 0128 established a read-only, workspace-filtered LSP capability. ADRs
0129-0131 established managed worktrees, durable baseline checkpoints, and a
serialized writable child whose authority comes from the actual parent
`ConversationBinding`. The remaining integration risk was not protocol
support: it was ensuring that a writable worker's semantic service used the
same managed child identity, did not share parent or sibling state, observed
explicit child writes, and ended with the worker rather than the application.

## Decision

Extend the existing writable-subagent runtime seam; do not introduce a second
Worker hierarchy. The runtime remains one serialized child. Its construction
chain is:

```text
actual parent ConversationBinding
  -> parent capability intersection global policy intersection bounded worker request
  -> managed worktree
  -> READY baseline checkpoint
  -> fresh child session and ConversationBinding
  -> optional read-only lsp tool
  -> fresh child-rooted LanguageServerManager
```

### Authority and workspace identity

`lsp` is an optional bounded read tool. It is present only when the actual
parent binding and composition-owned global policy both expose it. The generic
capability subset checks remain strict; no boolean or caller-reported manifest
bypasses them. The write set remains exactly `search_replace` and
`apply_patch`. Bash, terminal, background tasks, MCP, network, Git, worktree,
checkpoint, rollback, and recursive subagent tools remain absent.

The `ManagedChildWorkspaceGrant` derives a `WorktreeWorkspaceBinding` from its
immutable managed `WorktreeHandle`. Runtime construction fails closed unless:

```text
binding cwd
  == effective capability cwd
  == WorktreeWorkspaceBinding.primary_root
  == LanguageServerManager.workspace_root
  == canonical managed child root
```

The binding and manager have no additional workspace roots. Parent roots,
sibling worktrees, and the controller state directory are not inherited.

### Read-only process and path boundary

The existing `LspTool`, canonical `FilesystemAccessPlan`, URI projection, and
permission visibility boundary are reused unchanged. Input paths and
server-returned file URIs are canonicalized and checked against the child root;
parent, sibling, state-directory, lexical escape, and link-like aliases are
filtered. `workspace/applyEdit` remains explicitly rejected, and no rename,
format, code-action mutation, or execute-command mutation is added.

Each LSP server starts lazily through the child binding's
`LocalProcessSandbox`, with child cwd/profile, one read-only child filesystem
root, argv-safe execution, and the existing bounded process lifecycle. It does
not use the parent sandbox object or a shell.

### Isolation and synchronization

Every binding already receives a fresh `LanguageServerManager`; therefore a
parent and child, and two workers with the same relative filename, have
different managers, clients, routes, document versions, diagnostics, and
restart counters. No singleton or absolute-path document cache is introduced.

The manager re-reads the canonical document bytes before each semantic
operation and sends `didOpen` or a versioned `didChange` when the fingerprint
changes. A real `search_replace` followed by `lsp` therefore observes the
post-write child bytes, while the parent LSP continues to observe parent bytes.

### Binding-owned ephemeral lifetime

`ConversationBindingResourceScope` owns the binding's ephemeral LSP manager and
background-task scope. It uses one shared close task, so asynchronous close is
idempotent and continues if an individual waiter is cancelled. Writable and
read-only subagent runtimes close their binding. Worker success, provider
failure, cancellation, or timeout therefore closes the LSP client/process and
releases route/document caches immediately. Closing an idle or already failed
manager also converges safely. Application shutdown remains the fallback owner
for bindings that are still open.

This close path does not remove or roll back durable evidence. Managed
worktrees, baseline checkpoints, leases, and child sessions retain the existing
preservation/classification semantics. LSP process, route, document,
diagnostics, and restart state are never stored in SQLite and are reconstructed
by a future binding after restart.

### Instruction and skill isolation

The existing per-binding `InstructionTracker` and `SkillTracker` are created
from the selected child cwd. Because the worker binding has the managed child
as its sole root, discovery observes the committed child copy and does not read
dirty parent `AGENTS.md`, dirty parent `SKILL.md`, or parent additional roots.
Parent transcript/context reuse is not added.

## Not implemented

Parallel workers, Parent Context/Relay, DAG/Leader/Swarm/Ultracode
orchestration, Bash or terminal workers, writable LSP operations, automatic
delegation, commit/merge/cherry-pick/patch integration, conflict resolution,
workspace retirement, and automatic worktree/checkpoint cleanup remain future
capabilities. CLI, TUI, ACP, and `/subagent` exposure are unchanged.

## Validation boundary

Acceptance requires authority boundary tests, two-worker and parent/child LSP
isolation, real Tool-to-stdio-LSP synchronization, the server-returned escape
matrix, explicit `workspace/applyEdit` rejection, success/failure/cancel/timeout
process cleanup with preserved workspaces, full local quality gates, and the
exact PR merge-ref Linux/macOS/Windows/package matrix.
