# ADR 0124 — Runtime parity 与有界集成

[English](../../en/adr/0124-runtime-parity-and-bounded-integration.md) · **简体中文**

## 状态

已接受，适用于当前 pre-alpha Runtime。

## 背景

在本 ADR 接受时，现有设计有意按窄纵向切片交付：安全工具流水线、会话拥有的 MCP 工具、
partial ACP stdio、Provider 无关压缩，以及显式只读子代理。这些边界保证了安全性，但客户端
仍能观察到一些集成缺口：模型请求没有紧凑的重建证据，独立只读工具总是串行，
MCP 能力列表只有 tools，Provider 故障没有有界重试/熔断，用户权限规则也没有持久化。

实现必须保持现有的所有权规则。调度器不能绕过权限/工作区/sandbox 工具流水线执行；
MCP 仍必须由 session 持有；不可信协议内容必须有界且脱敏；Provider 在已经产生可观察
模型输出后绝不能重发请求。

## 决策

以下有界集成作为生产能力交付：

- 每个模型步骤发出不包含秘密的 request snapshot，包含上下文/工具指纹和形状计数；完整
  重建载荷只留在内存中。
- 只读工具调用可以在有界并行组中运行；有副作用和交互控制工具保持 exclusive。完成、
  失败、拒绝、取消和未启动调用都会发出规范终态结果；流水线 hook 观察同一脱敏边界。
- ACP 拥有的 MCP session 会枚举 resources、resource templates 和 prompts，并通过私有
  命名空间扩展支持有界读取、刷新、sampling 和 elicitation。刷新时工具名集合原子替换。
- ACP 接受有界音频和内嵌二进制 prompt 内容。它们保留在模型上下文中；不具备原生媒体
  能力的 Provider 收到安全占位符。WebSocket 换行 JSON 桥接复用 stdio ACP 路由以及同样
  的 session、权限和工作区门禁。
- 显式子代理调度通过 scope-aware 应用服务提供：并行度、重试、超时、深度、递归创建、
  工具名和写能力均受限，并关闭每个新建子 Runtime。
- Provider 适配器具备输出前重试和冷却熔断。TUI 模型发现使用有界原子缓存，只保存模型
  标识，并在分类网络失败时回退到近期数据；凭据绝不持久化。
- 权限规则支持 path/operation 匹配和有界原子 JSON 存储。加载仍由 CLI/composition
  边界显式完成；本地 deny、工作区、sandbox、审批和脱敏检查仍是权威边界。
- 当 session 和 Provider 容量已知时，Runtime 会在安全位置接入上下文压缩；CLI、TUI、ACP
  和 session 应用调用均可显式触发。持久压缩行仍与 Provider 生成分离，SQLite 既有回合
  最终化事务继续负责整回合原子性。

## 兼容性边界

在本 ADR 接受时，本决策不宣称有状态 Responses `previous_response_id` 链、自动 Ultracode 委派、
模型生成标题、任意插件或 hook 执行、ACP-transport MCP server declaration、客户端交互式 PTY
输入/resize/framing，或 Provider 原生音频/视频语义。后续有界切片实现了 automatic Ultracode
delegation/result adoption。二进制内容在 ACP/domain 边界被接受且有界，但二进制历史回放和
Provider 原生处理仍是明确的后续能力。

## 结果

Runtime 现在为 CLI、TUI、ACP、回放和诊断共享证据与结果契约，不重复建立 prompt 或凭据存储。
代价是并行只读执行需要私有 transcript 投影，MCP 元数据只能通过私有扩展暴露，其他外部协议
契约仍需稳定并完成测试后才能扩展。
