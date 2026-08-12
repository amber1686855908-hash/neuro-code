# ADR 0067：TUI 中会话作用域的有界工具输出详情

## 状态

阶段 5BN 已接受。

## 背景

阶段 5BK 只把确实被工具预览上限截断的本地输出保存为已脱敏且有界的
artifact。阶段 5BL 增加不透明句柄读取器，阶段 5BM 增加
`SessionToolOutputArtifactApplicationService`，在读取前确认句柄确实由当前会话记录。
TUI 之前只能显示事件中的有界预览，无法安全查看剩余输出。

## 决策

bootstrap 组合根使用现有 `SessionStore` 和 `FileToolOutputArtifactStore` 创建
`SessionToolOutputArtifactApplicationService`，并将这个应用层边界注入
`NeuroCodeApp`。TUI 只保存工具终态事件中的有界 `output_artifact_id`，不会收到文件系统路径或
artifact 存储器。

用户从有界 Inline Peek 继续进入独立 Tool Inspector 时，TUI 才会异步请求当前 runner 会话的
artifact；Summary 与 Peek 永远不会请求或渲染 artifact 内容。应用服务检查持久化会话事件
关联，读取器继续执行不透明句柄路径边界、脱敏和 256 KiB 读取上限。Inspector Output 可滚动，
并会明确说明读取上限或 artifact 存储截断，绝不暗示截断投影是真正完整的原始输出。读取失败时
显示通用的本地化不可用提示。artifact ID、路径、任意 metadata 和异常文本都不会渲染；独立
可选择的 Input 视图会递归脱敏。

读取只在用户展开时发生，并且始终有界；它不改变事件、会话项、Runtime、Provider、权限或 SQLite schema。
Provider 切换和会话切换复用同一个组合服务，但每次读取仍由应用服务重新执行会话授权检查。

## 影响

- 用户可以检查长 Bash 输出，而不把完整输出放进模型可见上下文或会话事件载荷。
- 缺失、删除、损坏或跨会话 artifact 会安全降级为 UI 提示，不暴露存储细节。
- TUI 持有一个只在 Inspector 打开后启动的小型异步 worker。ADR 0108 将 disclosure 改为
  Summary → 单调用 Peek → Inspector；会话授权的应用接缝与流式事件流保持不变。
- CLI、ACP 和其他入站 artifact 视图仍属于后续独立切片。

## 被否决的方案

- 将状态目录路径或 `FileToolOutputArtifactStore` 传给 TUI：这会跨接口层泄露基础设施细节。
- 根据调用方提供的路径读取 artifact：这会绕过持久化会话关联和路径校验。
- 工具完成时自动读取每个 artifact：即使用户从不展开卡片，也会增加 I/O 和内存压力。
- 新增 AgentEventKind 或 SQLite 表：现有工具终态 metadata 和会话事件投影已足以支持这个只读切片。
