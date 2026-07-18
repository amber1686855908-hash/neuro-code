# ADR 0018 — 限定工作区的交互式会话恢复

**简体中文** · [English](../../en/adr/0018-workspace-scoped-interactive-session-resume.md)

## 状态

已接受。

## 背景

Neuro Code 已能通过 CLI 列出和恢复 SQLite 会话，但第一版 TUI 只能在调用者预先知道 ID
并于启动时传入 `--resume` 时恢复，而且打开后不会回放可见历史。实用的应用内恢复流程
不得跨越工作区边界、静默移植供应商原生状态，也不能在记录中暴露持久化推理或原始工具
输出。

## 决策

- `ProfileConversationController` 同时拥有交互式会话目录与恢复边界。`run`、profile 选择
  和会话选择共用一把轮次锁，因此活动模型/工具轮次中不能替换绑定。
- 组合根最多读取 50 条近期 SQLite 摘要，只保留记录工作区与活动工作区具有相同文件系统
  身份的记录。打开所选 ID 时，`AgentConversation.open` 会再次执行相同检查，因此选择器
  过滤只是易用性边界，不是唯一授权检查。
- `Ctrl+R`、`/sessions` 和不带参数的 `/resume` 打开选择器；`/resume SESSION_ID` 直接
  选择。每行只包含缩短的 ID、更新时间、保存的供应商/模型、就绪状态及当前/备用 profile
  标记，不包含提示内容、端点、凭据或其他工作区路径。
- 优先使用名称与保存来源供应商一致且就绪的已配置 profile。如果该 profile 不存在或未
  就绪，可以用当前就绪 profile 恢复普通消息投影。新的 `AgentConversation` 仍携带保存的
  来源供应商、模型与上下文亲和值，因此每个供应商适配器继续拒绝不兼容的不透明/原生
  上下文。没有就绪 profile 时失败关闭。
- 恢复绑定必须成功打开指定会话 ID 后才能替换活动绑定。重新选择当前会话不执行操作；
  之前的活动会话绝不会被删除或改写。
- 历史回放只是规范 `Message` 项的表现投影。系统消息以及推理/后端工具类型的
  `PreservedContextItem` 会被跳过；绝不渲染 `Message.reasoning_content`、原始工具结果
  内容、工具参数、图片 URL 或供应商原生载荷。用户/助手模型内容投影按每条 20,000 字符
  限制，本地工具调用/结果只变为带名称的已恢复生命周期记录。
- 启动时 `--resume` 与应用内选择使用同一投影。替换可见记录不会改变 SQLite 历史或
  供应商上下文。

## 后果

用户无需离开 TUI 或复制 UUID 就能发现并重新打开近期对话，经过安全投影的导入会话也
可恢复。该能力刻意只服务本地且限定当前工作区。会话标题/内容搜索、删除、跨工作区切换、
远程目录和工具卡片富回放仍属于后续切片。

## 历史源代码证据

固定提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` 中的以下只读路径用于确定会话选择、
加载与回放行为。Neuro Code 保留自己的 SQLite/应用契约，不复制上游控件或远程会话架构：

- `crates/codegen/xai-grok-pager/src/views/session_picker.rs`；
- `crates/codegen/xai-grok-pager/src/app/dispatch/session/load.rs`；
- `crates/codegen/xai-grok-pager/src/app/dispatch/tests/session/load.rs`；
- `crates/codegen/xai-grok-pager/src/sessions_cmd.rs`。
