# ADR 0038：Session-owned stdio MCP 工具

**简体中文** · [English](../../en/adr/0038-session-owned-stdio-mcp-tools.md)

- 状态：已接受
- 日期：2026-07-19

## 背景

在本 ADR 接受时，partial ACP v1 适配器此前拒绝所有非空 `mcpServers` 请求。ACP stdio MCP
server 属于基线输入，而 HTTP、SSE 与 ACP 传输拥有独立的可选能力标志。支持 stdio 基线可以
关闭当时最大的兼容缺口，同时不声明这些可选传输。

Neuro Code 不得实现私有 MCP Schema 或 JSON-RPC 调度器，同时必须保持比官方 MCP SDK
传输更严格的进程树保证：POSIX 子进程属于独立进程组；Windows 入口必须在创建时原子加入
关闭即终止的 Job，不能先 spawn 再附加。

MCP server 及其 annotation 均不可信。因此配置、环境变量、工具目录、参数、结果、
stderr、取消与清理都需要显式上限和失败关闭所有权。

## 决策

- 把官方 MCP Python SDK 固定在 `mcp>=1.28.1,<2`。使用其 `ClientSession`、Schema、
  版本协商、JSON-RPC 调度、工具分页、调用与结果类型。
- 在本 ADR 接受时，只接受 ACP `McpServerStdio`。由于当时没有实现可选 HTTP/SSE 传输，保持
  ACP `agentCapabilities.mcpCapabilities` 缺省。后续有界切片增加了 Streamable HTTP 与 legacy
  SSE MCP 传输；ACP-transport MCP server declaration 仍以 `mcp_transport_unsupported` 拒绝。
- 对 server 数、名称、命令、参数、环境变量、序列化配置、工具页/数量、工具名、Schema、
  frame、JSON 复杂度、调用参数、结果与超时实施上限，并忽略 `_meta`。
- 在连接工作区内启动 server，环境只包含 SDK 定义的少量安全继承值和有界客户端值。
  拒绝覆盖活动 provider/proxy 环境变量名，并把每个显式值都作为脱敏来源。
- 保留官方 session 与 dispatcher，同时通过现有 `ProcessTree` 桥接其类型化消息。桥接
  只负责有界 UTF-8 换行分隔传输，不实现 MCP request 路由或 Schema 解释。MCP stderr
  会被排空并丢弃，避免阻塞子进程、污染 ACP stdout 或暴露凭据。
- 在发布 `session/new` 或 `session/load` 前初始化每个 server，并校验完整初始工具目录。
  拒绝远端工具重名以及与内建工具冲突。MCP 配置是临时资源；加载持久 session 时必须
  重新提供。
- 只投影工具。清理并脱敏文本、结构化内容和 ResourceLink 元数据；绝不解引用
  ResourceLink。图片、音频与 embedded body 使用有界占位符省略。
- 无论不可信 annotation 如何，都把每个 MCP 工具视为有副作用。在
  bypass/always-approve 之上加入精确 ASK 规则，同时保留本地显式 DENY 优先级。现有
  runtime 因此继续保证 pending → client permission → in-progress → terminal 顺序。
- 每个 server 串行执行调用。prompt 取消、调用超时、传输故障、close 或断连时，中止
  官方 SDK request，并在结束工具调用前终止完整 server 进程树。session 清理幂等且与
  其他 session 隔离。

## 影响

- 标准 ACP client 可以为新建与加载的 session 提供有界 stdio MCP 工具 server，而不会
  扩大连接工作区或绕过本地安全策略。
- ACP/MCP Schema 与调度仍由官方 SDK 持有，Neuro Code 同时保留创建时原子加入的 Windows
  Job 与 POSIX 进程组不变量。
- 被取消或终态不确定的调用会使该 server 在本 session 的后续调用中不可用。这种保守
  行为可防止未确认的远端副作用继续运行。
- 本 ADR 记录最初仅支持 stdio 的 ACP MCP 切片；更广义的 ACP adapter 仍属于 partial ACP v1。
  后续有界 ACP 切片现在提供 Streamable HTTP 与 legacy SSE MCP transport，以及私有有界的 resource/resource-template
  发现、resource 读取、prompt 发现/获取、sampling/elicitation callback 和动态工具目录刷新。
  ACP-transport MCP server declaration、配置持久化以及 MCP 多媒体/embedded 结果投影仍不支持。
