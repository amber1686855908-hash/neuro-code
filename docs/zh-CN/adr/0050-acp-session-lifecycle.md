# ADR 0050：ACP 持久会话生命周期

**简体中文** · [English](../../en/adr/0050-acp-session-lifecycle.md)

- 状态：已接受
- 日期：2026-07-28

## 背景

在本 ADR 接受时，partial ACP 适配器已经能够创建、列出、加载、提示、取消和关闭 session，但
客户端还不能删除持久历史、分叉独立对话，或在不回放可见历史的情况下恢复。新增操作必须保持客户端
可见 ACP ID 与内部 SQLite ID 分离，只作用于启动工作区，并继续对 provider、sandbox、
权限、MCP 和后台任务所有权实行失败关闭。

固定的 `agent-client-protocol==0.11.0` SDK 已生成
`DeleteSessionRequest`、`DeleteSessionResponse` 和 delete capability 模型，但 Agent
router 没有注册稳定的 `session/delete` 方法。同一个 router 把 `session/fork`、
`session/resume` 和 `session/close` 放在 unstable protocol 开关之后。

## 决策

- 除 list 外，声明 `sessionCapabilities.delete`、`fork`、`resume` 和 `close`，并继续声明
  `loadSession: true`。
- 为 canonical `SessionStore` port 增加持久 delete 和 fork 操作。SQLite 在现有写锁和
  单个事务内执行每项操作。
- Delete 只接受能够解析到当前连接工作区 session 的有界合法 ACP ID。活动绑定会先执行
  close/cancel 清理，再删除持久行。外键级联删除 event、alias 和搜索文档，搜索 trigger
  同步删除 FTS 行；尚未持久化内部 ID 的新活动 session 只关闭其受控资源。
- Fork 要求来源 session 已持久化，并拒绝仍有活动 prompt 的来源。SQLite 使用新的内部
  ID 和时间戳，复制有序上下文、provider/model 亲和、sandbox profile 与标题，但不复制
  event 或 alias。ACP 分配新的外部 ID，重建普通 binding 与可选 session-owned MCP
  工具，并只在全部资源和 alias 就绪后发布。之后任一步失败都会删除复制行并关闭新资源。
- Resume 复用 load 的工作区、alias、固定 sandbox、provider 亲和、MCP、ID 预留与发布
  检查，但不发送历史 update；load 仍负责回放有界的可见历史。
- 继续使用 SDK 的 stdio stream、`Connection`、dispatcher、生成 Schema 和
  `MessageRouter`。使用 unstable 开关构建官方 router 以启用 fork/resume/close，然后只
  在该 router 上注册已经生成的稳定 delete request。项目不替换或重新解释 JSON-RPC
  framing/dispatch 行为；路由测试会锁定这一兼容接缝，直到 SDK 自身注册 delete。

## 影响

- 标准客户端现在可以发现、恢复、分叉和删除工作区内持久 session，而不会看到内部数据库
  ID。
- Load 与 resume 具有不同的可观察语义：load 回放安全的可见历史，resume 静默恢复上下文。
- 分叉对话在创建时共享不可变前缀，但拥有独立 ID、event、alias、运行时资源和后续历史。
- Delete 按协议意图属于破坏性操作；工作区过滤和稳定 not-found 错误避免其成为跨工作区
  元数据探针。
- ACP 适配器仍是 partial。后续有界切片已实现按 profile 控制的额外目录、Streamable HTTP 与
  legacy SSE MCP 传输，以及有界的私有 resource/prompt/callback/refresh projection；也已实现
  有界 prompt 多媒体输入、客户端文件系统/终端方法、WebSocket 传输和私有
  artifact/subagent/lifecycle/compaction 扩展。二进制多媒体历史回放、ACP-transport MCP
  server declaration、持久化 MCP 配置、交互式 client-terminal input/resize/PTY framing 以及
  完整 conformance 仍在支持边界之外。
