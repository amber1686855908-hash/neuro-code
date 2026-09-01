# ADR 0148：ACP MCP Configuration 边界

- 状态：Accepted
- 日期：2026-08-30
- 范围：V1 Interface Boundary Consolidation 的第四个结构切片
- 依赖：ADR 0052、ADR 0053、ADR 0056、ADR 0145、ADR 0146 和 ADR 0147

## Context

冻结的 PR #75 HEAD 是
`3e8a8cd6796b886213cf57bc70231b415d07ca5c`。顶层的
`neuro_code.acp` adapter 仍包含 ACP 协议处理、session lifecycle、live MCP callback 与
tool lifecycle、transport，以及 inbound MCP server declaration 的无状态转换。

本切片只提取最后一项职责：将 ACP MCP configuration input 做有界校验，并转换为既有的
application `AcpMcpServerConfig` contract。不迁移 live MCP execution、callback dispatch、
session state 或 transport behavior。

## 提取前的 MCP boundary audit

审计针对 PR #75 exact head 完成，并在移动代码前结束。

### Configuration symbols

以下 symbols 只属于 configuration，构成一个 cohesive boundary：

- `McpServer`：HTTP、SSE、ACP-transport 和 stdio declaration 的 ACP input union；
- `_mcp_string`：有界字符串与 control-character validator；
- `_mcp_server_configurations`：转换入口；
- `_mcp_http_url`：URL validator；
- `_mcp_http_headers`：header validator；
- `_ENVIRONMENT_NAME`、`_HTTP_HEADER_NAME` 和 `_RESERVED_MCP_HTTP_HEADERS`；
- 以 `MAX_MCP_` 开头的 server-name、command、argument、environment、URL、header 和
  configuration total bounds。

上面的 ACP schema input class 由 ACP SDK 拥有。输出的
`AcpMcpStdioServerConfig`、`AcpMcpHttpServerConfig` 和 `AcpMcpServerConfig` contract
仍由 application 拥有，并通过 agent/service seam 消费；interface module 不重复定义它们。

`MAX_MCP_SERVERS` 有意不移动、不重新定义。它是 application ACP contract，同时被具体的
stdio 与 HTTP MCP runtime adapter 消费，因此 canonical owner 仍是
`application.acp.contracts`。

`MAX_MCP_URL_BYTES` 与 `MAX_MCP_CONFIGURATION_BYTES` 因为是 configuration bound，由新的
configuration module 定义。ACP live callback 与 metadata projection 可以复用这些值，但
live validation 和 projection code 仍留在 `neuro_code.acp`。

### 明确留在 boundary 之外的 symbols

审计将 `acp.py` 中其余 ACP/MCP symbols 分类如下：

| 分类 | 现有职责 | 决定 |
| --- | --- | --- |
| `MCP_LIVE_CALLBACK` | `_safe_mcp_callback_payload`、`_mcp_sampling_handler`、`_mcp_elicitation_handler`、`McpSamplingHandler`、`McpElicitationHandler`、`MAX_MCP_SAMPLING_MESSAGES`、`MAX_MCP_SAMPLING_TOKENS`、`MAX_MCP_ELICITATION_MESSAGE_BYTES` 和 `MAX_MCP_CALLBACK_BYTES` | 留在 ACP agent；它们依赖 live client 与 session state |
| `MCP_SESSION_LIFECYCLE` | `AcpMcpTools`、`AcpMcpToolError`、`mcp_tools`、`mcp_tool_names`、`_open_mcp_tools` 与 MCP cleanup | 留在 ACP agent/service lifecycle |
| `MCP_PRIVATE_EXTENSION` | `_safe_mcp_extension_value`、`_mcp_list_payload`、`_mcp_extension`、`AcpMcpQuery`、`AcpMcpQueryError`、`ACP_MCP_EXTENSION` 和 `MAX_MCP_RESOURCE_BYTES` | 作为 private extension protocol behavior 保留 |
| `SESSION_LIFECYCLE` | `NeuroCodeAcpAgent`、`_AcpSession`、session registry、aliases、reservations、publication、activation、fork、cleanup、prompt、cancel 和 permission coordination | 原样保留 |
| `TRANSPORT` | router、connection、stdio stream 和 WebSocket bridge symbols | 原样保留 |
| `SHARED_ACP_VALIDATION` | `serialized_size_bytes`、`RequestError`、`MAX_MCP_SERVERS` 以及复用的 configuration-owned URL/serialized-size bound | 复用既有 owner，不建立泛化 MCP validator bucket |
| `OTHER` | `initialize` 中的 `McpCapabilities` 与其余 ACP schema/session response types | 作为 capability negotiation 或一般 ACP protocol mapping 保留 |

