# Neuro Code (PyGrokBuild)

[简体中文](../zh-CN/README.md) · **English**

Neuro Code is an independent Python reimplementation of the open-source Grok
Build terminal coding agent. The project targets observable compatibility at
the CLI, configuration, session, tool, MCP, and ACP boundaries while using a
Python-native internal architecture.

The implementation is pre-alpha. The first supported vertical slice is the
headless agent runtime; TUI and protocol integrations are tracked in the
[compatibility matrix](compatibility-matrix.md).

## Development

Python 3.12 or newer is required. The canonical environment manager is `uv`:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The package may also be exercised without installation during bootstrap:

```bash
PYTHONPATH=src python -m pygrok_build version
PYTHONPATH=src python -m unittest discover -s tests
```

Inspect effective configuration without exposing secrets:

```bash
PYTHONPATH=src python -m pygrok_build inspect --json
```

Run a headless prompt against the default OpenAI-compatible/xAI endpoint:

```bash
export XAI_API_KEY="..."
pygrok-build -p "Explain this repository" --output-format plain
```

Native Anthropic and Gemini streaming endpoints use the same runtime. Select
one in `~/.pygrok-build/config.toml` (or the project-local equivalent) and set
an API model ID available to your account:

```toml
[provider.default]
kind = "anthropic" # or "gemini", or "openai-compatible"
model = "replace-with-an-api-model-id"
max_output_tokens = 8192
timeout_seconds = 120
```

The built-in endpoints and credential variables are:

| Kind | Default base URL | Credential variable |
|---|---|---|
| `openai-compatible` | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` |

`base_url` and `api_key_env` remain configurable for gateways and compatible
deployments. Native Anthropic/Gemini configurations require an explicit model
to prevent accidentally sending the xAI default model to another API.

Resume, list, export, and import sessions:

```bash
pygrok-build -p "Continue the work" --resume SESSION_ID
pygrok-build sessions --json
pygrok-build export SESSION_ID --format markdown --output transcript.md
pygrok-build import-session ~/.grok/sessions/ENCODED_CWD/SESSION_ID --json
```

`import-session` accepts either an upstream Grok Build session directory or
its `summary.json`. It reads the Rust JSONL files without modifying them and
atomically creates a new SQLite session while preserving the source session
ID, workspace, model, and timestamps. A duplicate session ID is rejected
rather than overwritten. The JSON report identifies skipped corrupt or
unsupported records. Reasoning/backend-tool records are not yet represented in
the canonical message model, and imported images currently use explicit text
placeholders.

Headless Bash permissions accept the compatible `Bash(...)` spelling. Every
command in a simple chain is evaluated independently, so allowing `git status`
does not implicitly allow a later command:

```bash
pygrok-build -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

Explicit deny rules override `--always-approve`. Under a restrictive Bash
policy, substitutions, redirections, multiline scripts, and other constructs
that cannot yet be decomposed safely are denied in headless mode. Commands run
with null stdin, bounded output, disabled pagers/prompts, and process-tree
cleanup on timeout or cancellation.

## Project status

- Source oracle: [`xai-org/grok-build`](https://github.com/xai-org/grok-build)
- Pinned source commit: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
- Minimum Python: 3.12
- Target platforms: Linux, macOS, Windows
- License: Apache-2.0; see [`NOTICE`](../../NOTICE) for provenance requirements

The installed `grok` executable is a compatibility alias for `pygrok-build`.
Install it in an isolated environment if an official Grok installation is also
present.

This project is not affiliated with or endorsed by xAI or SpaceXAI. “Grok” and
related names may be trademarks of their respective owners.
