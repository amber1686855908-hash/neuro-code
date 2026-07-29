# 智能体开发规则 / Agent instructions

- [中文完整规则](docs/zh-CN/agent-instructions.md)
- [Full instructions in English](docs/en/agent-instructions.md)

## 必须遵守 / Mandatory

- 历史 Rust 源基线固定为提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`；来源说明见
  根目录 README。本地源码克隆和 `.ua` 分析目录只能读取，不能修改。
- The historical Rust source baseline is pinned to commit
  `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`; attribution is documented in the
  root README. Treat a local source clone and its `.ua` directory as read-only.
- 不得机械翻译 Cargo crate；应按可测试的用户能力完成纵向切片。
- Do not mechanically translate Cargo crates; deliver testable vertical user capabilities.
- 开发新的用户能力时，应按需参考只读的 `/home/amber/Projects/grok-build` 和
  `/home/amber/Projects/codex` 中相关源码与测试，借鉴可观察行为、安全边界和跨平台处理；不得修改
  这些检出，也不得机械移植代码或目录结构。
- When developing a new user capability, consult relevant code and tests in the read-only
  `/home/amber/Projects/grok-build` and `/home/amber/Projects/codex` checkouts for observable
  behavior, safety boundaries, and cross-platform handling; do not modify either checkout or
  mechanically port code or directory structure.
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
