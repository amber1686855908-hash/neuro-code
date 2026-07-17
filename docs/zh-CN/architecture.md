# Neuro Code 架构

**简体中文** · [English](../en/architecture.md)

## 设计意图

Neuro Code 采用模块化单体架构。它保留有价值的外部行为，但不会照搬历史上游 Cargo
crate 图。所有交互界面消费同一条带类型的运行时事件流。

所有由项目拥有的公共标识都遵循
[ADR 0013](adr/0013-neuro-code-namespace.md) 定义的 Neuro Code 命名空间。

## 系统边界

Neuro Code 负责本地编排：CLI/TUI、代理轮次、模型适配器、工具、权限、工作区、
会话、扩展和协议端点。它不负责模型托管、训练、专有云中继、Computer Hub 服务或
Web 控制台后端。纯云端能力必须通过显式适配器接入；不可用时必须明确报告，不能
模拟成功。

## 依赖方向

```text
界面层（CLI、TUI、ACP、WebSocket）
                    |
应用层（代理循环、会话、命令、任务）
                    |
领域层（消息、事件、工具、权限、错误）
                    |
端口层（模型、存储、工具、工作区、沙箱、钩子）
                    |
适配器（供应商、SQLite、MCP、Git、PTY、操作系统、HTTP）
```

依赖只能向下。领域模块和应用模块不得导入 UI 框架、供应商 SDK、数据库驱动或平台
实现。适配器实现带类型端口，并且只能在组合根中选择。

## 运行时事件模型

一次代理轮次是只追加的带类型事件流：

1. 接受用户消息；
2. 可选地产生供应商尝试失败/选择事件，随后接收模型文本/推理增量；
3. 接收零个或多个供应商托管工具生命周期事件和/或本地工具调用请求；
4. 产生权限判定和本地工具生命周期事件；
5. 把本地工具结果追加到模型上下文；
6. 开始下一模型步骤或结束轮次；
7. 把事件提交到会话存储。

运行时负责步骤上限、取消、重试和事件顺序。UI 可以渲染事件，但不得直接修改运行时
状态。后台任务必须由 `asyncio.TaskGroup` 或具有关闭契约的显式注册表管理；禁止没有
引用的即发即弃任务。

## 稳定端口

- `ModelProvider`：把有序 `ModelContext` 和工具 schema 转换为模型事件；它暴露所选
  profile 身份和不含密钥的亲和指纹。上下文携带会话来源 profile/模型/亲和元数据，
  供适配器自行作出回放决策。
- `Tool`：发布 JSON schema，并在受限 `ToolContext` 中执行。
- `ToolRegistry`：解析规范工具名称并拒绝重复注册。
- `PermissionManager`：在任何副作用之前返回 allow、deny 或 ask。
- `SessionStore`：追加带版本事件、保留有序 `SessionItem`，并同时提供规范序列与普通
  消息投影。
- `PlatformAdapter`：封装 PTY、进程、信号、路径、剪贴板和沙箱差异。

外部边界的协议模型必须版本化。内部状态优先使用冻结 dataclass 和枚举。未经校验的
字典不得跨越模块边界，已校验的 JSON 载荷除外。

## 供应商 profile 与兼容网关

组合根选择命名 `ProviderProfile`；代理运行时不会按商业供应商名称分支。profile 将线路
协议（`openai-chat`、`openai-responses`、`anthropic-messages` 或
`gemini-generate-content`）与 xAI Responses 等可选方言行为分离。凭据只能是环境变量
引用或通过校验的回环代理占位符，不能作为密钥持久化。

CC Switch 是可选配置源和 HTTP 网关，不是应用依赖。其导出的活动 profile 只在配置
边界转换为内存对象；项目配置优先级更高，CC Switch 数据库和进程控制 API 不会
进入领域层或应用层。详见 [ADR 0010](adr/0010-provider-profiles-and-cc-switch.md)。

可选路由包装器负责一条有序、按需构造的供应商候选链。供应商产生的第一个事件就是
提交点：在此之前发生配置或供应商错误时可以推进到下一个候选项；在此之后发生的错误
会直接终止当前模型步骤。某个候选项一旦成功，同一进程运行期间的选择只会向前推进，
不会回切。尝试失败和选择结果会作为显式运行时事件，而不是隐藏在日志里。无论候选项
直连端点还是经过 CC Switch 网关，规则都相同。详见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)。

每个 profile 还会在构造适配器时解析一个 `HttpClientPolicy`。环境模式把标准代理/证书
环境变量交给 HTTPX；直连模式关闭 HTTPX 环境信任；显式模式从指定环境变量读取一个
代理 URL。解析后的策略为所有供应商适配器提供相同的客户端选项和错误脱敏。代理 URL
不会进入领域事件、配置检查输出或持久化配置。详见
[ADR 0012](adr/0012-provider-http-proxy-policy.md)。

供应商适配器统一文本、推理、工具调用、结束原因和 Token 用量。需要跨工具轮次保留的
供应商专属状态存入可选的 `ToolCall.metadata`，键必须带供应商命名空间；该映射随消息
持久化，应用层把它视为不透明数据。属于供应商工具调用连续性契约的流式 assistant
推理会单独存入仅允许 assistant 使用的可选 `Message.reasoning_content`。OpenAI 兼容
对于新生成的轮次，OpenAI 兼容适配器只会在同一条 assistant 消息包含工具调用时回传
该字段；已完成且没有工具调用的推理不会回传。供应商亲和的导入可见推理遵循 ADR 0007
定义的独立有序投影。

