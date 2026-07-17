# Neuro Code

[简体中文](../zh-CN/README.md) · **English**

Neuro Code is an extensible Python terminal coding agent. The project targets
stable behavior at the CLI, configuration, session, tool, MCP, and ACP
boundaries while using a Python-native internal architecture.

The implementation is pre-alpha. The headless agent runtime and an initial
minimal Textual TUI are supported vertical slices; remaining interface and
protocol work is tracked in the [compatibility matrix](compatibility-matrix.md).

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
PYTHONPATH=src python -m neuro_code version
PYTHONPATH=src python -m unittest discover -s tests
```

Inspect effective configuration without exposing secrets:

```bash
PYTHONPATH=src python -m neuro_code inspect --json
```

## Interactive TUI

The development extra includes Textual. After configuring a provider, launch
the interactive interface without a subcommand:

```bash
uv sync --extra dev
uv run neuro-code
```

When installing the built package outside the development environment, include
the optional UI dependency with `pip install 'neuro-code[tui]'`. The initial TUI
provides prompt input, scrollback, streamed assistant text, provider/tool status,
and local `/help`, `/status`, `/provider`, `/model`, `/cancel`, `/clear`, `/quit`,
and `/exit` commands. Prompts in one launch share a durable session;
`--resume SESSION_ID` opens an existing session after workspace validation.

Use `Ctrl+P`, bare `/provider` or bare `/model` to open the configured-profile
picker. `/provider PROFILE` and `/model PROFILE` select directly. The picker
shows only profile name, model, protocol, and readiness; unavailable profiles
or profiles with missing credentials are disabled. It selects configured
profiles, not arbitrary remote model IDs, and does not edit configuration.
Switching is rejected during a turn. Switching to a different profile keeps
the previous SQLite session available and gives the next prompt a fresh
conversation, preventing provider-affine or encrypted context from crossing
provider boundaries.

While a turn is running, use `Ctrl+C` or `/cancel` to request cancellation. The
runtime records cancellation, balances any active and not-yet-started local tool
calls with error results, reloads the durable conversation, and leaves the same
session ready for another prompt. The current slice retains the cancelled user
message in session history; pristine pre-token rewind and draft restoration are
not implemented yet.

When a side-effecting tool resolves to `ask`, the TUI opens a fail-closed
approval modal. Deny is focused by default. Choose allow once, allow the
identical tool/argument action for this process session, or deny; `Esc` also
denies, as does `Ctrl+C` while the modal is open. Edit summaries show the
workspace path but hide replacement/patch content, while Bash shows the bounded
command being authorized. Session grants
store only an in-memory exact-action digest and are checked after policy, so an
explicit deny can never be overridden. Closing or cancelling approval never
starts the tool.

`Ctrl+C` inside the approval modal denies that one request; it does not invoke
whole-turn cancellation.

Use `--always-approve` only in a workspace where unrestricted tool execution is
intentional. For scripts and machine-readable output, retain the headless path;
unresolved approval remains denied there:

```bash
neuro-code -p "Explain this repository"
neuro-code agent -p "Explain this repository" --output-format jsonl
```

## Model providers

Neuro Code has no implicit cloud provider. Configure at least one named profile
in `~/.neuro-code/config.toml` or `.neuro-code/config.toml`; otherwise model
runs fail with setup guidance instead of silently targeting xAI.

```toml
[routing]
default = "deepseek"
fallbacks = ["anthropic"]

