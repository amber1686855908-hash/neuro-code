# 智能体开发规则 / Agent instructions

- [中文完整规则](docs/zh-CN/agent-instructions.md)
- [Full instructions in English](docs/en/agent-instructions.md)

## 必须遵守 / Mandatory

- 不得机械移植外部项目的模块或目录结构；应按可测试的用户能力完成纵向切片。
- Do not mechanically port external modules or directory structures; deliver testable vertical user capabilities.
- 开发新能力时，可按需参考只读的 `/home/amber/Projects/codex` 中相关源码与测试，借鉴可观察
  行为、安全边界和跨平台处理；不得修改该检出，也不得机械移植实现。
- When developing a new capability, consult relevant code and tests in the read-only
  `/home/amber/Projects/codex` checkout as needed for observable behavior, safety boundaries,
  and cross-platform handling; do not modify it or mechanically port its implementation.
- 所有副作用必须经过权限、工作区或平台适配器，且不得泄露凭据或弱化显式沙箱要求。
- Route side effects through permissions and workspace/platform adapters; never expose credentials or weaken an explicit sandbox.
- `docs/en/` 和 `docs/zh-CN/` 必须保持相同的 Markdown 文件结构。
- Keep identical Markdown file sets under `docs/en/` and `docs/zh-CN/`.

## 完成检查 / Completion checks

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```
