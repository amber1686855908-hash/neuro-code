# 贡献指南 / Contributing

- [中文贡献指南](docs/zh-CN/CONTRIBUTING.md)
- [English contributing guide](docs/en/CONTRIBUTING.md)

所有实现改动都必须通过以下检查。All implementation changes must pass:

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```

新增或修改英文 Markdown 文档时，必须在 `docs/zh-CN/` 的相同相对路径提供中文版本；
中文文档也必须在 `docs/en/` 提供英文对应版本。

Every English Markdown document added or changed under `docs/en/` must have a
Chinese counterpart at the same relative path under `docs/zh-CN/`, and vice
versa.
