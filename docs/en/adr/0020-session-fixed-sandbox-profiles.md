# ADR 0020: session-fixed sandbox profiles

[简体中文](../../zh-CN/adr/0020-session-fixed-sandbox-profiles.md) · **English**

## Context

An operating-system sandbox is selected before provider or tool composition and
becomes irreversible after the Linux process re-executes under bubblewrap. A
resumed session may have been created under a different profile from today's
default, user configuration, or project configuration. Applying the current
value silently would change the security and capability boundary of an existing
conversation.

The fixed historical Rust baseline records the creation profile and restores it
on resume. It rejects a canonically different explicit CLI/environment request,
while sessions created before that metadata existed retain configuration-based
behavior.

## Decision

SQLite schema v3 adds nullable `sessions.sandbox_profile`:

- every newly created native session stores one canonical built-in
  `SandboxProfile`, including `off`;
- supported upstream imports preserve a recognized built-in profile;
- migrated or imported sessions without metadata retain `NULL` and are treated
  as legacy;
- unsupported or corrupt stored values fail closed instead of becoming legacy.

Explicit-ID resume follows this startup order:

1. resolve ordinary configuration and the state database path;
2. inspect only the selected session's profile through an immutable, read-only
   SQLite connection that cannot create a database, WAL/SHM files, or migrations;
3. resolve the saved value against the current request;
4. enforce or attest the resulting process sandbox;
5. initialize/migrate SQLite and compose providers, permissions, and tools.

Resolution is deterministic:

| Saved profile | Explicit CLI/`NEURO_CODE_SANDBOX` | Result |
|---|---|---|
| absent | any or absent | use normal configuration resolution |
| present | absent | restore the saved profile |
| present | canonically equal | restore the saved profile |
| present | different | fail before sandbox/provider composition |

User/project configuration is not an explicit resume override; the saved value
wins. Aliases such as `readonly` are parsed before comparison. This permits an
explicit matching confirmation without allowing a session to be strengthened
or weakened during resume.

`AgentConversation.open` rechecks the loaded summary against the active runtime
profile as a defense in depth. An already-running TUI cannot change its process
sandbox, so its session picker disables a different-profile session and asks the
user to restart with `--resume SESSION_ID`. Legacy `NULL` sessions remain
selectable under the active profile.

## Consequences

Schema v1 migrates through v2 to v3, and v2 migrates directly to v3 without
rewriting existing session data. JSON session export is version 3 and session
list/export summaries expose the canonical profile or a legacy null value.

The preflight rejects an active SQLite WAL rather than risk ignoring
uncheckpointed metadata. A writer racing after that check is still covered by
the later ordinary load and `AgentConversation` mismatch check before a model
turn or tool action. Neuro Code does not currently coordinate multiple writers
to the same state directory.

Custom upstream profiles such as `devbox` cannot be enforced by the current
Python adapters and are rejected during import rather than downgraded. Most-
recent resume, session forks, custom profile definitions, and profile migration
for an existing session remain separate future capabilities.

The source evidence is the sandbox startup resolver and persisted session
summary at historical commit
`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`.
