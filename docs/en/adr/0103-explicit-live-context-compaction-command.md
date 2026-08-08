# ADR 0103: Explicit live-context compaction command

- Status: accepted
- Date: 2026-08-08
- Scope: `AgentRuntime` and `AgentConversation`

## Context

Stages 5DU and 5DV provided a locked owner seam and deterministic usage/stale-
source builders, but an application caller still had to rebuild the live model
context and supply persistence metadata itself. That duplication made it easy
to compact an obsolete snapshot or omit the current request guidance.

Automatic compaction is still intentionally out of scope. A command must be
explicit, bounded, and serialized with the same conversation lock as a normal
turn.

## Decision

Add a narrow explicit application command,
`AgentConversation.run_explicit_context_compaction_with_owner()`.

The command:

- requires a persisted session and a caller-supplied provider context window;
- acquires the existing conversation `_turn_lock` before building the snapshot;
- asks `AgentRuntime.build_context_snapshot()` to apply the same reasoning,
  interaction, instruction, and skill guidance used by model requests;
- delegates request construction to the configured
  `ContextCompactionRuntimeGate`, which reuses the usage snapshot and
  stale-source builder;
- allocates bounded compaction identity/time metadata when the caller does not
  provide them;
- invokes the existing owner projection path under the same lock;
- does not append transcript items, emit events, start a normal model turn, or
  enable automatic threshold checks.

The command is actionable-owner oriented: a non-actionable assessment still
fails closed through the existing owner contract rather than calling a
Provider or storage adapter. Provider generation and persistence retain their
existing separate transaction boundaries.

## Consequences

Application callers now have one live-context entry point that cannot race a
normal turn and cannot silently reuse a stale source digest. The Runtime
facade remains thin, the ModelProvider protocol is unchanged, and future CLI or
TUI commands can call this seam without reaching SQLite directly. A later
user-facing command must define how a no-op is displayed and how an owner
finalizes a successful projection; this ADR does not add that interface.
