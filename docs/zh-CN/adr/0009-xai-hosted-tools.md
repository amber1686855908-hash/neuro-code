# ADR 0009 — xAI 托管工具与生命周期归属

**简体中文** · [English](../../en/adr/0009-xai-hosted-tools.md)

## 状态

已接受。

[ADR 0010](0010-provider-profiles-and-cc-switch.md) 对本决策作了扩展：配置现在使用带
`dialect = "xai"` 的 `openai-responses` profile；托管工具归属和生命周期规则不变。

## 背景

xAI Responses 可以在模型推理期间执行网页搜索、X 搜索和代码解释。这些托管工具不同于
Neuro Code 函数工具：xAI 负责执行并返回后端输出项，本地工具则必须经过权限管理器和
工作区范围执行器。若二者共用同一生命周期，就会错误暗示本地策略可以批准、拒绝或
复现供应商侧副作用。

固定 Rust 基线单独建模托管工具，让托管工具在同名冲突时覆盖函数工具，并把网页/X 的
流式进度统一为后端工具事件。xAI 当前文档还公开了 `code_interpreter`，以及网页来源和
代码输出的详细 include 选择器。

## 决策

新增可选的供应商专属配置字段：

```toml
[provider.default]
kind = "xai-responses"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

数组保持顺序、不得重复，并且只允许以上三个精确名称。其他供应商类型使用非空配置时，
配置加载直接失败。xAI 适配器先把每个配置项发送为 Responses 原生工具，再追加名称不
冲突的本地函数 schema；冲突时托管工具胜出。网页搜索请求包含
`web_search_call.action.sources`，代码解释器请求包含
`code_interpreter_call.outputs`，同时继续包含加密推理。

新增供应商领域事件 `ModelBackendToolStarted`、`ModelBackendToolCompleted`，以及运行时
事件 `backend_tool_started`、`backend_tool_completed`。公开载荷只包含供应商调用 ID
和规范工具名称；它们绝不会进入本地权限、注册表、执行或工具结果消息路径。供应商原生
终态输出仍按 ADR 0008 作为可持久化结果。

适配器识别网页/X/代码原生进度事件、xAI custom-tool 输入事件和通用 output-item
added/done 事件，并按调用 ID 与工具名称去重。若终态响应包含后端输出，但流中没有
生命周期事件，适配器会补出一对有序开始/完成事件，使 JSON、JSONL、会话事件及未来
UI 都获得一致审计轨迹。

## 后果

用户无需添加临时客户端工具，就能选择启用 xAI 托管研究和代码执行，同时不会混淆
供应商副作用与本地工作区副作用。同一套统一运行时事件既覆盖实时进度，也覆盖只有终态
的夹具；完整原生输出继续支持 SQLite 持久化和供应商亲和回放。

托管工具可能在模型 Token 之外产生供应商费用，因此默认关闭，inspect 也只公开工具
名称。域名、X 账号、日期、图片理解等高级筛选条件留待带类型配置设计。需要凭据才能
启用的 xAI 在线夹具仍待完成；协议行为由模拟 SSE 和固定 Rust 证据覆盖。

## 参考

- [xAI 工具概览](https://docs.x.ai/developers/tools/overview)
- [xAI 网页搜索](https://docs.x.ai/developers/tools/web-search)
- [xAI X 搜索](https://docs.x.ai/developers/tools/x-search)
- [xAI 流式与同步工具](https://docs.x.ai/developers/tools/streaming-and-sync)
- [xAI 定价](https://docs.x.ai/developers/pricing)