终态 `ModelCompleted` 事件还可以携带供应商原生保留项和规范响应文本。运行时会把这些
项目插入 assistant 消息之前，把终态文本作为持久化和后续模型输入的真值，同时继续把
流式增量作为 UI 事件。随后提交的是完整 `SessionItem` 序列，而不只是消息投影。这样
可以把及时渲染与字节稳定的上下文回放分离开来。

供应商托管工具与本地工具刻意使用不同事件类型。`backend_tool_started` 和
`backend_tool_completed` 表示已由供应商负责并执行的工作；应用层绝不会把它们送入
`PermissionManager`、`ToolRegistry` 或本地工具结果消息合成。本地从
`tool_requested` 到 `tool_completed`/`tool_failed` 的事件仍遵守现有权限和工作区
边界。xAI Responses 适配器会去重流式生命周期通知；如果中间事件缺失，则根据终态
后端输出补出一对开始/完成事件。

## 安全不变量

- deny 规则优先于 allow 规则和绕过模式。
- 无头执行把未解决的 `ask` 转换为拒绝。
- 写入前必须解析并校验目标；工作区工具不能通过 `..` 或符号链接逃逸。
- 平台无法实施显式沙箱要求时必须失败关闭。
- inspect 输出、日志、会话事件和异常都不得包含密钥。
- API 与代理凭据只能通过环境变量引用；解析后的代理 URL 保留在适配器内部，并从网络
  异常中移除。
- 只有在候选供应商产生第一个模型事件之前才能故障转移；越过该边界后，错误必须直接
  上抛，不得在其他供应商上重放当前步骤。
- 取消操作必须终止子进程并提交终止事件。
- Shell 命令在受所有权控制的进程组中运行。超时和取消先尝试优雅终止整个进程树，
  有界宽限期后再强制终止；输出按固定内存上限持续排空。
- 限制性 Bash 规则检查每一个可安全分解的命令段，包括常见包装器和嵌套
  `bash -c`。deny/ask 策略可能适用时，无法分类的脚本必须失败关闭。
- 旧上游状态只能只读导入，不能原地修改。

## 持久化

SQLite 是会话及其有序事件的规范事务存储。JSON 和 Markdown 用作交换/导出格式。
数据库暴露整数 schema 版本；每次变更必须包含前向迁移、fixture 覆盖和已记录的兼容
决策。Rust 会话由独立的只读适配器解析。该适配器校验格式版本 0 和 1，以明确上限
读取 JSONL 记录，把受支持的新旧记录转换为有序 `SessionSnapshot`，并报告损坏或
不支持的记录，而不是静默编造内容。SQLite 适配器在单个事务中插入快照，并保留其
ID、工作区、模型和时间戳；ID 已存在时不做任何修改并返回失败。源会话文件永远不会
以写入模式打开。

规范序列由普通 `Message` 和不透明但经过校验的 `PreservedContextItem` 联合组成。
消息内容项保留文本/图片顺序及图片 URL；推理和后端工具载荷保留供应商 JSON 与相对
顺序。运行时会把完整有序序列带入每个模型步骤，应用层视图仍使用普通消息投影。恢复
导入会话时，存储只允许在原前缀后追加，并拒绝改写已保存的上下文。JSON 导出格式
版本 2 同时包含两个投影。供应商适配器会校验图片引用，并且只在协议角色和 URI 形式
受支持时使用原生多模态内容块；其他图片无需执行适配器侧媒体 I/O，直接降级为可见
占位文本。保留上下文遵循失败关闭的亲和策略。只有来源标记可信的 Rust 导入会话才能
向 xAI 官方 HTTPS Chat Completions 端点发送可见推理与后端工具摘要；不透明加密
内容和所有非亲和目标都会被过滤。通用 Responses 适配器使用 `store: false`；可选 xAI
方言会请求加密推理并支持托管工具。不透明输出只有在保存的 profile 亲和指纹完全匹配
时才能回放；没有指纹的旧 Rust 导入继续采用更严格的 xAI 官方 HTTPS/来源标记回退规则。
回放前仍会剥离仅供输出使用的推理状态。详见
[ADR 0004](adr/0004-ordered-session-items.md) 和
[ADR 0005](adr/0005-provider-native-image-replay.md)。新生成的思考模式工具轮次改走带类型
消息路径；详见 [ADR 0006](adr/0006-thinking-tool-continuity.md)。导入上下文亲和规则见
[ADR 0007](adr/0007-provider-affine-context-replay.md)；Responses 原生回放规则见
[ADR 0008](adr/0008-xai-responses-native-replay.md)；xAI 托管工具的配置与生命周期归属见
[ADR 0009](adr/0009-xai-hosted-tools.md)；通用 profile 决策见
[ADR 0010](adr/0010-provider-profiles-and-cc-switch.md)；安全的输出前故障转移规则见
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md)；供应商 HTTP 传输选择规则见
[ADR 0012](adr/0012-provider-http-proxy-policy.md)。

Rust 边界还会对旧 assistant 记录执行有界的内存升级。`raw_output` 中携带上下文的
条目、单体 `reasoning` 和 v0 `reasoning_content` 会被提升到对应 assistant 之前。
读取流范围内会维护独立后端工具 ID 集合，仅抑制重复的内嵌副本；推理项保持原顺序，
绝不合并。损坏和未知的内嵌条目会分别计数，但不会导致原本有效的 assistant 整行
被拒绝。

## 平台策略

Linux、macOS 和 Windows 都是一等 CI 目标。平台专属代码隔离在适配器后。内核沙箱
和进程隔离可以使用小型原生辅助程序或系统设施，但业务与编排逻辑必须保留在 Python
中。不受支持的安全保证必须在启动时报告，绝不能静默降级。
