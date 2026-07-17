# ADR 0015 — 异步交互式工具审批

**简体中文** · [English](../../en/adr/0015-async-interactive-tool-approval.md)

## 状态

已接受。

## 背景

`PermissionManager` 已经提供确定性的 `allow`、`ask` 和 `deny` 策略判定，并保证显式
deny 优先。第一版 TUI 没有安全暂停工具调用并等待用户响应的机制，因此交互组合有意
把未解决的审批转换为拒绝。

审批 UI 不能成为代理运行时的依赖，用户响应不能覆盖策略拒绝；同时必须证明具有副作用
的工具在等待审批、被拒绝或审批等待被取消后都不会启动。

## 决策

- `PermissionManager` 继续作为同步策略引擎；`PermissionApprover` 是独立、可选的异步
  端口。无头组合不提供审批器，保留原有的失败关闭行为。
- 策略结果为 `ask` 时，运行时先产生 `tool_approval_requested`，等待审批器，然后产生
  `tool_approval_resolved`。只有允许结果之后才能出现 `tool_started`。处理器缺失时拒绝；
  取消等待会终止本轮、按 ADR 0016 记录配对的错误结果，并且绝不启动工具。
- TUI 通过 `SessionApprovalBroker` 和模态框提供三种结果：仅允许本次、在本进程会话中
  允许完全相同的操作，或者拒绝。拒绝是初始焦点；`Esc`、`Ctrl+C` 和 `D` 也会拒绝。
  模态框内的 `Ctrl+C` 只拒绝本次请求，不取消整轮。
- 会话批准的作用域是“规范工具名 + 完整参数映射”的 SHA-256 摘要，只保存在内存中。
  每次调用仍会先经过 `PermissionManager`，因此后续显式 deny 不会被此前的会话批准
  绕过。无法规范化为 JSON 的参数，以及无法安全分解的 Bash 命令，都会把会话允许降级
  为仅允许本次。
- 模态框接收有界、由策略层生成的摘要，而不是通用原始参数映射。Bash 会显示有界命令，
  因为命令本身就是待授权操作；搜索替换只显示工作区路径和操作数量，隐藏 old/new 文本；
  patch 内容同样隐藏。
- 审批请求与结果追加到现有会话事件审计日志；不会改变数据库 schema，也不会创建持久
  权限规则。

## 后果

交互式编辑和命令现在可以安全暂停并等待决定，测试也能观察到批准之前工作区保持不变。
精确操作的会话缓存可以减少重复询问，同时不会授权整个工具或命令类别。

批准不会跨进程重启保留，目前也不能创建经过审查的持久 allow/deny 规则。丰富的参数
差异、多个并发智能体的审批队列、用户自定义拒绝反馈，以及 ACP 权限映射仍属于后续
纵向切片。

## 历史源代码证据

固定提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` 中的以下只读路径用于确定行为
边界；本项目不会复制其队列、ACP 和组件实现：

- `crates/codegen/xai-grok-pager/src/views/permission_view.rs`；
- `crates/codegen/xai-grok-pager/src/app/dispatch/permissions.rs`；
- `crates/codegen/xai-grok-pager/src/app/dispatch/tests/permissions.rs`；
- `crates/codegen/xai-grok-pager/src/headless.rs`。