[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
auth = "env"
api_key_env = "DEEPSEEK_API_KEY"
max_output_tokens = 8192
timeout_seconds = 120
proxy_mode = "environment"

[providers.anthropic]
protocol = "anthropic-messages"
model = "replace-with-an-api-model-id"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

Supported wire protocols are `openai-chat`, `openai-responses`,
`anthropic-messages`, and `gemini-generate-content`. Configuration stores only
an environment-variable name. Neuro Code never writes a raw API key, never
loads project `.env` files, and redacts credentials from inspection and errors.

Inspect and select profiles without editing the default:

```bash
neuro-code providers list
neuro-code providers inspect deepseek --json
neuro-code -p "Explain this repository" --provider deepseek
```

`--provider`, `--model`, and `--base-url` are one-shot overrides. The matching
`NEURO_CODE_PROVIDER`, `NEURO_CODE_MODEL`, and `NEURO_CODE_BASE_URL` environment variables
are applied before CLI overrides. Legacy `[provider.default]` and
`[model.default]` tables remain readable during migration.

### Safe provider failover

`[routing] fallbacks` defines an ordered list of alternative profiles. Provider
instances are created lazily, so an unavailable fallback does not prevent the
primary profile from running. Each candidate is attempted at most once and a
successful fallback remains selected for later model steps in the same run.

Failover is intentionally limited to failures before the candidate emits its
first model event. Once text, reasoning, a tool call, a provider-hosted tool
lifecycle event, usage, or completion has appeared, an error is propagated
without trying another provider. This commit boundary avoids replaying work
that may already have produced output, side effects, or charges. Attempts and
selections are exposed as `provider_attempt_failed` and `provider_selected`
runtime events. Use `--no-failover` to run only the selected profile:

```bash
neuro-code -p "Explain this repository" --no-failover
```

When every candidate fails, Neuro Code reports a bounded aggregate error. It
does not yet retry a candidate, maintain persistent health, or implement a
circuit breaker. See [ADR 0011](adr/0011-safe-pre-output-provider-failover.md).

### HTTP proxy policy

Every provider profile has an explicit HTTP transport policy:

- `proxy_mode = "environment"` is the backward-compatible default. HTTPX reads
  `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and its certificate
  environment. Neuro Code validates configured proxy URL schemes lazily before
  the selected profile starts.
- `proxy_mode = "direct"` sets HTTPX `trust_env = false`. This ignores proxy and
  certificate environment variables for that profile only.
- `proxy_mode = "explicit"` requires `proxy_url_env`; the named environment
  variable supplies the proxy URL without persisting it in TOML.

For example:

```toml
[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "NEURO_DEEPSEEK_PROXY_URL"
```

Inspection exposes only the mode, environment-variable name, and a configured
boolean. Proxy URLs and credentials are redacted from errors. The ambiguous
`socks://` scheme is rejected instead of guessed; use `socks5://` or
`socks5h://` with HTTPX's optional SOCKS dependency, or use an HTTP proxy.
See [ADR 0012](adr/0012-provider-http-proxy-policy.md) and the official
[HTTPX environment-variable documentation](https://www.python-httpx.org/environment_variables/).

### CC Switch compatibility

Set `NEURO_CODE_CC_SWITCH_CONFIG` to a CC Switch-exported TOML file to load it
read-only as the lowest-precedence source. Its active `[models] default` and
`[model."<profile>"]` entry become an in-memory profile named
`cc-switch:<profile>`. Backend values map as follows:

| CC Switch `api_backend` | Neuro Code protocol |
|---|---|
| `responses` | `openai-responses` |
| `chat_completions` | `openai-chat` |
| `messages` | `anthropic-messages` |

An `env_key` is used as an environment-variable reference. The
`PROXY_MANAGED` placeholder is accepted only for a plain-HTTP loopback URL,
such as `http://127.0.0.1:15721/provider/v1`. Other inline keys are neither
copied nor used; the profile is reported unavailable with remediation. CC
Switch remains optional: Neuro Code does not read its private database, manage
its process, or rely on it for direct providers. CC Switch still requires a
legitimate upstream credential, OAuth grant, relay token, or local model.

Auto-detected proxy profiles default to `native_context = "disabled"` because
the proxy may change upstream providers. A project-local override may set
`native_context = "profile"` only when the endpoint/profile is stable and
trusted. Opaque reasoning is then replayed solely when the stored profile
affinity fingerprint matches exactly.

### Optional xAI dialect

xAI is an optional Responses dialect rather than the application default:

```toml
[providers.xai]
protocol = "openai-responses"
dialect = "xai"
model = "replace-with-an-xai-model-id"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
native_context = "profile"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

The generic Responses adapter uses local `store: false` history. The xAI
dialect additionally requests encrypted reasoning, supports xAI-hosted tools,
and preserves validated reasoning/backend-tool items. Hosted tools are executed
by xAI, bypass local tool execution, emit separate backend lifecycle events,
and may incur additional charges. Stateful `previous_response_id` chaining and
compaction items are not yet implemented.

DeepSeek and other Chat Completions services use `openai-chat`. During
thinking-mode tool use, assistant reasoning associated with a tool call is
persisted and replayed on the next request; completed no-tool reasoning remains
local. Load `DEEPSEEK_API_KEY` through the shell or a secret manager before
running the selected profile.

### Opt-in live regression tests

Live tests are guarded twice because they use network access and may incur
provider charges. The normal test command excludes the `live` marker. Even
when `-m live` is selected, collection adds a skip unless
`NEURO_CODE_RUN_LIVE_TESTS=1` is present. Credentials are read only from the process
environment; the test suite never loads `.env` automatically.

After exporting `DEEPSEEK_API_KEY` in the calling shell, run:

```bash
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

Optional `NEURO_CODE_LIVE_DEEPSEEK_MODEL` and
`NEURO_CODE_LIVE_DEEPSEEK_BASE_URL` values override the safe defaults shown in
`.env.example`. The current DeepSeek checks cover real streaming, recovery from
an intentional pre-output primary failure, and a read-only local tool round
trip. They never enable Bash or write tools. Live tests inherit standard proxy
environment variables by default. Set `NEURO_CODE_LIVE_PROXY_MODE=direct` to ignore
them, or set `NEURO_CODE_LIVE_PROXY_MODE=explicit` together with the ephemeral
`NEURO_CODE_LIVE_PROXY_URL` environment variable to route only these checks. This
is useful when a local `ALL_PROXY` uses a URL scheme rejected by HTTPX; never
place proxy credentials in project configuration.

Resume, list, export, and import sessions:

```bash
neuro-code -p "Continue the work" --resume SESSION_ID
neuro-code sessions --json
neuro-code export SESSION_ID --format markdown --output transcript.md
neuro-code import-session /path/to/upstream/session --json
```

`import-session` accepts either a supported upstream Rust session directory or
its `summary.json`. It reads the JSONL files without modifying them and
atomically creates a new SQLite session while preserving the source session
ID, workspace, model, and timestamps. A duplicate session ID is rejected
rather than overwritten. The JSON report identifies skipped corrupt or
unsupported records. Ordered reasoning/backend-tool records and image URLs are
preserved structurally. JSON export schema version 2 exposes the complete
`conversation_items` sequence alongside its ordinary `messages` projection.
Legacy assistant `raw_output`, singular reasoning, and v0
`reasoning_content` are upgraded in memory; backend-tool IDs prevent an
embedded copy from duplicating an earlier standalone record. The import report
counts recovered, deduplicated, malformed, and unsupported embedded records.
Supported image references are replayed through native provider blocks:
OpenAI-compatible and Gemini user messages, plus Anthropic user and tool-result
messages. Invalid references and unsupported roles receive an explicit image
placeholder. Resuming an imported upstream session against the official xAI
HTTPS endpoint replays visible reasoning and ordered backend-tool summaries
only when the stored source marker is trusted. Opaque encrypted Responses state
is never copied into Chat Completions, and non-affine providers receive only
the ordinary-message projection. Selecting an `openai-responses` profile with
the xAI dialect instead replays validated reasoning and supported backend-tool
items natively, strips output-only
reasoning status on input, and continues to reject non-affine opaque state.

Headless Bash permissions accept the compatible `Bash(...)` spelling. Every
command in a simple chain is evaluated independently, so allowing `git status`
does not implicitly allow a later command:

```bash
neuro-code -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

Explicit deny rules override `--always-approve`. Under a restrictive Bash
policy, substitutions, redirections, multiline scripts, and other constructs
that cannot yet be decomposed safely are denied in headless mode. Commands run
with null stdin, bounded output, disabled pagers/prompts, and process-tree
cleanup on timeout or cancellation.

## Project status

- Minimum Python: 3.12
- Target platforms: Linux, macOS, Windows
- License: Apache-2.0; see [`NOTICE`](../../NOTICE) for provenance requirements