`_safe_mcp_extension_value` 已特别审计，仍留在本切片之外，因为它主要保护 live MCP
sampling、elicitation 和 private extension result projection；它不是 configuration serializer。

### ACP agent call sites 与 capability boundary

`NeuroCodeAcpAgent._validate_session_workspace` 仍是 caller：先校验 workspace，再把
`self._service.protected_environment_variables` 传给 configuration parser。
`new_session`、`load_session`、`resume_session` 和 `fork_session` 继续接收 ACP `mcpServers`，
在 session publication 前校验，并把生成的 application configuration 传给 `_open_mcp_tools`。

Capability negotiation 仍在 `initialize` 中。`_open_mcp_tools`、live sampling/elicitation
callback 构造、session-owned `AcpMcpTools` 和 cleanup 仍在 ACP agent/application service 路径。

protected environment 集合的流向保持为：

```text
ACP agent/service
        -> protected_environment_variables
        -> canonical MCP configuration parser
        -> AcpMcpServerConfig
```

parser 不读取 global state，不扫描 process environment，不读取 bootstrap configuration，
也不从 infrastructure、providers 或 stores 获取 authority。

### Existing behavior 与 tests

冻结 behavior 已由 `tests/test_acp.py` 中的 `McpConfigurationTests` 与 ACP integration tests
覆盖。审计确认其覆盖 valid HTTP/SSE/stdio input、application contract construction、server
与 argument bounds、environment validation/protection、URL/header validation、duplicate 与
reserved names、unsupported ACP transport、serialized configuration bound、session-owned MCP
opening、cancellation 和 cleanup。`tests/test_acp_e2e.py`、`tests/test_acp_raw_stdio.py`、
`tests/test_mcp_stdio.py` 和 `tests/test_mcp_http.py` 覆盖 downstream runtime/protocol surface，
继续作为 integration tests，而不是 parser tests。

## Decision

`neuro_code.interfaces.acp.mcp_config` 作为无状态 ACP MCP configuration conversion 的
canonical owner。它导入 ACP SDK schema、既有 application ACP configuration contract 以及
既有 canonical ACP serialized-size helper。

`neuro_code.acp` 直接导入 canonical function、type alias、helpers 和 configuration bounds，
作为 compatibility alias。它继续是 protocol caller，并继续拥有 live MCP、session、permission
和 transport behavior。

## 支持的 ACP input shapes

支持 behavior 与冻结 behavior 完全一致：

- stdio `McpServerStdio` declaration 转为 `AcpMcpStdioServerConfig`；
- Streamable HTTP `HttpMcpServer` declaration 转为
  `AcpMcpHttpServerConfig(transport="http")`；
- legacy SSE `SseMcpServer` declaration 转为
  `AcpMcpHttpServerConfig(transport="sse")`；
- ACP-transport `AcpMcpServer` declaration 作为 ACP schema input 被识别，但以
  `mcp_transport_unsupported` 拒绝，与之前一致。

没有新增 application contract 或 transport discriminator。name、arguments、environment
tuples、URL、headers 和 transport value 保持原有表示。

