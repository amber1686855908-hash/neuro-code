# ADR 0017 — Safe interactive profile selection

[简体中文](../../zh-CN/adr/0017-safe-interactive-profile-selection.md) · **English**

## Status

Accepted.

## Context

Neuro Code already supports named provider profiles and one-shot CLI selection,
but the TUI could not change profiles after launch. Replacing only the provider
inside an existing conversation is unsafe: persisted items may contain
encrypted reasoning, provider-hosted tool state, dialect metadata, image replay
rules, or other context that is valid only for the session's original profile.

The interactive slice needs a useful picker without exposing credentials,
editing configuration, switching during an active turn, or pretending that all
provider context is portable.

## Decision

- `ProfileConversationController` is an application boundary over the active
  `AgentConversation`. It owns a lock shared by `run` and `select_profile`, so a
  profile cannot change during a model or tool turn.
- The TUI receives immutable `ProviderOption` values derived from redacted
  configuration. It displays only profile name, model, wire protocol, default/
  current markers, and readiness. Unavailable profiles and profiles whose
  referenced credential is absent are disabled. Endpoint and credential values
  never enter the picker.
- `Ctrl+P`, bare `/provider`, and bare `/model` open the picker.
  `/provider PROFILE` and `/model PROFILE` select a configured profile directly.
  The `/model` spelling is an alias for profile selection in this slice; it does
  not accept arbitrary remote model IDs or reasoning-effort values. Effort is a
  separate application policy selected through `Ctrl+E`, `/effort`, or
  `/reasoning` as defined by
  [ADR 0027](0027-semantic-tui-and-application-reasoning-effort.md).
- Reselecting the current profile is a no-op. Selecting a different profile
  asks the composition root to create a new provider, runtime, and
  `AgentConversation` against the same workspace and SQLite store. The factory
  must return a conversation without a session ID. The previous session stays
  unchanged and recoverable; the next prompt lazily creates a new session.
- Selection is process-local and does not modify TOML, environment variables,
  CC Switch data, or the configured default. The selected profile's configured
  fallback chain remains active, and normal provider-selection events may still
  report a fallback after the next prompt begins.
- Provider construction must succeed before the active binding is replaced.
  Failure leaves the existing profile and conversation intact.

## Consequences

Users can switch among explicitly configured providers without restarting the
TUI, while provider-affine state never crosses the switch boundary. The safety
tradeoff is deliberate loss of conversational continuity in the new profile;
the old conversation is retained in SQLite and can be resumed later.

The profile inventory is a launch-time snapshot. Live configuration reload,
remote model catalogs, compatible-context migration, and persistent default
editing remain future vertical slices. The application-owned effort selection
is independent of profile inventory and is reapplied to a new binding; native
provider effort mapping remains unimplemented.

## Validation

Neuro Code validates profile selection and new-session boundaries through its
own catalog, transport, and TUI behavior tests.
