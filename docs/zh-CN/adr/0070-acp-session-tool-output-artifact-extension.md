# ADR 0070：ACP 会话作用域工具输出 artifact 扩展

## 状态

阶段 5BQ 接受.

## 背景

阶段 5BM 增加了会话作用域的有界、脱敏工具输出 artifact 应用服务.
阶段 5BN 和 5BO 已将该服务提供给 TUI 和 CLI.在阶段 5BQ 接受时，ACP 客户端仍无法查看同一份诊断输出.

ACP 0.11 没有标准 artifact 资源或 artifact 列出操作.不过 SDK 会将以 `_` 开头的方法
路由到 agent 扩展处理器.任何扩展都必须保持私有、有界且限定会话,不能变成未记录的第二套会话协议.

## 决策

增加命名空间明确的私有扩展方法 `_neuro-code/session/artifacts`.

请求载荷只有以下两种有界形式:

- `{ "sessionId": "...", "limit": N }` 列出最多 100 个 artifact 句柄;
- `{ "sessionId": "...", "artifactId": "...", "maxBytes": N }` 读取一个已关联 artifact,
  上限为 256 KiB.

ACP 适配器通过已有 `acp-v1` alias 命名空间解析 ACP 外部会话 ID,再委托给
`AcpApplicationService`,由它继续委托 `SessionToolOutputArtifactApplicationService`.
ACP 不读取状态目录,也不会接收内部会话 ID.

响应只包含不透明 artifact ID、字节数、事件序号、截断事实和有界脱敏内容.路径、原始事件 metadata、
工具参数、secret 和存储异常绝不会被序列化.格式错误或跨会话句柄会以稳定协议错误拒绝.
该扩展不会作为标准 ACP capability 宣告。后续有界 ACP 切片已为 MCP、subagent、lifecycle
和 compaction projection 定义其他 namespaced method；超出这些已接受边界的方法仍不支持。

## 边界

- 不修改 ACP schema 或标准 capability.
- 不修改 SQLite schema、事件类型、Runtime、Provider、Finalizer、权限或 TUI 行为.
- 现有应用服务仍是工作区所有权和持久化事件关联的授权边界.
- ACP 列出/读取操作只读,不会创建或修改会话状态.

## 被否决的方案

- 向 `SessionInfo` 增加 artifact 字段:会让标准会话目录意外读取事件和文件系统.
- 返回文件系统路径或原始 metadata:会绕过不透明句柄边界并暴露基础设施细节.
- 宣告新的标准 capability:ACP 0.11 没有兼容的标准 artifact capability,因此使用明确且需调用方选择的私有命名空间扩展.
