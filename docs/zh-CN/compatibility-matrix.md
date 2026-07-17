# 兼容性矩阵

**简体中文** · [English](../en/compatibility-matrix.md)

状态取值：`unassessed`（未评估）、`planned`（已规划）、`partial`（部分实现）、`compatible`（兼容）或 `intentionally-different`（有意不同）。“证据”指向固定版本的 Rust 源项目。

| 能力 | 目标 | 状态 | 证据 / 备注 |
|---|---|---|---|
| 软件包与 CLI 组合根 | M1 | partial | 已在独立 `neuro-code` 命令下实现 Python CLI 骨架 |
| `version` 与 JSON 版本输出 | M1 | partial | 已实现独立软件包元数据 |
| 生效配置检查 | M1 | partial | `neuro-code inspect`；已实现隐藏密钥的 Python 视图 |
| 命名供应商 profile 与选择 | M2/M3 | partial | 已实现四种显式线路协议、默认/单次选择、旧 TOML 兼容、脱敏的 `providers list/inspect`，以及带 `/provider`、`/model` 和 Ctrl+P 的 TUI 已配置 profile 选择器；远程模型目录、推理强度选择和应用内持久配置编辑仍待实现 |
| CC Switch 兼容 | M2 | partial | 已实现只读映射 `NEURO_CODE_CC_SWITCH_CONFIG`、三种 backend 格式、回环 `PROXY_MANAGED`、环境变量引用、内联密钥拒绝和项目配置覆盖；仍不包含 CC Switch 数据库/进程控制及其内部故障转移 |
| 安全供应商故障转移 | M2 | partial | 已实现有序按需备用 profile、第一个事件提交边界、单次运行单向选择、可审计失败/选择事件、`--no-failover`、汇总错误和不透明会话来源保护；重试、熔断和持久化健康状态仍待实现 |
| 供应商 HTTP 代理策略 | M2 | partial | 已实现按 profile 配置环境/直连/显式环境变量模式、按需 URL 校验、四适配器统一 HTTPX 选项、检查/错误脱敏和含义不明 SOCKS scheme 的严格诊断；保留无效继承 `ALL_PROXY` 时，显式路由已通过 DeepSeek 手动验证；PAC、多代理挂载和内置 SOCKS 支持仍待实现 |
| 无头单次提示 | M2 | partial | 已实现流式文本/工具循环，以及纯文本、JSON、JSONL 输出 |
| OpenAI 兼容/xAI Chat 端点 | M2 | partial | 已实现 Chat Completions 流式响应、有界 `max_tokens`、工具、经过校验的原生用户图片块、思考模式工具往返连续性，以及失败关闭的上游导入可见上下文回放；双门禁 DeepSeek 在线流式/故障转移/读取工具测试已实现并手动通过，仍需可提交的模型专用录制夹具 |
| OpenAI Responses/xAI 方言 | M2 | partial | 已实现通用可移植 Responses 子集，以及可选 xAI 加密推理、网页/X/代码托管工具、profile 亲和原生回放、严格旧官方主机回退、终态权威输出、SSE 生命周期统一、状态剥离、回退修复和错误脱敏；仍需托管工具高级筛选、有状态 response ID、压缩项和选择性在线夹具 |
| Anthropic 提供商 | M2 | partial | 已实现原生 Messages SSE、文本/推理、工具、用量、错误、密钥脱敏，以及经过校验的用户/工具结果图片块；仍需在线/模型专用夹具 |
| Gemini 提供商 | M2 | partial | 已实现原生 `streamGenerateContent` SSE、文本/思维、工具、用量、错误、thought-signature 往返，以及经过校验的用户内联/File API 图片项；仍需在线/模型专用夹具 |
| 核心工具注册表与模式 | M2 | partial | 已实现独立 Python 契约 |
| 读取/列举/grep 工具 | M2 | partial | 已实现限制在工作区内的 UTF-8 基线 |
| 搜索/替换编辑 | M2 | partial | 已实现原子精确替换与路径边界检查 |
| Bash 执行与取消 | M2 | partial | 已实现有界流式输出、空标准输入、超时/取消清理、POSIX 进程组 TERM/KILL，以及 Windows 进程组/`taskkill` 回退；仍需 Windows Job Object 和后台任务对齐 |
| 权限规则优先级 | M2/M3 | partial | 已实现 deny/ask/allow 优先级、逐片段检查 `&&`/`||`/`;`/管道、包装器剥离、嵌套 `bash -c`、复杂脚本失败关闭、异步单次允许/拒绝，以及仅保存在内存中的精确操作会话批准；每次审批前都会重新判定 deny，仍需持久化审查规则和完整规则/文件访问语法 |
| SQLite 会话事件存储 | M2 | partial | 已实现 schema v2、事务化 v1 迁移、profile 亲和元数据、规范有序会话项持久化、消息投影、只追加前缀检查、列表、恢复、导出，以及跨平台路径别名的文件系统身份工作区匹配 |
| Rust 会话导入 | M2 | partial | 已实现只读解析 v0/v1、新旧消息混排、结构化图片、有序推理/后端工具载荷、内嵌 `raw_output`/单体推理恢复与后端 ID 去重、有界损坏行恢复、SQLite 原子导入、前缀安全恢复、导出 v2、供应商原生图片回放、可信来源的 xAI Chat 回放，以及严格亲和的 Responses 原生加密/后端工具回放；压缩与有状态 ID 仍待实现 |
| 全屏/精简 TUI | M3 | partial | 已实现 Textual 提示输入、滚动记录、流式文本、有界供应商/工具状态、持久多轮上下文、支持单次允许/精确会话允许/拒绝的失败关闭审批框、安全的已配置 profile 选择器、本地 `/help`/`status`/`provider`/`model`/`cancel`/`clear`/`quit`、Ctrl+C 运行中取消、同会话重试、当前/未启动本地调用的取消结果配对及无头 UI 测试；远程模型目录/推理强度选择、首 token 前无痕回退/插话队列、富工具卡片/渲染和跨平台终端冒烟测试仍待实现 |
| Markdown/Mermaid/媒体渲染 | M3/M5 | planned | 适用渲染 crate 及其随附声明 |
| PTY 与进程树对齐 | M3 | planned | `ptyctl`、Shell 终端模块 |
| 操作系统沙箱配置 | M3 | planned | Landlock/bwrap、Seatbelt、Windows 适配器 |
| ACP stdio/WebSocket | M4 | planned | `xai-acp-lib`、pager/shell ACP 模块 |
| MCP 服务器 | M4 | planned | MCP 生命周期与传输实现 |
| 技能与 AGENTS.md | M4 | planned | 代理提示词/发现机制及用户指南 |
| 钩子与插件 | M4 | planned | 钩子和插件市场 crate |
| 子代理与计划模式 | M4 | planned | 生命周期/会话/目标模块 |
| 记忆与上下文压缩 | M5 | planned | 记忆和压缩 crate |
| LSP、工作树与检查点 | M5 | planned | 工具/工作区/worktree crate |
| 语音、图像、视频和网页工具 | M5 | planned | 需要由提供商支持的适配器 |
| Leader、relay 与 Computer Hub | M5 | planned | 本地组件与外部服务边界 |
