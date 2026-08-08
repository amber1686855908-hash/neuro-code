# ADR 0104: Explicit compaction command projection

- Status: accepted
- Date: 2026-08-08
- Scope: application memory and interface serialization

## Context

Stage5DW added an explicit live-context compaction command, but its owner
callback receives an internal `ContextCompactionTurnProjection`. That type is
deliberately suited to turn finalization and is not a stable interface result:
it can contain a validated durable item, a controlled timeout outcome, or a
propagation-only failure. Interface callers need a single bounded projection
that distinguishes a successful compaction from a no-op without seeing the
summary or source context.

## Decision

Add `ContextCompactionCommandResult` and
`project_context_compaction_command_result()` to the existing application
compaction runtime module.

The public statuses are:

- `completed`: a validated durable item was persisted;
- `not_needed`: the explicit request was disabled or non-actionable and no
  Provider/storage work occurred;
- `budget_limited`: the existing bounded wall-clock timeout produced the
  recoverable `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome.

The projection contains only the opaque compaction ID, bounded source and
candidate counts, summary token metadata, and the canonical timeout outcome.
It never contains summary text, source fingerprints, prompts, messages, tool
output, credentials, or exception text.

Provider, cancellation, storage, and unknown failures remain exceptions. The
projection helper fails closed for propagation-only failures instead of
turning them into a result. CLI and ACP serializers use the same bounded
fields, but no command, normal Agent loop, event, or automatic trigger is
enabled by this ADR.

## Transaction boundary

The `completed` projection is returned only after the existing persistence
service has confirmed its short compaction write. Provider generation and
that write are not one transaction. The projection itself performs no storage,
event, or transcript mutation.

## Consequences

Future CLI/TUI/ACP command handlers can share a stable result shape and keep
propagation semantics explicit. A later interface integration must still
choose its own user-facing text and must not expose the bounded metadata as a
summary substitute.