## 保留的 stdio validation

parser 保留：

- 非空且有界的 server name 与大小写不敏感的重复检测；
- direct executable command 校验，不进行 shell parsing；
- argument count、单个 argument bytes 和 argument aggregate bytes bound；
- environment count、identifier syntax、大小写不敏感的重复检测、protected-name rejection、
  value bytes 和 aggregate bytes bound；
- empty argument 与 environment value behavior；
- control-character rules，包括 environment value 的既有允许行为；
- 既有 invalid-parameter reason strings。

protected environment matching 继续大小写不敏感，并且只针对 ACP service 传入的集合执行。

## 保留的 HTTP 与 SSE validation

parser 保留：

- HTTP/HTTPS scheme 与 host requirements；
- embedded credentials、fragment、invalid port 和 oversized URL rejection；
- header count、name、value 和 aggregate bytes bound；
- 大小写不敏感的 duplicate header rejection；
- reserved framing/routing header rejection；
- 不做 normalization，保持 URL wire value 原样，且不访问网络。

配置转换期间不会发生 redirect、DNS lookup、socket creation 或 endpoint probing。

## Serialized configuration bound

parser 继续生成相同的 canonical JSON-compatible projection，并在返回 configuration contract
前对该 projection 使用 `serialized_size_bytes`。`MAX_MCP_CONFIGURATION_BYTES` 仍是 UTF-8
serialized payload bound，不替换为 `sys.getsizeof` 或其他内存测量。

## Zero-I/O 与 dependency direction

依赖方向为：

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.mcp_config
        -> ACP SDK schema
        -> application ACP configuration contracts
        -> interfaces.acp.serialization
```

canonical module 不依赖 `neuro_code.acp`、bootstrap、infrastructure、providers 或 stores。它不执行
filesystem I/O、subprocess creation、network access、environment mutation、database access、
provider call 或 MCP connection opening。

## Compatibility 与 private aliases

旧的 `neuro_code.acp` 路径保留 private parser helpers 与 configuration-only bounds 的直接
aliases，因为 repository tests 与内部 call sites 当前使用这些名字。aliases 保持 object identity，
不是 wrappers，`acp.py` 中不再存在重复的 authoritative configuration implementation。移动后的
private helpers 不因 owner 改变而变成 public ACP API，也不加入 interface package barrel。

application configuration dataclass 继续由 `application.acp.contracts` 拥有。
`MAX_MCP_SERVERS` 作为 shared application/runtime bound 继续留在那里。ACP live callback 与
extension code 即使复用 configuration-owned URL 或 serialized-size bound，也仍留在 ACP agent。

## Live MCP、session 与 transport non-migration

本 ADR 明确不移动、不重设计：

- sampling/elicitation callbacks 与 callback payload projection；
- `AcpMcpTools` opening、refresh、tool execution、resource/prompt reads 或 session-owned cleanup；
- private `ext_method` routing 与 `neuro-code/session/mcp` behavior；
- `NeuroCodeAcpAgent`、`_AcpSession`、session registry、aliases、reservations、publication、
  activation、fork、prompt、cancel 或 permission coordination；
- ACP capability negotiation；
- stdio、WebSocket 或 MCP infrastructure transports；
- retry、failure、sandbox、provider、checkpoint/rollback、delegation 或 UI behavior。

## Validation 与 acceptance

本切片增加 focused canonical configuration tests 和 architecture guards，验证 canonical
ownership、direct alias identity、禁止 reverse/concrete dependencies、protected-environment
caller ownership，以及 `neuro_code.acp` 中不存在 authoritative duplicate parser definitions。

Acceptance 要求 focused ACP/MCP tests、ACP raw-stdio 与 E2E tests、repository quality gates、
documentation parity，以及 fully green PR merge-ref CI。不能只凭本地 tests 将 boundary 标记为
frozen。
