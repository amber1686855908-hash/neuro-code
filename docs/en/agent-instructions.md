# Neuro Code agent instructions

[简体中文](../zh-CN/agent-instructions.md) · **English**

## Source of truth

- The historical Rust source oracle is pinned to commit
  `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`; attribution is documented in the
  root README.
- Treat a local source checkout and its `.ua` directory as read-only.
- Source code, tests, and executable behavior override generated `.ua` text.
- Do not mechanically translate crates or synchronize files by path.

For the optional baseline check, set `NEURO_CODE_SOURCE_REPOSITORY` to the local
checkout or pass `--source` to `scripts/check_source_baseline.py`.

## Architecture

- Deliver vertical user capabilities through the canonical ports in
  `src/neuro_code/application/ports` (`neuro_code.application.ports.*`).
  New production code must use the canonical application ports paths.
- Domain/application code must not depend on UI, provider, database, or
  platform implementations.
- Preserve CLI/config/session/protocol compatibility at boundaries while using
  Python-native internals.
- Route side effects through permissions and workspace/platform adapters.
- Never weaken an explicitly requested sandbox or expose a credential.
- Keep identical Markdown file sets under `docs/en/` and `docs/zh-CN/`.

## Completion checks

Run all of the following for implementation changes:

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```

Update the compatibility matrix in both languages and relevant architecture or
ADR material whenever observable compatibility or a stable internal boundary
changes.
