# Neuro Code 智能体开发规则

**简体中文** · [English](../en/agent-instructions.md)

## 判定依据

- Neuro Code 的源代码、测试与可执行行为是唯一判定依据。
- 不得机械移植外部模块，也不得按照文件路径机械同步。
- 应交付独立设计且可测试的纵向用户能力。

## 架构规则

- 通过 `src/neuro_code/application/ports` 中的 canonical 端口交付纵向用户能力，
  代码导入路径为 `neuro_code.application.ports.*`。新的生产代码必须使用 canonical
  application ports 路径。
- 领域层/应用层代码不得依赖 UI、供应商、数据库或平台实现。
- 在外部边界保持 CLI、配置、会话和协议兼容，内部采用 Python 原生设计。
- 所有副作用都必须经过权限系统以及工作区/平台适配器。
- 不得弱化显式请求的沙箱，也不得暴露凭据。
- `docs/en/` 与 `docs/zh-CN/` 必须保持相同的 Markdown 文件集合。

## 完成检查

实现改动必须执行：

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```

每当可观察兼容行为或稳定内部边界发生变化时，必须同时更新中英文兼容矩阵以及相关
架构文档或 ADR。
