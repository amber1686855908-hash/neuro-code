# ADR 0072: Isolated read-only subagent runtime

[简体中文](../../zh-CN/adr/0072-isolated-read-only-subagent-runtime.md) · **English**

## Status

Accepted for Stage5CR.

## Context

Stage5CQ defined an explicit application lifecycle around an injected
`SubagentExecutor`, but it did not provide a concrete child runtime.  A real
first subagent slice must be useful for repository research while remaining
strictly narrower than the parent agent.  It must also survive restart
inspection without putting prompts, credentials, tool arguments, or raw model
output into ownership metadata.

## Decision

Add `IsolatedSubagentExecutionService` and a composition-owned
`CompositionReadOnlySubagentRuntimeFactory` for one explicit, synchronous
read-only run:

- Every request creates a fresh child session and a metadata-only parent
  `SUBAGENT` task.
- A durable `SubagentLink` stores only parent session/task IDs, child session
  ID, and a timezone-aware creation timestamp.  The link is written before the
  child runtime starts.
- The child uses a fresh `AgentConversation` binding.  Its provider profile is
  copied with provider builtin tools removed, and its registry is limited to
  `read_file`, `list_dir`, `grep`, and `skill`.
- Write-capable filesystem tools, Bash, client terminal, background tasks,
  automatic wake, scheduling, and recursive spawn are not available to this
  factory.
- The child receives a fresh prompt only.  Parent messages and mutable parent
  context are never copied into the child runtime.
- Child model steps are bounded by `RunSubagentRequest.max_steps`, and child
  execution has a finite wall-clock limit capped by
  `MAX_SUBAGENT_TIMEOUT_SECONDS`.
- Cancellation is propagated after shielded runtime cleanup and a durable
  `CANCELLED` parent task update.  Timeout becomes a typed
  `SubagentTimeoutError`; provider/runtime failures remain failures.
- Deleting a parent session recursively deletes linked child sessions through
  the foreign-keyed `subagent_links` table.

The service is still explicit and caller-driven.  It is not connected to the
normal `AgentRuntime` loop, CLI, TUI, ACP, an automatic scheduler, or a result
projection into the parent transcript.

## Persistence and transaction boundary

Schema version 12 adds `subagent_links` with a composite parent-session/task
key and a unique child-session ID.  Saving one link is one SQLite write
transaction and validates the parent task kind/status and child existence.
It is not a claim that child creation, link persistence, model execution,
task completion, and session events are one cross-process transaction; the
runtime only guarantees that the link is attempted before child execution.

## Capability and security boundary

The child capability set is a fixed subset of the parent composition's
available infrastructure.  The factory does not grant a new permission or
sandbox bypass, and provider builtin tools are removed before provider
construction.  The read-only registry filter is a composition constraint, not
a replacement for tool, workspace, permission, or sandbox checks.

## Rejected alternatives

- Reusing the parent conversation would mix transcripts, budgets, and provider
  context.
- Passing the full parent tool registry would make the read-only contract
  accidental and allow write-capable tools.
- Creating a second provider or permission protocol would duplicate existing
  infrastructure boundaries.
- Automatically starting child tasks, retrying them, or exposing a CLI/TUI/ACP
  command would expand this narrow capability into scheduling and product
  policy before its lifecycle is proven.
