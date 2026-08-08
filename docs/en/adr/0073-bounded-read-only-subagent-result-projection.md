# ADR 0073: Bounded read-only subagent result projection

[简体中文](../../zh-CN/adr/0073-bounded-read-only-subagent-result-projection.md) · **English**

## Status

Accepted for Stage5CS.

## Context

Stage5CR provides an explicit isolated read-only runtime, but its internal
`SubagentRunResult` still contains the complete `AgentRunResult`.  That value
is useful inside the application workflow, but it is too broad for a caller
facing application entry: it includes messages, session items, events, and
the child response without a projection boundary.

## Decision

Add `ReadOnlySubagentApplicationService` and
`SubagentResultProjection` in the application workflow:

- The caller submits the existing bounded `RunSubagentRequest` explicitly.
- The facade delegates exactly one run to the isolated service; it does not
  schedule, retry, or append to the parent transcript.
- A result requires the durable parent/child `SubagentLink` and a matching
  child session ID.
- The returned projection contains only parent session ID, task ID, child
  session ID, terminal task status, bounded step count, optional typed
  execution outcome, and a redacted response.
- Messages, session items, events, tool arguments, credentials, snapshots,
  and raw model context are never returned by this boundary.
- Response redaction happens before UTF-8 byte bounding; truncation is explicit
  and deterministic.

The composition root supplies configured redaction values and exposes a
factory for this application service.  No CLI, TUI, ACP, AgentRuntime,
automatic scheduling, write-capable tool, or parent-context integration is
added in this stage.

## Rejected alternatives

- Returning `AgentRunResult` directly would leak a broad child transcript
  projection to every caller.
- Persisting the projection in the parent session would mix child output into
  the parent conversation and create a new transcript ownership contract.
- Silently accepting a missing or mismatched parent/child link would make the
  result impossible to audit after restart.
