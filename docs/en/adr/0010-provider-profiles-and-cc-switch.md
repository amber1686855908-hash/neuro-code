# ADR 0010: Named provider profiles and optional CC Switch compatibility

[简体中文](../../zh-CN/adr/0010-provider-profiles-and-cc-switch.md) · **English**

Status: accepted

## Context

The first Python vertical slice used one `[provider.default]` table and defaulted
to xAI. That makes provider identity, wire protocol, endpoint, and credential
policy one concept. It also makes a resumed session unable to distinguish a
stable provider profile from a gateway that changed its upstream.

[CC Switch](https://github.com/farion1231/cc-switch) manages user-supplied
provider credentials/configuration and can expose a loopback proxy with protocol
conversion. It does not provide model access without a legitimate API key,
OAuth grant, relay token, or local endpoint. Making it mandatory would couple
Neuro Code's runtime to another application's process and private state.

## Decision

- Use named `[providers.<name>]` profiles plus `[routing] default` and optional
  `[routing] fallbacks`.
- Separate profile identity from the wire protocol: `openai-chat`,
  `openai-responses`, `anthropic-messages`, or `gemini-generate-content`.
  Provider-specific behavior is an optional dialect; xAI is a Responses dialect.
- Store only environment-variable references. Accept `PROXY_MANAGED` only for a
  validated plain-HTTP loopback URL. Never import other inline keys.
- Read a CC Switch-exported TOML file named by `NEURO_CODE_CC_SWITCH_CONFIG` at
  the lowest precedence and translate only its active profile in memory. Do not
  read its database or control its process.
- Resolve configuration in this order: CC Switch/legacy user configuration,
  Neuro Code user configuration, project configuration, environment overrides,
  then CLI overrides.
- With no selected profile, inspection remains available but a model run fails
  with setup guidance. There is no implicit xAI provider.
- Persist a non-secret profile-affinity fingerprint with new sessions. Opaque
  native context replays only on an exact fingerprint match. Auto-detected CC
  Switch profiles disable native context by default because their upstream may
  change. Legacy Rust/xAI sessions without a fingerprint retain strict official
  xAI HTTPS and trusted-source-marker checks.
- Upgrade SQLite schema v1 to v2 transactionally by adding the nullable affinity
  field; do not rewrite existing rows.

## Consequences

Neuro Code can call providers directly or through CC Switch without a mandatory
runtime dependency. Users can select profiles per invocation, and configuration
inspection remains secret-free. Existing TOML remains readable, while new
configuration no longer embeds an xAI default.

Safe pre-output failover is now defined by
[ADR 0011](0011-safe-pre-output-provider-failover.md). Candidate retry, circuit
breaking, persistent health, CC Switch process/database integration, OS keyring
storage, and a Neuro Code HTTP proxy remain separate future work.
