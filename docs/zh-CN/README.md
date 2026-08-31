# Neuro Code

[English](../../README.md) · **简体中文**

[![CI](https://github.com/amber1686855908-hash/neuro-code/actions/workflows/ci.yml/badge.svg)](https://github.com/amber1686855908-hash/neuro-code/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](../../pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-2F5D50.svg)](../../LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#项目状态)

Neuro Code 是一个 Python 原生的终端 Coding Agent。它通过模型驱动的工作流帮助开发者理解、修改和测试代码库，同时将本地操作置于明确的工作区、权限和沙箱边界之内。

命名的 provider profile 和持久会话，让工作流可以在受支持的模型服务之间灵活切换，而不必将运行时绑定到单一托管 provider。

<p align="center">
  <img src="../NeuroCode.png" alt="Neuro Code 终端界面" width="90%">
</p>

## 为什么选择 Neuro Code？

- **Agentic coding workflow** — 从一次提示开始，在同一个 agent loop 中完成仓库检查、受边界约束的文件修改、命令执行和迭代反馈。
- **Provider flexibility** — 通过命名 profile 连接受支持的模型服务，并可按次运行或在 TUI 中切换活动 profile。
- **Explicit control** — 副作用工具经过权限策略和工作区检查；可选的 OS 级子进程 sandbox 会约束已获批准的本地操作。
- **Durable sessions** — 基于 SQLite 的会话支持继续、搜索、重命名、分叉、导出和导入；恢复时会校验工作区、provider 和 sandbox 关联。

## 快速开始

Neuro Code 目前处于 pre-alpha 阶段，当前应从源码 checkout 运行。需要 Python 3.12 或更高版本；本流程使用 [`uv`](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/amber1686855908-hash/neuro-code.git
cd neuro-code
uv sync --extra dev
uv run neuro
```

首次交互式启动时，如果没有已就绪的 profile，会打开 provider 设置。当前目录会作为工作区。配置好 provider profile 后，可以使用以下命令运行无头 prompt：

```bash
uv run neuro-code -p "Explain this repository"
```

## 核心能力

- **Coding workflow** — 无头 prompt 和 Textual TUI 共享同一个事件驱动运行时。内置工具覆盖受边界约束的文件查看与搜索、精确替换编辑、Bash、后台任务和计划。
- **Model providers** — 命名 profile 使用 OpenAI Responses、OpenAI-compatible Chat、Anthropic Messages 或 Gemini 适配器，并执行 provider 和 model 级别的能力检查。
- **Tools** — 内置仓库和 shell 工具；在受支持配置下，还可使用可选的 web search、公开网页获取和只读 LSP 集成。
- **Sessions** — 基于 SQLite 的会话支持恢复、工作区范围内的搜索、标题、分叉、导出/导入以及持久化的计划/任务元数据。
- **TUI** — Textual 界面提供流式对话、provider 和 session 选择器、审批提示、斜杠命令、Markdown 渲染以及持久化的 UI 偏好设置。

## 安全与控制

- **Workspace boundary** — 结构化文件系统操作会将目标解析到启动工作区和显式配置的根目录内；会拒绝通过链接类路径逃逸。
- **Permission boundary** — Deny/ask/allow 规则会控制副作用工具。显式 deny 始终有效；无头模式下未解决的审批请求会被拒绝。
- **Sandbox boundary** — `off`、`workspace`、`read-only` 和 `strict` profile 可用。默认 `off` profile 明确不提供 OS 隔离；在可实施时，启用的 profile 使用平台特定的子进程边界，无法支持的显式请求会失败关闭。

权限、工作区身份和 sandbox 策略是彼此独立的决策。有关当前边界，请参阅[架构](architecture.md)和[兼容性矩阵](compatibility-matrix.md)。

## 集成

- **Provider integrations** — 当前 service catalog 包含 OpenAI、xAI、Anthropic、Gemini、DeepSeek、Kimi、GLM、MiniMax、Volcengine Ark、Baidu Qianfan、Alibaba Model Studio、Tencent TokenHub，以及通用 OpenAI-compatible endpoint。实际能力取决于 provider 和 model；请参阅[兼容性矩阵](compatibility-matrix.md)。
- **MCP** — 由 session 持有的 MCP server 连接支持 stdio、Streamable HTTP 和 legacy SSE transport，并提供有边界的工具发现与执行。
- **ACP** — 提供 partial ACP v1 适配器，支持换行分隔的 stdio，并提供有边界的 WebSocket bridge，以及部分工作区绑定的 session、permission、filesystem 和 terminal 能力。ACP 兼容性明确是 partial；请参阅[兼容性矩阵](compatibility-matrix.md)。

## 项目状态

Neuro Code 处于 **pre-alpha** 阶段。当前源码树已包含 CLI 和无头运行时、Textual TUI、命名 provider profile、本地工具、SQLite session、权限与 sandbox 控制、MCP 连接以及 partial ACP 的已实现切片。

Provider/model 兼容性、平台 sandbox 覆盖范围和协议能力仍在演进。当前支持边界请查看[兼容性矩阵](compatibility-matrix.md)，路线图请查看[开发计划](rewrite-plan.md)。

## 文档

| 内容 | 简体中文 | English |
|---|---|---|
| 架构 | [架构](architecture.md) | [Architecture](../en/architecture.md) |
| 兼容性 | [兼容性矩阵](compatibility-matrix.md) | [Compatibility matrix](../en/compatibility-matrix.md) |
| 路线图 | [开发计划](rewrite-plan.md) | [Development plan](../en/rewrite-plan.md) |
| 贡献 | [贡献指南](CONTRIBUTING.md) | [Contributing](../en/CONTRIBUTING.md) |
| 架构决策 | [ADR](adr/) | [ADRs](../en/adr/) |

## 参与贡献

开发流程、必要检查和文档规则请参阅[贡献指南](CONTRIBUTING.md)。

## 许可证

根据 [Apache-2.0 License](../../LICENSE) 授权。
