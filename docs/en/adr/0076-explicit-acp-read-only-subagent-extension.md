# ADR 0076: Explicit ACP read-only subagent extension

## Status

Accepted for Stage5CV.

## Context

Stage5CU added an explicit CLI entry over the read-only subagent application
service. ACP already has a private, namespaced extension seam for bounded
session-scoped operations, but the standard ACP method set has no subagent
operation. Adding an invented standard method would make the wire contract
invalid for clients that do not know it.

## Decision

Expose one opt-in private extension:

```text
_neuro-code/session/subagent
```

The request payload is limited to `sessionId`, `prompt`, and optional
`maxSteps` (default 8, maximum 12). The session ID is an external ACP ID; the
adapter resolves it to an internal session only after validating the request.
The application service checks that the parent belongs to the current
workspace, then invokes the existing read-only subagent application service.

The response contains only status, bounded redacted response text, child step
count, truncation state, and an optional typed execution outcome. Internal
parent/task/child IDs, messages, events, prompts, tool arguments, credentials,
and child context are never returned.

Provider failures and child failures are mapped to bounded ACP internal-error
reasons; cancellation remains cancellation. The extension does not advertise
a new standard ACP capability, does not reuse parent context, and does not
introduce scheduling, retry, recursion, parallel children, or write-capable
tools.

## Rationale

Using the existing private extension route preserves ACP protocol validity
while giving clients that explicitly opt in a real read-only subagent
vertical slice. Keeping the application service as the owner preserves the
same isolation and redaction guarantees as the CLI entry.

## Consequences

No schema or normal ACP session lifecycle changes are required. Clients must
know the private method explicitly; standard ACP clients continue to see the
existing method and capability set. TUI invocation, automatic delegation,
and write-capable or recursive subagents remain future work.
