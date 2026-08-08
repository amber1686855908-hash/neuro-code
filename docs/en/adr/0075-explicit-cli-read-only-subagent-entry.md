# ADR 0075: Explicit CLI read-only subagent entry

## Status

Accepted for Stage5CU.

## Context

Stage5CQ–Stage5CT established the bounded subagent application workflow, a
fresh read-only child runtime, a redacted result projection, and a read-only
parent/child relationship query. Those capabilities were application seams
only; no inbound interface could invoke one explicitly.

## Decision

Add one explicit CLI command:

```text
neuro subagent --parent-session SESSION_ID PROMPT
```

The command requires an existing parent session, runs the existing parent
resume preflight, creates the composition-owned read-only application
service, and runs exactly one bounded request. The child uses a fresh session
and the fixed read-only capability set; child model steps are limited to
twelve (eight by default). Plain mode prints only the bounded redacted
response. `--json` exposes only stable `SubagentResultProjection` fields.
Child resources are closed before the command returns.

The command does not reuse the parent transcript, expose child
messages/events or tool arguments, schedule work, retry, recursively spawn, or
add a TUI/ACP entrypoint. It adds no database schema and does not change the
normal `agent`, provider, session, or transcript behavior.

## Rationale

An explicit CLI entry makes the first subagent capability user-invokable
without introducing automatic delegation or a second presentation protocol.
Keeping output at the interface serialization boundary prevents internal child
state from becoming a wire contract.

## Consequences

Provider selection and execution-control options remain explicit CLI settings,
while read-only capabilities and child isolation remain composition-owned.
TUI/ACP invocation, automatic scheduling, retry, recursive spawn,
write-capable tools, and parallel children remain future work.
