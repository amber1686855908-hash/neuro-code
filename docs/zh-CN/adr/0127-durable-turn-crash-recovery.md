# ADR 0127：持久化回合崩溃与 INDETERMINATE 恢复

[English](../../en/adr/0127-durable-turn-crash-recovery.md) · **简体中文**

## 状态

接受，适用于当前 pre-alpha Runtime。

## 背景

在本决策之前，会话虽然持久化了 `USER_MESSAGE`、模型步骤、请求快照、工具和终态事件，
但没有一个持久化身份表示完整的 `AgentRuntime.run()`。请求快照之后，Provider 请求可以
在没有 write-ahead 请求事实的情况下开始；工具也可以在没有恢复专属 started 事实的情况下
开始。普通失败记录还会通过分离的存储调用更新任务、失败事件和保留上下文。进程退出后，
单凭事件缺失无法区分回合尚未开始、已经产生模型输出，还是已经产生副作用。

已有的 execution segment checkpoint 文档记录进度指引和有界审计元数据。它们不是进程崩溃
恢复点，也不是工作区回滚点。本 ADR 增加独立的持久化层，不改变该契约。

## 决策

- 每个持久化回合获得唯一的不透明 `turn_id`。第一次 Provider 请求或工具 body 之前，
  回合 attempt 会先在 `session_turn_attempts` 中接受。后台唤醒回合也创建 attempt；由于
  子任务状态可能独立变化，其输入标记为不可重建。
  对计划执行而言，接受与任务归属属于同一个 SQLite 事务：新建的 `RUNNING` 任务与精确
  的 `attempt.task_id` 一起插入，或校验精确的 `QUEUED` 任务并用相同身份转为 `RUNNING`。
  恢复不会从最新任务、指纹或事件推断归属。
- `session_turn_attempts` 是小型规范恢复索引。有序 `events` 表继续作为追加式审计证据。
  恢复事实与对应事件在同一个 SQLite 事务中写入，因此分类依赖 sticky facts，而不是事件
  缺失或最后一条 UI 消息。
- write-ahead 边界是明确的：Provider stream 进入前持久化 `MODEL_REQUEST_STARTED`。
  第一个可观察的文本、推理、后端工具、工具调用或完成事件处理前，持久化
  `MODEL_OUTPUT_STARTED`。工具 body 执行前持久化 `TOOL_STARTED`，并记录工具是否可能产生
  副作用。
- 现有 `TURN_COMPLETED` 事务仍是唯一 commit point。它在一个 SQLite 事务中原子写入完成
  事件、最终会话项、标题/搜索投影、可选 execution record、任务终态和 attempt 的
  committed resolution。失败和取消使用对应的原子终态事务。
- 对已关联的 `RUNNING` 计划任务执行明确放弃时，也使用一个 SQLite 事务：校验 session、
  精确任务身份、任务 kind 和当前状态，然后按“任务事件先于回合事件”的确定顺序写入
  `RUNNING → CANCELLED`、`SESSION_TASK_CANCELLED`、`TURN_ABANDONED` 以及 attempt 的
  `ABANDONED` resolution。没有任务的普通用户回合保持原有放弃路径。
- 恢复扫描器忽略普通的 `FAILED` 和 `CANCELLED` 终态 attempt。重启不会写入 `ABANDONED`；
  只有明确的恢复操作可以写入它。`INDETERMINATE` 永远不会自动 replay。
- 精确重试输入是有界、归属当前回合的 `TurnInput` 投影：prompt、有序 content parts、
  source 和 plan identity flags。只有输入可重建且不超过 256 KiB 时才保存，并验证指纹。
  Provider 请求体、请求头、凭据、system context、工具参数和无界输出都不进入恢复存储。
- CLI、TUI 和 ACP 私有 `neuro-code/session/recovery` 扩展共享应用层恢复服务。默认 inspect
  只展示未决 attempt；已提交/已放弃历史通过明确的审计视图获取。它们提供有界证据和明确
  `abandon`；`retry` 只对无输出、输入精确可重建且非计划执行的用户回合开放。即使计划回合
  的安全分类是 `SAFELY_RETRYABLE`，计划执行 retry 仍不支持。重试会放弃旧 attempt 并创建
  新的回合身份，不会原地续接旧 attempt。

## 恢复分类

| 持久化事实 | 分类 | 自动 replay |
|---|---|---:|
| 原子完成且 attempt resolution 为 `committed` | `COMMITTED` | 否 |
| 非计划用户 attempt 未终态、输入精确可重建、没有输出、没有工具开始且无事实冲突；请求可以已经标记为 started | `SAFELY_RETRYABLE` | 否；只能明确 retry |
| 计划 attempt 未终态、任务归属明确、输入精确可重建、没有输出、没有工具开始且无事实冲突 | `SAFELY_RETRYABLE` | 否；不提供 retry，只能明确 abandon |
| 已观察模型输出、任意工具开始、可能有副作用的工具开始、输入缺失、后台唤醒或事实冲突 | `INDETERMINATE` | 永不 |
| 明确持久化 `TURN_ABANDONED` 终态 | `ABANDONED` | 否 |

当前重试策略刻意保守。若某个 Provider 特性可能在第一个可观察事件前产生外部效果，
它必须保持 `INDETERMINATE`；不能仅因为存在 `MODEL_REQUEST_STARTED` 就推断安全。

## 事务与迁移模型

schema version 14 新增带外键的 `session_turn_attempts` 表以及按 session/resolution 的索引。
13 → 14 迁移只创建新表，不改写既有会话；因此旧会话没有中断 attempt，恢复行为不变。
该行保存有界身份、输入指纹/可重建性、sticky request/output/tool facts、最新阶段和终态
resolution，不复制完整模型请求。

存储端口拥有普通 `start_turn_attempt`、带任务归属的原子计划接受、原子恢复事实追加、
完成、失败/取消以及明确放弃操作。SQLite 写锁和事务边界构成持久化边界。如果搜索索引
写入或恢复转换失败，整个所属事务回滚，包括 attempt resolution、事件、任务状态和会话项。

## 后果

存在未决 attempt 时，普通 resume 会被阻止。用户必须先查看它，然后明确重试安全的非计划用户回合，
或明确放弃它。`INDETERMINATE` attempt 可以审计和放弃，但本阶段不实现回合中途续接、工具
补偿、工作区回滚、后台子进程协调、计划执行重试或自动 replay。

持久化输入投影可能包含用户提供的文本或稳定媒体引用，因此遵循现有会话 retention 边界。
接口投影只暴露有界元数据，不渲染该输入或 Provider secret 字段。

## 证据

聚焦测试覆盖输出前安全分类、request/output/tool write-ahead 事实、计划接受与放弃回滚的
原子性、明确计划任务归属、普通失败过滤、迁移，以及真实子进程退出/重开。持久化输出标记后
退出的进程重开为 `INDETERMINATE`；原子计划接受后退出的进程会以精确的 `RUNNING` 任务归属
重开；原子 commit 后退出的进程重开为 `COMMITTED`。
