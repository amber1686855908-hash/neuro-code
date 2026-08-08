# ADR 0074: Read-only parent-child subagent relationship projection

[简体中文](../../zh-CN/adr/0074-read-only-parent-child-subagent-relationship-projection.md) · **English**

## Status

Accepted for Stage5CT.

## Context

Stage5CR persists a narrow `SubagentLink` between a parent session task and a
fresh child session. Stage5CS projects one explicit child run, but callers
also need a safe way to inspect which child sessions belong to a parent and
which lifecycle operations are available. A query boundary must not become a
second execution or mutation owner, and it must not expose the child
transcript merely because a relationship exists.

## Decision

Add `SubagentRelationshipQueryService` in the application sessions layer with
typed request and projection values:

- Listing is scoped to one parent session and has a bounded limit. Results are
  ordered by the durable link timestamp and task ID.
- A projection contains only parent/task/child IDs, canonical task status,
  child provider and model labels, child summary timestamps, and bounded
  capability labels.
- Active child tasks (`queued` or `running`) expose no lifecycle action
  labels. Terminal tasks expose the labels `resume`, `fork`, and `delete` as
  capabilities only; the existing session lifecycle services remain the sole
  owners of those mutations.
- The query validates that the parent task is a `SUBAGENT` task and that the
  child session summary exists. Corrupt ownership is reported rather than
  silently projected.
- The query reads only links, task metadata, and the child session summary.
  It never loads messages, events, tool output, prompts, credentials,
  arguments, or raw child context.
- SQLite reuses the existing `subagent_links` table with a bounded ordered
  read. No schema migration or new persistence record is introduced.
- No CLI, TUI, ACP, scheduler, replay, automatic resume, or execution call is
  added. Future interfaces may consume this projection through the
  application boundary.

## Rejected alternatives

- Returning `SubagentLink` directly would expose a storage/domain value rather
  than an interface-safe query contract and would leave lifecycle capability
  semantics implicit.
- Returning full `SessionTask`, `SessionSummary`, or session items would leak
  fields that are not needed to inspect ownership and could include sensitive
  or transcript-related data.
- Making the query execute `resume`, `fork`, or `delete` would duplicate the
  existing lifecycle owners and turn a read into an unexpected side effect.
- Exposing actions for active tasks would invite races with task completion;
  the projection therefore exposes no actions until the task is terminal.

## Consequences

Callers can render or audit parent-child relationships without depending on
SQLite or reading a child transcript. The projection deliberately does not
provide a user-facing command yet; that remains a later vertical slice with
its own authorization, concurrency, and protocol tests.
