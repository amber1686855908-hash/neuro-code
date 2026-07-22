# ADR 0046: Global CLI and managed provider settings

[简体中文](../../zh-CN/adr/0046-global-cli-and-managed-provider-settings.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

A normal installation should launch the TUI from any directory without a
virtual-environment activation step. The TUI also needs to bootstrap an
unconfigured installation and manage multiple provider profiles without asking
users to edit TOML or export API-key variables. Existing manual TOML and
environment-variable profiles must remain valid, and a workspace must not be
able to redirect a user-owned stored key.

## Decision

- Publish `neuro` and `neuro-code` console scripts and accept `code` as an
  explicit TUI subcommand. All no-prompt forms retain `Path.cwd()` as the
  default workspace. Textual is a normal dependency, so `uv tool install` or
  `pipx install` creates a complete globally callable TUI.
- Add a `ProviderSettingsStore` port and a JSON adapter. It stores validated
  provider metadata in `providers.json` and API keys in a separate
  `credentials.json`, using atomic replacement and owner-only POSIX modes.
  Secret-bearing dataclass fields opt out of representation and comparison.
- Load managed profiles after TOML. A same-name managed profile replaces the
  whole provider table; it never inherits a project endpoint, proxy, built-in
  tool, or authentication option.
- Run first-use provider setup before `ApplicationComposition.open`. The normal
  Settings entry opens a first-level category page, then separate language and
  provider detail screens; first use goes directly to the required provider
  form. Save-and-use closes the active composition and background scopes, then
  reloads configuration through a bounded TUI restart code. Selecting an
  existing managed profile persists its default.
- Name wire behavior in provider presets instead of treating every OpenAI SDK
  service as the same protocol. `OpenAI Responses` selects `/responses`,
  `Compatible Chat` selects `/chat/completions`, and the dedicated DeepSeek
  preset selects Chat Completions with `https://api.deepseek.com`.
- Expose atomic profile deletion through the storage port so a profile and its
  separate credential entry are removed together. A deletion UI remains a
  later slice.
- Carry configured credential values in a representation-safe
  `ToolContext.redaction_values` field and redact actual tool results before
  they enter model context, events, or persistence.

## Consequences

- Manual `~/.neuro-code/config.toml` and environment-key profiles remain
  supported, and explicit CLI provider overrides still win for that launch.
- The current credentials file is private but not encrypted at rest. The port
  deliberately permits a future OS-keychain adapter without changing TUI or
  application contracts.
- A provider deletion UI, remote model discovery, native installers, and
  migration to an OS keychain remain outside this slice.
