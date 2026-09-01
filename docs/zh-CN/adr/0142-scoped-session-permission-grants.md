# ADR 0142 — 有范围的会话权限授予

**简体中文** · [English](../../en/adr/0142-scoped-session-permission-grants.md)

## 状态

已接受。

## 背景

此前的 `ALLOW_SESSION` 只缓存一次工具调用及其完整参数的精确 SHA-256 范围。这是安全的，
但普通审查或测试流程每换一个文件、每改变一点命令，就会再次询问。更宽的授权需要减少
提示，同时不能变成全局 Bash、全局编辑或全工作区绕过。

已有的权限策略、规范文件系统计划、Sandbox、capability manifest 与工具执行流水线仍是
彼此独立的 authority。本 ADR 只在这些 authority 之上增加进程内审批记忆层。

## 决策

- 保持精确操作 `ALLOW_SESSION` 不变。其摘要只保存在内存中，并且每次调用仍重新经过
  `PermissionManager`，所以它始终从属于新的策略判定。
- 增加 `WORKSPACE_EDITS` 与 `COMMAND_FAMILY` 两种由运行时生成的有类型候选。候选不是模型
  值，不能由 Provider、Planner、worker、ACP payload 或工具参数创建。代理只有在请求中
  存在完全相同的可信候选时才接受范围授权。
- 增加有类型的 decision source。只有普通 interactive 默认 `ASK` 才能产生宽范围候选；
  显式 `DENY`、显式 `ASK`、模式决定、无头决定和已经允许的调用都不会产生宽范围候选。
  因此显式策略仍强于审批记忆。
- 工作区编辑候选只能在已有不可变 `FilesystemAccessPlan` 证明以下条件后生成：所有目标都
  位于主规范根、没有 link-like traversal、操作只是普通 `CREATE` 或 `UPDATE`，并且不是
  Neuro metadata、checkpoint/internal state 或明显的 credential/key 目标。删除、移动、
  additional root、歧义路径和计划失败仍只能走精确授权或拒绝路径。
- 命令族候选只能由已有的保守 Bash tokenizer 和严格的单命令 classifier 生成。接受的形式
  是 `pytest`（包括已支持的 Python/`uv run` 形式）、只读 `ruff`/`mypy` 检查，以及有界的
  `git` 只读命令。组合、wrapper、嵌套解释器、substitution、重定向、后台执行、绝对路径/
  parent path、不安全选项和高风险命令仍只能精确授权或拒绝。
- 每个候选都绑定可信 logical session identity 与工作区主规范根。代理只在进程内存中保存
  授权，不改变 SQLite schema、导入/导出状态或持久化规则文件。新的 broker/进程不会继承
  以前的授权。
- 排在某个审批之后的等价请求会等待该决定，然后重新检查 grant，再决定是否打开模态框。
  仅允许一次、拒绝或取消只唤醒等待者，不会替等待者授权。格式错误的范围响应会降级为
  仅允许本次，绝不会进入缓存。
- TUI 保持拒绝为初始焦点，只展示规范根和有类型的命令族 metadata，不展示 patch body、
  replacement text、credential 或不受限的完整命令参数。ACP 继续只暴露既有的精确操作选项，
  因而不能伪造远程宽范围。
- 请求/结果审计事件继续保留，只追加有界范围 metadata 与 `cache_hit` 标记。工具启动顺序、
  headless 拒绝、mode、sandbox、capability ceiling、Worktree、Checkpoint/Rollback 和
  Ultracode 行为均不改变。

## 后果

在支持的交互路径中，用户选择有类型范围后，同一主工作区内的普通重复编辑以及同一命令族
的后续命令不再打开新的模态框。没有安全宽范围候选时仍可使用精确授权。审批记忆会在进程
重启时丢失，也不能创建持久策略。

命令 classifier 有意窄于 Shell interpreter。新增命令形式必须单独证明并补充测试；未知或
高风险形式继续要求精确操作授权或失败关闭。`bash` 全局授权、任意 Shell 组合、破坏性
Git/文件系统操作、带网络副作用的命令、终端创建、MCP、Provider tool 和 writable-subagent
capability 构造都没有自动范围授权。

## 验证

Focused 与 production-path 测试覆盖规范多文件编辑、受保护和非主工作区目标、显式策略优先级、
命令族拒绝、session/workspace 隔离、进程内存重启行为、排队审批、取消、TUI Pilot 交互、精确
兼容性及代表性审批疲劳计数。完整仓库验证仍是完成门槛：lock 与 docs parity、Ruff、格式化、
mypy、至少 85% coverage、package build 以及 diff whitespace 检查。
