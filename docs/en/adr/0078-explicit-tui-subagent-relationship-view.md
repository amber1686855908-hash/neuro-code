# ADR 0078: Explicit TUI Subagent Relationship View

## Status

Accepted for Stage5CX.

## Context

The application already exposes a bounded, read-only
`SubagentRelationshipQueryService`. TUI users need a way to inspect child
subagent lifecycle metadata without invoking a child, replaying a transcript,
or learning implementation details from storage.

## Decision

Add the explicit `/subagents` TUI command. It queries the current session only
and renders bounded parent-task/child-session identifiers, provider/model
labels, task status, timestamps, and capability labels. The command never
executes `resume`, `fork`, or `delete`, and it does not load child messages,
prompts, tool arguments, output, credentials, or events.

Missing sessions, unavailable services, invalid arguments, and empty results
fail closed without starting a model turn. The view uses the composition-owned
application query service rather than reading SQLite from TUI code.

## Rejected alternatives

- Automatically resuming or forking a child from the listing would turn a
  read-only view into an execution entrypoint.
- Rendering child transcript or prompt text would bypass the bounded
  relationship projection and leak untrusted or sensitive data.
- Adding a second TUI-specific storage query would create a competing owner.

## Consequences

The TUI now has a safe inspection seam for parent/child relationships. Future
resume, fork, and delete controls must use their existing application
lifecycle services and separate tests; this stage intentionally adds no such
controls, persistence, or model activity.
