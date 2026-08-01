# ADR 0047: Recoverable managed-provider proxy settings

**English** · [简体中文](../../zh-CN/adr/0047-recoverable-managed-provider-proxy-settings.md)

Status: accepted

## Context

Managed provider profiles previously inherited environment proxy behavior but
the TUI did not expose that choice. A valid DeepSeek profile could therefore be
saved successfully and then fail during provider construction when an ambient
`ALL_PROXY=socks://...` value was rejected. The settings screen had already
closed, so the top-level configuration error terminated the TUI and left no
in-application repair path. Storage already supported atomic credential-aware
deletion, but no TUI action exposed it.

## Decision

- Persist one user-wide default of `environment`, `direct`, or `explicit` for
  managed providers. A profile inherits that default unless it explicitly
  overrides it. Explicit policies persist only an environment-variable name,
  never the resolved proxy URL or credentials. Legacy environment-default
  profiles migrate to inheritance, while `direct` and explicit policies remain
  profile-specific overrides.
- Use the runtime's shared `HttpClientPolicy` resolver before saving. This is
  local structural/dependency validation and does not contact a model endpoint.
- During TUI startup preflight, validate the selected managed profile before
  composing the runtime. A recoverable failure opens the focused provider
  screen with that profile selected and the redacted error visible. Explicit
  CLI overrides and unmanaged configuration retain fail-closed CLI errors.
- Require a second confirmation before deleting a managed profile. Metadata and
  its credential entry are removed atomically, and storage selects a stable
  remaining default when possible.
- Provide a `socks` package extra for explicit `socks5://`/`socks5h://` use.
  Continue rejecting ambiguous `socks://` rather than guessing a protocol.

## Consequences

Users can repair inherited proxy failures without leaving the TUI, choose a
global direct route, or override one provider with a dedicated proxy variable
without placing a proxy secret in managed JSON. Saving remains atomic after
validation. The pre-save check does not prove endpoint reachability or API-key
validity; those remain request-time provider outcomes. ADR 0048 adds a
separate, explicit read-only connection test without changing offline save
semantics. PAC processing, proxy mounts, and platform keychain storage remain
future work.
