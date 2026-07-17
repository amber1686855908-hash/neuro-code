# Neuro Code

[简体中文](docs/zh-CN/README.md) · [English](docs/en/README.md)

Neuro Code 最初基于开源 Grok Build 的行为边界进行 Python 重写，目前正作为独立、
可扩展的终端编码智能体继续演进。
当前项目处于 pre-alpha 阶段，已经具备可安装 CLI、无头代理循环、带失败关闭审批的
最小交互 TUI（含可恢复取消和安全 profile 选择）、多模型流式适配、工作区工具、
权限控制和 SQLite 会话能力。

Neuro Code began as a Python reimplementation of the open-source Grok Build
terminal coding agent and now evolves as an independent, extensible project. It
is currently pre-alpha, with an installable CLI, a headless agent loop, a
minimal interactive TUI with fail-closed approvals, recoverable in-flight
cancellation, and safe profile selection, multi-provider streaming, workspace
tools, permissions, and SQLite sessions.

## 文档 / Documentation

| 内容 | 中文 | English |
|---|---|---|
| 项目介绍 / Overview | [中文](docs/zh-CN/README.md) | [English](docs/en/README.md) |
| 架构 / Architecture | [中文](docs/zh-CN/architecture.md) | [English](docs/en/architecture.md) |
| 重写计划 / Rewrite plan | [中文](docs/zh-CN/rewrite-plan.md) | [English](docs/en/rewrite-plan.md) |
| 兼容矩阵 / Compatibility | [中文](docs/zh-CN/compatibility-matrix.md) | [English](docs/en/compatibility-matrix.md) |
| 贡献指南 / Contributing | [中文](docs/zh-CN/CONTRIBUTING.md) | [English](docs/en/CONTRIBUTING.md) |
| 架构决策 / ADRs | [中文](docs/zh-CN/adr/) | [English](docs/en/adr/) |

## 快速验证 / Quick verification

需要 Python 3.12 或更高版本，并推荐使用 `uv`：

Python 3.12 or newer is required; `uv` is the recommended environment manager:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy
```

## 当前状态 / Status

- 源基线 / Source baseline: [`xai-org/grok-build`](https://github.com/xai-org/grok-build) at `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
- 目标平台 / Target platforms: Linux, macOS, Windows
- 许可证 / License: Apache-2.0
- 完整重写进度粗略为 36–41%；M2 退出测试已经满足，M3 最小 TUI、交互审批、可恢复
  取消与安全 profile 选择切片已实现。
- Overall public-surface parity is roughly 36–41%; the M2 exit test is satisfied
  and the M3 minimal-TUI, interactive-approval, recoverable-cancellation, and
  safe-profile-selection slices are implemented.

本项目与 xAI 或 SpaceXAI 没有关联，也未获得其认可。“Grok”等名称可能是其各自所有者的商标。

This project is not affiliated with or endorsed by xAI or SpaceXAI. “Grok” and
related names may be trademarks of their respective owners.
