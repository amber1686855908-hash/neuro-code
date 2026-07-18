# ADR 0014 — 最小事件流 TUI

**简体中文** · [English](../../en/adr/0014-minimal-event-stream-tui.md)

## 状态

已接受。

## 背景

无头代理循环已经产生统一、只追加的事件流，但可用的终端会话还需要持久多轮上下文、
提示输入、滚动记录、流式反馈和本地命令。直接重写历史终端组件图会让应用状态与 UI
框架耦合，也不符合按纵向切片重写的规则。

因此，第一个 M3 切片需要建立狭窄的交互边界：复用 M2 运行时和会话存储，同时不宣称
已经对齐审批对话框、模型选择、富渲染或平台 PTY 行为。

## 决策

- `AgentConversation` 是面向应用的多轮控制器，负责当前有序会话项、会话 ID、供应商
  来源元数据、恢复时的工作区校验和轮次串行化。无头与交互入口共同使用它。
- Textual 是可选界面依赖。不带子命令和提示运行 `neuro-code` 时打开
  `NeuroCodeApp`；`neuro-code -p ...` 和 `neuro-code agent -p ...` 保留适合机器调用的
  无头路径。
- TUI 渲染 `AgentEvent`，不得直接修改运行时状态。文本增量更新当前响应；供应商
  选择/失败与工具生命周期事件转为有界状态行。后续的稳定消息与本地化设计由
  [ADR 0026](0026-stable-localized-tui-conversation.md) 规定。
- 助手回答使用安全 Markdown 和应用自有语义主题；本地系统/状态/工具/错误行使用对齐的
  标签栏与语义值高亮。模型文本不会作为 Rich/Textual markup，链接点击也被禁用。该
  渲染边界由 [ADR 0027](0027-semantic-tui-and-application-reasoning-effort.md) 规定。
- 表现层使用一套由应用持有的冷色中性深色主题。Textual 内建命令面板的 `Ctrl+P` 和表情符号
  搜索表面会与应用的供应商选择器及纯文字 `/sessions QUERY` 流程冲突，因此将其禁用。
- 提示框上方常驻状态栏，显示当前供应商/模型、压缩后的工作区路径、上下文窗口占用、
  请求/实际思考强度和交互模式。
- 全屏终端模式会周期比较真实 TTY 单元格尺寸与当前 Textual Screen，并且只在二者不同时
  发送正常 resize 事件，用于从缺失的信号或带内尺寸通知中恢复。无头、行内或 Web 驱动
  不安装这项兜底。
- 原始推理增量以及通用工具参数/结果映射不会渲染；有界的有用参数白名单用于调用摘要。
  完成后的调用只会在 [ADR 0029](0029-auditable-in-place-tool-cards.md) 定义的稳定卡片中
  暴露经过控制字符清理、凭据脱敏和长度限制的输出/变更预览。模型步骤、工具和整轮耗时
  使用客户端单调时钟，并遵循
  [ADR 0028](0028-timed-tool-feedback-and-interaction-modes.md)。审批模态框只接收 ADR 0015
  定义的有界操作摘要。
- `/help`、`/status`、`/provider`、`/model`、`/effort`、`/reasoning`、`/mode`、`/cancel`、
  `/clear`、`/quit` 和 `/exit` 在本地处理，不调用模型。`Ctrl+C` 与 `/cancel` 通过受
  所有权管理的轮次 Worker 和 ADR 0016 的恢复契约执行；已配置 profile 选择遵循
  [ADR 0017](0017-safe-interactive-profile-selection.md)，应用层强度选择遵循 ADR 0027；
  `Shift+Tab` 与 `/mode` 选择 ADR 0028 定义的应用自有权限行为。
- 交互组合使用 [ADR 0015](0015-async-interactive-tool-approval.md) 定义的异步、失败关闭
  审批边界，显式 deny 规则仍然优先。`--always-approve` 继续作为显式的高风险覆盖项，
  TUI 不会自动启用它。

## 后果

无头和 TUI 运行现在共享上下文、恢复、存储、供应商路由和权限行为。界面可以通过
Textual 的无头测试 pilot 验证，应用控制器也可以在不导入 Textual 的情况下测试。

这只是 M3 的部分支持。远程模型目录、供应商原生强度映射与工作流编排、首 token 前
无痕回退、插话队列、交互式工具卡片展开、Mermaid/媒体渲染、终端模拟器冒烟覆盖以及
跨平台 PTY 集成仍是独立的后续纵向切片。可恢复的运行中取消由
[ADR 0016](0016-recoverable-turn-cancellation.md) 定义。

## 历史源代码证据

固定提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` 中的以下只读路径用于确定行为
边界；本项目不会复制其 crate 布局：

- `crates/codegen/xai-grok-pager-minimal/src/lib.rs`；
- `crates/codegen/xai-grok-pager/src/views/prompt_widget/mod.rs`；
- `crates/codegen/xai-grok-pager/src/app/event_loop.rs`；
- `crates/codegen/xai-grok-pager/src/slash/command.rs`；
- `crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs`。
