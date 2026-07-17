# 兼容性矩阵

**简体中文** · [English](../en/compatibility-matrix.md)

状态取值：`unassessed`（未评估）、`planned`（已规划）、`partial`（部分实现）、`compatible`（兼容）或 `intentionally-different`（有意不同）。“证据”指向固定版本的 Rust 源项目。

| 能力 | 目标 | 状态 | 证据 / 备注 |
|---|---|---|---|
| 软件包与 CLI 组合根 | M1 | partial | `xai-grok-pager-bin/src/main.rs`；Python CLI 骨架已实现 |
| `version` 与 JSON 版本输出 | M1 | partial | `xai-grok-pager/src/app/cli.rs`；品牌信息有意保持独立 |
| 生效配置检查 | M1 | partial | `grok inspect`；已实现隐藏密钥的 Python 视图 |
| 无头单次提示 | M2 | partial | 已实现流式文本/工具循环，以及纯文本、JSON、JSONL 输出 |
| OpenAI 兼容/xAI 模型端点 | M2 | partial | 支持自定义模型配置；首先实现 Chat Completions 适配器 |
| Anthropic 提供商 | M2 | partial | 已实现原生 Messages SSE、文本/推理、工具、用量、错误和密钥脱敏；仍需在线测试和模型专用夹具 |
| Gemini 提供商 | M2 | partial | 已实现原生 `streamGenerateContent` SSE、文本/思维、工具、用量、错误和 thought-signature 往返传递；仍需在线测试和模型专用夹具 |
| 核心工具注册表与模式 | M2 | partial | `xai-grok-tools`；Python 契约已实现 |
| 读取/列举/grep 工具 | M2 | partial | 已实现限制在工作区内的 UTF-8 基线 |
| 搜索/替换编辑 | M2 | partial | 已实现原子精确替换与路径边界检查 |
| Bash 执行与取消 | M2 | partial | 已实现有界流式输出、空标准输入、超时/取消清理、POSIX 进程组 TERM/KILL，以及 Windows 进程组/`taskkill` 回退；仍需 Windows Job Object 和后台任务对齐 |
| 权限规则优先级 | M2 | partial | 已实现 deny/ask/allow 优先级、逐片段检查 `&&`/`||`/`;`/管道、包装器剥离、嵌套 `bash -c`，复杂脚本按失败关闭处理；仍需完整规则与文件访问语法 |
| SQLite 会话事件存储 | M2 | partial | 已实现版本化事件/消息存储、列表、恢复和导出 |
| Rust 会话导入 | M2 | partial | `xai-grok-shell/src/session/storage/jsonl/mod.rs`、`session/persistence.rs`；已实现只读解析 v0/v1 JSONL、兼容新旧消息混排、有界恢复损坏行和原子导入 SQLite；仍需无损表示推理、后端工具和图片记录 |
| 全屏/精简 TUI | M3 | planned | `xai-grok-pager`、`xai-grok-pager-minimal` |
| Markdown/Mermaid/媒体渲染 | M3/M5 | planned | 适用渲染 crate 及其随附声明 |
| PTY 与进程树对齐 | M3 | planned | `ptyctl`、Shell 终端模块 |
| 操作系统沙箱配置 | M3 | planned | Landlock/bwrap、Seatbelt、Windows 适配器 |
| ACP stdio/WebSocket | M4 | planned | `xai-acp-lib`、pager/shell ACP 模块 |
| MCP 服务器 | M4 | planned | `xai-grok-mcp` |
| 技能与 AGENTS.md | M4 | planned | 代理提示词/发现机制及用户指南 |
| 钩子与插件 | M4 | planned | 钩子和插件市场 crate |
| 子代理与计划模式 | M4 | planned | 生命周期/会话/目标模块 |
| 记忆与上下文压缩 | M5 | planned | 记忆和压缩 crate |
| LSP、工作树与检查点 | M5 | planned | 工具/工作区/worktree crate |
| 语音、图像、视频和网页工具 | M5 | planned | 需要由提供商支持的适配器 |
| Leader、relay 与 Computer Hub | M5 | planned | 本地组件与外部服务边界 |
