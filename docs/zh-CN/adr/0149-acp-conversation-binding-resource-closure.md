# ADR 0149：ACP ConversationBinding 资源关闭权

- Status：Accepted
- Date：2026-08-30
- Scope：V1 ACP interface-boundary slices 之后的有界 correctness closure
- Depends on：ADR 0145、ADR 0146、ADR 0147 和 ADR 0148

## Context

精确冻结的 PR #76 HEAD 是
`791ceb16e74c7e9e0fbab5882c11882417166648`。其中 ACP agent 继续拥有 session
发布、持久化 session 激活、fork 激活、活动 session 清理和 connection shutdown。

修改前审计发现，ACP 有四条清理路径直接调用
`binding.background_tasks.shutdown()`：新 session 发布失败、load/resume 激活失败、
fork 失败，以及活动 session 清理。这绕过了 application composition 已经创建的
`ConversationBindingResourceScope`。在 production composition 中，该 scope 同时拥有
binding 的 LSP manager，因此直接调用可能使 LSP manager 一直保留在 composition 注册表中，
直到 composition shutdown。

## Decision

`ConversationBinding.close()` 是 ACP 关闭 binding 的唯一 authority。ACP 决定 binding
何时失去 ownership，并在 `asyncio.shield` 下调用规范 close method。ACP 不读取
`resource_scope`，不重建其 callback，也不直接关闭 `background_tasks`。

`ConversationBinding` 继续是 application-owned resource authority。既有幂等且
cancellation-resistant 的 `ConversationBindingResourceScope` 继续负责恰好一次关闭 LSP
manager 与 binding task scope；没有 resource scope 的 binding 继续使用原有
background-task fallback，均不改变。

ownership transfer 保持不变：

```text
publication 之前：ACP locals 拥有 binding、MCP tools 和 client terminal
publication 之后：_AcpSession 拥有 binding、MCP tools 和 client terminal
```

成功 publication 后清空 locals 的 ownership。新 session、resume 和 fork 激活失败时，
仍由 locals 负责关闭所有尚未发布的资源。活动 close、delete 和 connection shutdown
继续使用既有 aggregate cleanup lock，顺序为：prompt task、MCP tools、client terminal、
最后 binding。Fork durable-copy rollback 保持不变。

## Dependency direction

```text
neuro_code.acp
        -> application ConversationBinding.close()
        -> ConversationBindingResourceScope
        -> application-owned LSP 与 background-task resources
```

ACP adapter 只依赖 binding close contract，不依赖具体 LSP manager、background-task
implementation 或 composition resource callback。

## Non-goals

本 closure 不移动 `_AcpSession`，不增加 session runtime/controller，不改变 capability
negotiation，不改变 MCP 或 terminal protocol，不改变 prompt 或 permission behavior，也不
重设计 cleanup error aggregation。CLI 独立的 parent-binding cleanup 与 composition root
自身的 cleanup 不属于本 ACP-owned lifecycle slice。

## Validation

Focused tests 覆盖 publication、resume、fork、active cleanup、cancellation、binding/MCP/
terminal exactly-once cleanup，以及真实的
`ApplicationComposition -> ACP service -> ACP agent` 路径。production composition assertion
验证 ACP session 关闭时真实 LSP manager 被关闭并从 composition registry 移除。一个小型
architecture guard 防止 ACP 绕过 `ConversationBinding.close()` 或读取 binding resource scope。

V1 session runtime/controller 仍未由本 ADR 实现。
