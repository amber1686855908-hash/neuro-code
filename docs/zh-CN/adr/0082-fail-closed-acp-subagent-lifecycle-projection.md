# ADR 0082：ACP 子代理生命周期投影失败关闭

- 状态：已接受
- 日期：2026-08-08
- 范围：Stage5DB

## 背景

私有 ACP 子代理生命周期扩展会委托注入的 application 生命周期 owner。协议边界不能信任错误的 owner 或测试桩返回
属于其他父会话/任务的动作，也不能把不安全的外部 alias 写入 wire。

## 决策

在投影生命周期结果前，ACP 适配器要求返回的父 session ID、父 task ID 和 action 必须与已校验并已分派的请求一致。
任何不一致都会以 `subagent_lifecycle_invalid_result` 失败关闭，绝不会转换为成功的 resume、fork 或 delete 响应。

ACP 接口 serializer 只有在非 delete 的 session alias 非空、不含控制字符和 NUL，并且 UTF-8 字节长度处于有界范围内时，
才接受该 alias。无效输出会在序列化前失败关闭。Delete 仍保持不含标识符的 `{action, deleted}` 投影。

## 边界

这是一个响应边界加固切片，不改变生命周期 owner、SQLite 事务、alias 分配重试、子会话执行、模型调用、工具重放、调度、
递归、并行或可写能力。

## 结果

注入的 application seam 和未来实现不能意外跨越父会话关系或输出无界/不安全的 ACP 标识符。有效结果的 wire 契约保持不变，
而错误结果会以稳定的内部错误失败关闭。
