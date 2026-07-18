# ADR 0024：通过完成事件等待多个后台任务

**简体中文** · [English](../../en/adr/0024-event-driven-multi-background-task-wait.md)

- 状态：已接受
- 日期：2026-07-18
- 源基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

第一版受管后台命令切片提供了 `task_output`，可以短暂等待单个任务。模型同时协调多个测试
或构建时，否则只能反复串行调用或用 sleep 轮询。固定 Rust 基线最多接受 20 个 ID，支持
`wait_any` 和 `wait_all`，并注册完成等待器，而不是轮询任务状态。

多任务等待必须保持现有会话边界。工具返回、超时或被取消时，还必须清理所有辅助等待器；
遗留的分离等待器可能消费后续完成事件，或让本应属于当前轮次的任务在不可见状态下继续
存活。

## 决策

`BackgroundTaskManager.wait` 接受唯一且已规范化的 ID、`BackgroundTaskWaitMode` 和有限
超时，返回冻结的 `BackgroundTaskWaitResult`，其中包含已知快照、当前作用域不存在的 ID，
以及请求的完成条件是否超时。

`LocalBackgroundTaskManager` 等待每条任务记录已有的 `asyncio.Event`。`wait_any` 在至少
一个已知任务进入终态后返回；`wait_all` 在全部已知任务进入终态后返回。条件已经满足时
立即返回。未知和跨作用域 ID 都报告为 `not_found`，不会暴露其他作用域的任务元数据。
无论通过哪条路径退出，未完成的辅助等待器都会被取消并收拢。

面向模型的 `wait_tasks` 工具：

- 接受 1 至 20 个 ID，去除首尾空白，并按首次出现顺序去重；
- 要求使用 `wait_any` 或 `wait_all`；
- `timeout_seconds` 默认且最大为 30 秒，零值保留历史接口“使用默认等待”的含义；
- 返回已知任务的快照和有界输出，以及逐 ID 的 `not_found` 结果；
- 用 `ToolContext.output_byte_limit` 限制组合文本，元数据不包含捕获输出；
- 和终态 `task_output` 一样确认所有已返回的终态快照，防止后续完成提醒重复投递。

等待与确认属于观察性生命周期记账，因此在权限系统中该工具为只读。启动和终止进程仍是
有副作用操作。

## 影响

- 模型可以协调并行工作，而无需承担串行轮询延迟。
- 超时只返回部分状态，不会取消底层任务。
- 取消调用不会遗留僵尸等待器，也不会吞掉模型尚未看到的完成状态。
- 会话隔离和输出限制覆盖完整的多任务表面。
- Python API 与 `task_output` 一致使用秒；历史 Rust 表面使用毫秒。
- 子代理尚未进入该任务命名空间。自动唤醒、持久完整输出文件和跨进程任务恢复仍是独立
  切片。

源证据来自固定提交中的历史 `WaitTasksTool`、`WaitMode`、`MAX_MULTI_WAIT_IDS`、
`wait_any_event_driven` 和 `wait_all_event_driven` 行为。
