# Neuro Code 智能体开发规则

**简体中文** · [English](../en/agent-instructions.md)

## 判定依据

- 历史 Rust 源代码判定基线固定为提交
  `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`，来源归属见根目录 README。
- 本地源代码检出和其中的 `.ua` 目录只能读取，不能修改。
- 源代码、测试和可执行行为优先于自动生成的 `.ua` 文本。
- 不得机械翻译 crate，也不得按照文件路径机械同步。

如需执行可选的基线校验，请把 `NEURO_CODE_SOURCE_REPOSITORY` 指向本地源码检出，或者向
`scripts/check_source_baseline.py` 传入 `--source`。

## 架构规则

- 通过 `src/neuro_code/ports` 中的端口交付纵向用户能力。
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
