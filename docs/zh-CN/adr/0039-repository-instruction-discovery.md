# ADR 0039：仓库指令发现

**简体中文** · [English](../../en/adr/0039-repository-instruction-discovery.md)

- 状态：已接受
- 日期：2026-07-22

## 背景

Neuro Code 需要读取项目约定，但不能让仓库文本获得应用系统提示的信任级别。固定的
Rust 基线把项目指令表示为带来源标记的合成用户项，但其静态发现和运行时发现并不是
一条完整、统一的生产流程。

## 决策

增加 `InstructionDiscovery` 端口和有界的
`FilesystemInstructionDiscovery` 适配器。对于当前绑定目标，适配器从工作区根到目标
目录读取 `AGENTS.md`，并按浅到深返回。本切片只识别精确文件名 `AGENTS.md`。

发现最多允许 20 层目录、10 个已加载文件、单文件 64 KiB、总计 256 KiB。读取必须是
身份稳定的常规文件、有效 UTF-8，且不含禁止的 C0/C1/DEL 控制字符。所有符号链接和
Windows reparse point 都会拒绝并分类以供审计；拒绝路径中的控制字符会被转义。

每个模型步骤前，有界发现在线程中执行，不阻塞事件循环。内容作为标记为
`PROJECT_INSTRUCTIONS` 的临时 `User` 消息，放在系统消息之后、真实用户输入之前。
该消息及其标记从不持久化。

`InstructionTracker` 维护移动目标，并单独记录最近一次真正注入模型的结果。
`search_replace` 会按路径和内容对比目标当前指令与该快照；新增或变更的规则会让写入
以错误中止。任意 Bash 路径无法可靠推导，因此 Bash 写入没有这项预检保证。

## 影响

- CLI、TUI 和 ACP 共用相同的发现端口与默认适配器。
- 同会话变更在下一模型步骤生效；恢复会话也会重新发现。
- `inspect` 显示路径、深度、字节数、拒绝原因和稳定内容指纹。
- Claude/rules 兼容文件名、gitignore 过滤和 Bash 路径解析不属于本切片。
