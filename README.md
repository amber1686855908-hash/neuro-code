# Neuro Code

[简体中文](docs/zh-CN/README.md) · [English](docs/en/README.md)

Neuro Code 是一个独立开发的 Python 终端编码智能体。它以清晰的工作区、权限、沙箱和会话边界为基础，提供可扩展的 CLI、TUI、MCP 与 ACP 能力。

Neuro Code is an independently developed Python terminal coding agent. It provides extensible CLI, TUI, MCP, and ACP capabilities with explicit workspace, permission, sandbox, and session boundaries.

## 快速开始 / Quick start

正式发布后，全局安装一次即可在任意目录使用，无需为每次启动激活虚拟环境：

Install the released package once to use it from any directory; no per-use virtual-environment activation is required:

```bash
uv tool install neuro-code
# or / 或：pipx install neuro-code
```

以下命令都会以当前目录作为工作区启动 TUI：

Each command starts the TUI with the current directory as its workspace:

```bash
neuro
neuro code
neuro-code
```

开发源码时使用：

For source development:

```bash
uv sync --extra dev
uv run neuro
```

## 你可以做什么 / What it provides

- **模型与设置 / Models and settings** — 在 TUI 中保存、编辑、测试和切换多个模型供应商 profile；API 密钥不会回显，网络与代理错误会脱敏展示。
- **安全的工作区操作 / Safe workspace work** — 文件与终端操作经过权限、工作区和沙箱边界；显式拒绝始终优先。
- **可恢复的会话 / Durable sessions** — SQLite 会话支持恢复、搜索、重命名、分叉、删除，以及受限的计划与任务快照查看。
- **可读的终端体验 / Usable terminal experience** — TUI 提供流式对话、结构化工具反馈、Markdown、状态栏、斜杠命令和可持久化的交互偏好。
- **协议集成 / Protocol integration** — 提供工作区绑定的 partial ACP v1 stdio、stdio/HTTP/SSE MCP 工具，以及按能力协商的客户端文件系统和终端操作。

## 当前范围 / Current scope

Neuro Code 仍处于 **pre-alpha** 阶段。CLI、无头 agent runtime、交互式 TUI、会话存储、权限与沙箱边界，以及 partial ACP v1 stdio 是当前已交付的纵向能力。

ACP 实现明确不宣称完整兼容：WebSocket、完整 MCP transport、资源/提示/sampling/elicitation、音频与二进制多媒体历史回放、交互式客户端 PTY 输入/resize，以及自定义扩展尚未支持。具体支持范围与限制见兼容矩阵。

Neuro Code is still **pre-alpha**. The CLI, headless agent runtime, interactive TUI, durable sessions, permission and sandbox boundaries, and partial ACP v1 stdio are implemented vertical capabilities.

The ACP implementation deliberately does not claim complete compatibility. WebSocket, the full MCP transport surface, resources/prompts/sampling/elicitation, audio and binary-multimedia history replay, interactive client PTY input/resize, and custom extensions remain out of scope. See the compatibility matrix for the exact boundary.

## 文档 / Documentation

| 内容 / Topic | 中文 | English |
|---|---|---|
| 产品说明 / Product guide | [中文](docs/zh-CN/README.md) | [English](docs/en/README.md) |
| 架构 / Architecture | [中文](docs/zh-CN/architecture.md) | [English](docs/en/architecture.md) |
| 兼容矩阵 / Compatibility | [中文](docs/zh-CN/compatibility-matrix.md) | [English](docs/en/compatibility-matrix.md) |
| 开发计划 / Development plan | [中文](docs/zh-CN/rewrite-plan.md) | [English](docs/en/rewrite-plan.md) |
| 贡献 / Contributing | [中文](docs/zh-CN/CONTRIBUTING.md) | [English](docs/en/CONTRIBUTING.md) |
| 架构决策 / ADRs | [中文](docs/zh-CN/adr/) | [English](docs/en/adr/) |

## 开发与验证 / Development and verification

需要 Python 3.12 或更高版本，推荐使用 `uv`：

Python 3.12 or newer is required; `uv` is recommended:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

可通过 `PYTHONPATH=src python -m neuro_code inspect --json` 查看已生效的配置；输出会保护密钥。

Use `PYTHONPATH=src python -m neuro_code inspect --json` to inspect effective configuration without exposing credentials.

## 许可证 / License

[Apache-2.0](LICENSE)
