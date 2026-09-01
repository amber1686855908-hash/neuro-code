# ADR 0133：有界父上下文中继

- 状态：已实现为显式内部纵向切片；最终评级等待 merge-ref CI
- 日期：2026-08-24
- 范围：一个串行可写 worker 与一个不可变 parent→child 上下文快照

## 背景

ADR 0129-0132 已建立托管 worktree、READY 基线 checkpoint、可写 child lease、
全新 child session 与 child 作用域只读 LSP runtime。child 此前刻意不复用 parent
上下文。下一项窄能力是在不克隆 transcript、不共享实时状态、也不转移任何权限的
前提下，把有用的 parent 对话证据交给这个既有 worker。

## 决策

新增 Provider 无关的 `ParentContextRelay` 值与 insert-only 持久化边界。生产投影
只能来自实际 parent `ConversationBinding` 所绑定的 durable session；调用者不能
指定来源 session，也不能提供原始 Relay 文本。

Relay 绑定精确 parent session/task、child session、可写 lease、`WorktreeId`、基线
`CheckpointId`、base commit、capability/grant 指纹，以及显式 child task 的摘要。
来源投影和模型渲染分别具有确定性指纹，完整记录还具有每次加载都要校验的完整性指纹。

### 安全确定性投影

首切片从新到旧扫描 durable parent `SessionItem`，最多选择十项，再恢复时间顺序。
只有真实纯文本 USER 消息和 ASSISTANT 可见纯文本消息可进入。若
`reasoning_content` 是明确独立字段，可投影 assistant 可见正文，但绝不投影推理。
含 tool call 或任意 media part 的消息整条省略。

SYSTEM/TOOL 角色消息、应用 synthetic context、tool arguments/metadata、tool result、
保留推理/backend call、媒体数据/URL，以及项目或 runtime 通知都按结构排除。符合条件
的文本在持久化前统一经过 composition-owned 的既有配置脱敏边界。这是有界、按配置
脱敏的契约，并不声称能识别所有可能的 secret 形态。

字节预算为：

- 最多 10 个选中项；
- 每项最多 4 KiB UTF-8 文本；
- 投影文本合计最多 24 KiB；
- 完整渲染 Relay 最多 32 KiB。

截断保持合法 UTF-8。选择、渲染和指纹均为确定性过程，不调用模型，也不创建第二套
摘要系统。首切片不复用 durable compaction summary，因为其当前有效性证明属于独立
应用工作。

### Durable 顺序与失败语义

schema 17 新增一对一 `parent_context_relays` 记录，以 RESTRICT 外键关联可写 lease、
parent task 及 parent/child session。READY 行不可变。只有在相等性和完整性校验通过后
才可接受完全相同的重复插入；不一致必须拒绝，不存在盲目 UPSERT 或 payload 更新。

可写生命周期为：

```text
托管 worktree READY
  -> 基线 checkpoint READY
  -> child session
  -> SubagentLink
  -> 安全 parent 投影
  -> Relay 插入 READY 并重新加载完成完整性校验
  -> 创建 child runtime
  -> lease ACTIVE
  -> 第一次模型请求
```

Relay 发布失败会阻止 child runtime/模型执行，并保留既有 worktree、checkpoint、lease、
child session 与 link 身份。发布后的 Provider/tool 失败、取消、超时和进程死亡都会
保留不可变 Relay 作为审计证据。reconciliation 不会重跑 child，也不会重建 Relay。

### 模型上下文，而非权限

`ContextBuilder` 在每次 child 模型请求中精确注入一条纯文本 synthetic USER 消息，
标记为 `SyntheticReason.PARENT_RELAY`。稳定顺序是 system、project instructions、
available skills、parent relay、真实 child task/history。该 synthetic Relay 不会作为
真实 child `SessionItem` 持久化，并在 child 的模型/tool/LSP 多步骤间保持字节稳定。

Relay 文本不能改变 tool 名称、workspace root、sandbox profile、network、LSP、
worktree 或 checkpoint 权限。parent 提到的路径和命令只是不被解析的文本。既有权限
求交与 child-root 指令/技能发现保持不变。

## 未实现

原始 transcript 复用、隐藏推理传递、tool output 传递、compaction summary 复用、
parent 上下文实时流、长期记忆、共享会话状态、无界或共享并行 worker、超出有界 worker 切片的 Task DAG、
Leader/Swarm/Ultracode 编排、自动委派、Bash/process worker、更丰富的 child 结果
Relay、合并集成与自动清理仍不在本切片范围内。

## 验证边界

验收要求覆盖不安全结构排除与配置 secret 脱敏、字节边界与多字节截断、确定性快照/
指纹、insert-only 与篡改拒绝、populated schema-16 迁移、READY-before-model 顺序、
不写入 transcript、稳定多步骤模型请求、不变的 worker LSP 权限、失败/取消/超时保留，
以及 Relay 发布后、首次模型请求前的真实进程死亡场景。最终证明还要求完整本地门禁和
堆叠 PR merge-ref 的 Linux/macOS/Windows/package 矩阵。
