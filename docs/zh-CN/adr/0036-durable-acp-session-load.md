# ADR 0036：持久化 ACP session load

**简体中文** · [English](../../en/adr/0036-durable-acp-session-load.md)

- 状态：已接受
- 日期：2026-07-19

## 背景

ADR 0035 引入了连接内稳定的 ACP ID，同时内部 SQLite session ID 仍按需创建。标准
`session/load` 要求该客户端可见 ID 能跨进程重启继续使用，并要求 Agent 在 load request
成功前回放对话历史。load response 不会替换请求中的 session ID。

如果把内部数据库 ID 直接当成所有 ACP ID，会破坏现有分离；只在内存中保存映射，则
`session/new` 返回的 ID 会在断连后失效。直接回放原始持久化对象还可能暴露系统提示、
私有 reasoning、供应商原生上下文、工具参数或无界工具输出。

## 决策

- 将 SQLite session schema 升级到 v5，新增带 namespace 的 `session_aliases` 表。一个
  ACP 外部 ID 只映射一个内部 session；一个内部 session 在 `acp-v1` namespace 中至多
  拥有一个 alias。只有其他界面显式删除历史时，外键才会同步删除 alias。
- 运行时发出 `SESSION_STARTED` 时、任何模型或工具工作前持久化 alias。对于不发送该
  事件的 runner，在运行完成或取消后保留 shielded 回退。`session/close` 永不删除 alias
  或历史。
- 当 session 尚无 alias 时，允许旧内部 session ID 作为首次 load 引用，并把该值绑定为
  持久 ACP alias；已存在 alias 的 session 不得创建第二个 alias。
- 声明 `loadSession: true` 并实现标准 `session/load`。请求中的 ID 继续作为活动 ACP
  ID。load 与 `session/new` 使用相同的绝对工作区和空 `additionalDirectories` 规则；
  ADR 0038 随后允许两个方法接收有界且临时的 stdio `mcpServers`。
- 把恢复配置选择放入 `ApplicationComposition`：重新校验文件系统工作区身份和固定
  sandbox；存在已配置的原 provider/model 时重建它；原生上下文亲和不可用时直接拒绝，
  不静默更换来源。
- 完整重建 conversation 与后台任务 scope 后才发布活动 session。并发 load/new 使用
  reservation 防止重复发布；断连同时取消已发布 session 和创建中的任务。
- 只回放可见用户文本、助手文本、工具名称/类型/白名单路径，以及有界脱敏工具结果。
  system message、reasoning、供应商保留上下文、图片引用、`_meta`、raw input/output 和
  任意参数全部省略。
- 回放上限为 2,000 个持久项、4,096 个 update、每个可见消息块 64 KiB、每个工具结果
  32 KiB，以及 2 MiB 序列化 update。发送第一条 update 前先校验完整回放。
- 回放消息使用新的 UUID message ID。工具调用按 `pending` 后接 `completed` 回放；历史
  中未解决的调用发送终态 `failed` update。

## 影响

- `session/new` 返回的 ACP ID 可以由后续 `neuro-code acp` 进程加载，无需暴露内部
  数据库 ID。
- 工作区、sandbox、provider 亲和、畸形 ID、重复活动 session、超量历史和客户端回放
  故障都采用失败关闭。
- 标准 SDK Client 能收到历史并继续同一持久会话；官方 SDK 子进程测试覆盖 close、重启、
  load、回放、继续提示和历史保留。
- SQLite v1-v4 数据库可前向迁移且不改写会话内容。JSON session export 仍是 schema
  version 4，因为 alias 属于界面局部元数据，不是导出的对话内容。
- ACP 界面仍是 partial。后续切片实现了工作区范围的 `session/list`、
  `session/resume`、`session/delete`、`session/fork`、有界且按 profile 门控的额外目录、
  stdio/Streamable HTTP/legacy SSE 的临时 MCP declaration、客户端文件系统/终端调用、
  WebSocket 传输和私有有界 extension。二进制多媒体历史回放、ACP-transport MCP server
  declaration、持久化 MCP 配置、交互式客户端终端 input/resize/PTY framing 与完整一致性仍
  不属于支持边界。
