# ADR 0083：ACP 子代理别名重连兼容性

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DC

## 背景

私有 `_neuro-code/session/subagents` 扩展会为 `resume` 和 `fork` 返回外部
ACP alias。ACP 客户端可能在生命周期请求之间重连，存储也可能因为另一个会话
已经占用候选 alias 而拒绝提议。协议适配器必须在重连后保留持久 alias，并且绝不
投影一个解析到其他子会话的 alias。

## 决策

生命周期 alias 分配仍限制为最多四次尝试。每次分配成功后，适配器都会在 ACP
alias 命名空间中再次解析它，再进行序列化；如果 alias 不可用、无法解析，或解析
到其他内部会话，则使用新的有界候选重试。尝试耗尽后以
`session_alias_allocation_failed` 失败关闭。

持久化存储的 `get_or_create` 契约继续提供幂等性：重复 `resume` 请求以及客户端
重连后创建新的 ACP agent 实例，都会为同一个子会话返回已有 alias，不会仅因为
连接发生变化而创建新 alias。

## 边界

本兼容性切片不改变 ACP 标准 capability、子代理执行、生命周期所有权、SQLite
schema、父 transcript、Provider 行为、调度、模型工作的重试、递归、并行或写入
能力。Alias 分配仍是独立且有界的存储操作，不宣称与生命周期动作处于同一个事务。

## 验证

测试覆盖通过 SDK 私有路由的重连、alias 冲突重试、稳定 alias 复用、有界失败和
所有权不匹配时的失败关闭。Wire 投影仍只包含外部 alias 和动作；内部会话 ID 与
子会话内容继续被排除。
