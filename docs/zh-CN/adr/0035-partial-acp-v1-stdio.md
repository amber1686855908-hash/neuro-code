# ADR 0035：Partial ACP v1 stdio 适配器

**简体中文** · [English](../../en/adr/0035-partial-acp-v1-stdio.md)

- 状态：已接受
- 日期：2026-07-19

## 背景

Neuro Code 需要标准编辑器/客户端协议界面，但不应重复实现 ACP Schema 生成、JSON-RPC
调度或 stdio framing。现有 CLI 与 TUI 的组合逻辑位于界面私有函数中，而内部会话 ID
可能到首次 prompt 持久化时才创建。在本 ADR 接受时，宣称完整 ACP v1 兼容同样不正确：MCP
server、额外目录、会话发现/恢复、客户端文件系统与终端方法、多媒体内容和 WebSocket 传输
均未实现。

固定的官方 Python SDK 范围为 `agent-client-protocol>=0.11.0,<0.12`，锁文件当前选择
0.11.0。该 SDK 提供标准 `session/close` Schema 类型，但把路由项标为 unstable；它会
忽略 malformed JSON 行，也不会在把响应规范化为 2.0 前拒绝错误的入站 `jsonrpc` 版本。

## 决策

- 新增 `neuro-code acp`，作为明确标注 partial 的 ACP v1 stdio 界面；生产 Schema、
  调度、notification、request 和 framing 全部使用官方 SDK。
- 只为使 `session/close` 可达而打开 SDK unstable router 开关；不实现或声明其他
  unstable 方法与自定义扩展。
- 声明 `sessionCapabilities.close = {}`。实现 `initialize`、`session/new`、
  `session/prompt`、`session/cancel` 和 `session/close`；发送标准
  `session/update` notification，并使用 `session/request_permission`。ADR 0036
  随后加入标准 `session/load` 与真实的 `loadSession: true` 能力声明；ADR 0037 加入
  标准 `session/list` 与 `sessionCapabilities.list = {}`。
- 每条连接绑定到规范化后的启动工作区。拒绝相对或不同的 `cwd`，以及非空
  `additionalDirectories`。在本 ADR 接受时，原先对非空 `mcpServers` 的拒绝已由 ADR 0038
  针对有界 stdio server 部分取代；后续切片增加了 Streamable HTTP 与 legacy SSE MCP server，
  ACP-transport MCP server declaration 仍被拒绝。
- 为每个协议 session 生成稳定 ACP ID，并单独维护到按需创建的内部 SQLite ID 的关系；
  ADR 0036 将该关系持久化。
- 在本 ADR 接受时，只接受有界 Text 与 ResourceLink prompt block。保持顺序，只投影标准白名单
  字段，忽略 `_meta`，转换期间绝不解引用 URI。后续 prompt-content 切片通过 canonical
  content boundary 增加了有界 image、audio 和 embedded resource block。
- 通过显式、有界、脱敏白名单投影运行时事件。默认保持 reasoning 私有，同一回答使用
  稳定 `messageId`，整轮终态只通过 `PromptResponse.stopReason` 表达。
- 按 ACP session 适配现有失败关闭权限路径。客户端批准不能覆盖本地 deny、工作区、
  环境保护或沙箱结论。
- 同一 session 只允许一个 prompt，不同 session 可并行。cancel、close、EOF 和断连
  共享幂等清理，终止受控工作/后台 scope，但不删除历史。
- 提取 `ApplicationComposition`，让 CLI、TUI 和 ACP 共享配置、供应商、存储、工具、
  权限、工作区/沙箱绑定、后台 scope 与关闭，同时不把界面类型导入应用模块。

## 影响

- 标准 SDK 客户端可以驱动已实现核心切片，并且只看到真实存在的能力。
- 即使持久化仍按需发生，ACP ID 也保持稳定，内部 ID 继续使用现有格式。
- ResourceLink 是模型可见引用，不是隐式 I/O 授权。
- 审批与取消按 session 隔离，close 不等于删除会话。
- 进程仍是 partial ACP v1 实现。后续有界切片实现了 session discovery/resume/delete/fork、
  有界且按 profile 门控的额外目录、stdio/Streamable HTTP/legacy SSE 的临时 MCP declaration、
  客户端文件系统/终端调用、WebSocket 传输、有界 image/audio/embedded prompt input，以及
  私有 MCP、artifact、subagent、lifecycle、compaction 和 recovery extension。完整一致性、
  ACP-transport MCP server declaration、交互式客户端终端 input/resize/PTY framing、二进制
  多媒体历史回放和持久化 MCP 配置仍不支持。
- ADR 0050 后续实现了 resume/delete/fork 生命周期切片，但不改变本 ADR 最初的
  partial core 决策。
- 原始 stdio 测试会记录官方 0.11 SDK 的 malformed frame 与 JSON-RPC 版本行为。不会
  用私有生产 parser 或 dispatcher 作为绕过方案；后续可在单独评审的依赖升级中吸收上游
  SDK 变化。
