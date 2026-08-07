# ADR 0065：有界工具输出读取接缝

[English](../../en/adr/0065-bounded-tool-output-read-seam.md) · **简体中文**

- 状态：已接受
- 日期：2026-08-06

## 背景

Stage5BK 将本地 Bash artifact 写入会话和 SQLite 之外。未来界面可能需要查看某个
artifact，但让 TUI、CLI 或 ACP 自己拼接状态目录路径会绕过应用边界，也会使授权语义不清晰。

## 决策

新增类型化的 `ToolOutputArtifactReader` 端口和精简的
`ToolOutputArtifactApplicationService`。请求携带运行中工具产生的不透明
`ToolOutputArtifact` 句柄和显式字节上限。适配器根据经过校验的 artifact ID 推导文件名，
调用方不能提供任意路径。

读取默认限制为 256 KiB，且不会超过已有的 8 MiB artifact 上限。文件适配器将解析后的路径
限制在配置的 artifact 根目录下，在解码和截断前再次脱敏，并返回 frozen 文本投影。缺失或伪造
句柄会安全失败。

该服务暂不通过 CLI、TUI、ACP 或新工具暴露。Stage5BK 没有持久化 session 与 artifact 的关联，
因此本切片有意不宣称具备会话级授权或跨进程恢复。面向用户的读取路径必须先定义这种关联和可见性策略。

## 非目标

本决策不新增 SQLite 行、会话项、事件类型、文件系统路径参数、artifact 列表、分页或原始输出回放。
不改变 Bash 执行、权限、Sandbox、后台任务或模型上下文。

## 验证

应用层和文件适配器测试覆盖有界脱敏读取、伪造路径拒绝、缺失 artifact、显式字节上限以及写入行为不变。
