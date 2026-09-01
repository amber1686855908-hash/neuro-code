# ADR 0051：有界远程 MCP 传输

**简体中文** · [English](../../en/adr/0051-bounded-remote-mcp-transports.md)

- 状态：已接受
- 日期：2026-07-29

## 背景

ADR 0038 建立了由 session 持有的 stdio MCP 工具。ACP 还定义了可选的 HTTP 和 SSE
server 形式。固定的官方 MCP SDK 已提供 Streamable HTTP 和 legacy SSE transport；重复
实现它们的 Schema、协商或 JSON-RPC 会形成不兼容的第二套协议栈。

远程 server 不是 Neuro Code 的子进程。因此它们无法继承使 stdio 取消具有确定性的
POSIX 进程树或 Windows 原子 Job 保证。远程配置与 server 响应仍不可信，可能包含凭据、
代理意外行为、无界响应体或不安全传输输入。

## 决策

- 接受 ACP `McpServer::Http` 作为 Streamable HTTP，并接受 `McpServer::Sse` 作为
  legacy SSE。声明 `mcpCapabilities.http = true` 与 `sse = true`；继续拒绝不稳定的
  ACP 传输 MCP server。
- 继续由官方 `mcp>=1.28.1,<2` SDK 持有 `ClientSession`、Schema、版本协商与 JSON-RPC
  调度。Neuro Code 只负责校验、有界 HTTP client 构造、工具投影、生命周期和权限接线。
- 要求绝对 HTTP/HTTPS URL 且带 host、不含内嵌用户凭据和 fragment，并限制字节数与端口。
  限制 header 数量、名称、值与总大小；拒绝重复、hop-by-hop、framing、routing 与 proxy
  header。每个已配置 header 值都作为脱敏来源。
- 使用应用自有的 `httpx.AsyncClient`：保持 TLS 校验、禁用环境代理继承与重定向，并将
  每个远程响应体限制为 1 MiB。稳定错误原因不会包含秘密。
- 保持 session 所有权：在发布 session 前初始化并校验每个工具目录；在 stdio 与远程
  collection 之间实施冲突和聚合上限；保持配置临时性；创建失败、close、EOF 或断连时
  幂等关闭全部 collection。
- 只投影工具，并把每个远程工具都视为有副作用。现有精确 ASK 高于 bypass 的审批与本地
  DENY 优先级保持不变。
- 远程取消、超时或传输失败时，关闭本地 SDK 连接并让其无法再被调用；不声称仍在执行的
  远程副作用已被停止或成功取消。

## 影响

- 标准 ACP client 可以使用有界 HTTP 与 SSE MCP 工具，而无需自定义 MCP 协议实现或依赖
  环境代理。
- 每个远程工具调用前都会获得用户权限决定；取消保持保守：后续调用会失败关闭，而不会
  复用状态不确定的连接。
- 在本 ADR 被接受时，ACP-transport MCP server、resources、prompts、sampling、elicitation
  和动态 list-change 刷新仍是独立的后续切片。当前有界 ACP adapter 已通过私有 ACP
  extension 提供 resource/resource-template 发现、resource 读取、prompt 发现/获取、
  sampling/elicitation callback 和动态工具目录刷新。ACP-transport MCP server declaration、
  配置持久化以及 MCP 多媒体/embedded 结果投影仍不支持。独立的 ACP server stdio/WebSocket
  transport 由 ADR 0151 说明。
