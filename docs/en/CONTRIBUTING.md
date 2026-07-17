# Contributing to Neuro Code

[简体中文](../zh-CN/CONTRIBUTING.md) · **English**

Neuro Code is a behavioral reimplementation, not a mechanical translation.
Before changing a compatibility-sensitive path, identify its source evidence
in the pinned Rust tree and update the compatibility matrix.

## Required checks

```bash
uv lock --check
uv run python scripts/check_docs_parity.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=neuro_code --cov-report=term-missing
uv build
```

All checks must pass on Python 3.12. Platform-sensitive work also requires the
Linux, macOS, and Windows CI matrix.

## Credential-gated live checks

The default pytest selection excludes tests marked `live`. Running them
requires both explicit marker selection and the cost/network environment gate:

```bash
export DEEPSEEK_API_KEY="provided-by-your-secret-manager"
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

Never commit a populated `.env`, cassette, response dump, or failure artifact.
Live tests must use bounded tokens and timeouts, avoid destructive tools, and
must not print credentials, headers, or complete request bodies. A missing key
is a skip, not a failure. `.env.example` documents variable names only; neither
application nor test code loads `.env` implicitly.

## Code rules

- Domain and application code cannot import UI frameworks, provider SDKs,
  database drivers, or platform implementations.
- External dictionaries are validated at adapters; typed immutable objects
  cross internal module boundaries.
- Async tasks must have an owner, cancellation behavior, and shutdown path.
- Every side effect passes through permissions and the appropriate workspace or
  platform adapter.
- Never log credentials, authorization headers, raw secret files, or full HTTP
  request bodies.
- Explicit sandbox profiles fail closed.
- Every behavior change includes tests, compatibility status, and documentation
  or an ADR when the external contract changes.
- Every Markdown file under `docs/en/` has a Chinese counterpart at the same
  relative path under `docs/zh-CN/`, and vice versa.

## Source provenance

The upstream Apache-2.0 license permits derivative work but requires notices.
Code adapted from an upstream file must record its path and commit in the pull
request and preserve all applicable third-party attributions. Do not copy code
from a component until its entry in upstream `THIRD-PARTY-NOTICES` has been
reviewed.
