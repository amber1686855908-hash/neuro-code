# ADR 0081: Explicit ACP Subagent Lifecycle Extension

- Status: accepted
- Date: 2026-08-08
- Scope: Stage5DA

## Context

Stage5CZ exposed the parent-owned subagent lifecycle service through the CLI.
ACP clients also need a bounded way to request the same explicit `resume`,
`fork`, or `delete` action without receiving internal SQLite identifiers or
child transcript content.

## Decision

Stage5DA adds the private extension
`_neuro-code/session/subagents`. The request is strict and contains only:

- `sessionId`: the external ACP session alias for the parent;
- `taskId`: the bounded parent `SUBAGENT` task identifier;
- `action`: `resume`, `fork`, or `delete`.

The ACP adapter resolves the parent through the existing alias and workspace
boundary, then delegates a typed request to
`SubagentRelationshipLifecycleService`. `resume` and `fork` return a newly
allocated external ACP alias only. `delete` returns `{action, deleted}` and
does not expose the deleted child ID. Internal session IDs, prompts, child
messages, events, tool arguments, credentials, provider state, and filesystem
paths are never placed on the wire.

## Boundaries

The extension is not advertised as a standard ACP capability. It does not
start a model turn, replay tools, reconstruct child context, schedule or retry
work, create recursive or parallel children, or add write-capable tools. The
existing application lifecycle owner remains responsible for relationship and
terminal-task validation. ACP alias allocation is a separate bounded storage
operation; it is not claimed to be atomic with the lifecycle action.

Malformed payloads and unsupported fields fail closed with stable request
errors. Cancellation remains propagated rather than converted to a protocol
success response.

## Consequences

ACP, CLI, and TUI can use the same typed lifecycle owner while keeping their
wire projections deliberately small. No schema, provider, runtime-kernel,
finalizer, model-loop, or normal-session behavior changes.
