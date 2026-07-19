# ADR 0037：工作区范围 ACP session list

**简体中文** · [English](../../en/adr/0037-workspace-scoped-acp-session-list.md)

- 状态：已接受
- 日期：2026-07-19

## 背景

只有客户端已经知道 ACP session ID 时，持久化 `session/load` 才能发挥作用。标准
`session/list` 允许客户端发现持久会话，但当前进程有意只绑定一个启动工作区。直接列出
整个数据库会泄露无关工作区元数据；返回内部 SQLite ID 也会破坏 ADR 0036 的协议/存储
身份分离。

标准 request 不允许客户端指定 page size。它接受可选绝对 `cwd` 和 opaque cursor；
`SessionInfo` 只暴露 session ID、工作目录、可选标题与最后更新时间，以及可选额外根和
`_meta`。

## 决策

- 声明 `sessionCapabilities.list = {}` 并实现 SDK 的稳定 `session/list` 路由。不声明
  delete、resume、fork 或 additional directories。
- 未提供 `cwd` 时仍按连接绑定工作区过滤。提供的 `cwd` 必须是绝对路径并标识同一个
  工作区。该连接绝不列出其他工作区。
- 只返回已有持久 ACP alias、记录的绝对 `cwd`、有界/脱敏持久标题和 ISO 8601
  `updatedAt`。省略 `_meta`、`additionalDirectories`、provider/model、对话内容、工具
  数据和私有上下文。
- 可列出的持久 session 尚无 ACP alias 时，原子分配随机 `acp-<UUID>` alias。并发进程
  通过 schema v5 唯一约束收敛到同一个 alias；该 alias 随后可直接用于 `session/load`。
- 按规范化更新时间和内部 ID 降序分页。内部 ID 只存在于 store/cursor 状态，绝不编码到
  客户端可见 token。
- 每页返回 50 行；每个 request 最多以 250 行一批扫描 5,000 个数据库行，使大量其他
  工作区记录的过滤仍保持有界。扫描达到上限时可以返回空页和 continuation cursor。
- 生成随机、连接局部的 cursor token，最多保留 256 个 cursor 状态。校验 cursor 字节
  长度和控制字符；未知或已淘汰 token 返回稳定非法参数错误。断连时清空全部 cursor。
- list 对对话历史和运行时生命周期保持只读。它唯一可能产生的写入是持久 alias；不会
  load conversation、打开后台 scope 或暴露会话内容。

## 影响

- 标准 Client 可以发现 session、展示安全元数据，并把稳定 ID 交给 `session/load`。
- 无过滤 request 仍保持工作区隔离，cursor token 不泄露内部 ID 或跨工作区元数据。
- keyset pagination 避免无界 offset，并对未变化行保持确定性。并发 session 更新可能让
  行在分页窗口之间移动；在没有数据库 snapshot 时，这是正常的尽力 cursor 语义。
- ACP 实现仍是 partial。session resume/delete/fork、非 stdio MCP 传输与非工具 MCP
  能力、额外目录、多媒体 prompt/历史、客户端文件系统/终端调用、WebSocket 传输和
  自定义扩展仍不支持。
