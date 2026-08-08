# ADR 0071: Explicit bounded subagent lifecycle

[简体中文](../../zh-CN/adr/0071-explicit-bounded-subagent-lifecycle.md) · **English**

## Status

Accepted for Stage5CQ.

## Context

`SessionTaskKind.SUBAGENT` has been reserved by the durable session-task
lifecycle, but the reservation must not be mistaken for an executable
subagent runtime.  Automatic scheduling, parent-context inheritance, and a
second permission or sandbox path would be unsafe without an explicit user
contract.

## Decision

Add `SubagentExecutionService` as a narrow application workflow boundary for
one explicitly requested subagent run.  The service:

- accepts a bounded `RunSubagentRequest` containing only the parent session ID,
  a bounded prompt, and a bounded step limit;
- creates a metadata-only `SUBAGENT` `SessionTask` before invoking an injected
  `SubagentExecutor`;
- marks the task `COMPLETED`, `FAILED`, or `CANCELLED` exactly once;
- propagates the executor result, exception, and cancellation without
  conversion;
- passes the request and optional event sink to the executor without storing
  the prompt or output in the task record.

The executor is required to create a fresh child runtime/context.  It is not
allowed to reuse the parent conversation implicitly.  Capability selection,
provider choice, tools, permissions, sandbox, and event projection remain
executor/composition concerns and are not inferred by this service.

The service is explicit and caller-driven.  It has no queue, retry policy,
automatic scheduler, parent-context projection, ACP method, CLI command, or
TUI command in this slice.  A process-local lock serializes calls through one
service instance; cross-process coordination requires a later storage
contract.

## Boundaries

- No AgentRuntime main-loop or ModelProvider contract changes.
- Stage5CQ itself needs no schema migration because `subagent` is a canonical
  task kind and the existing lifecycle columns are sufficient.  The later
  Stage5CR isolated-runtime slice adds its separate parent/child link table in
  schema version 12; that migration is not part of this lifecycle decision.
- No prompt, tool argument, credential, raw output, or parent transcript is
  persisted by the new application service.
- Failure to create an executor does not leave a running task record.
- An executor failure is not reported as successful subagent completion.

## Rejected alternatives

- Automatically starting queued `SUBAGENT` tasks: this would turn durable
  metadata into an implicit scheduler without a user-visible approval and
  resource policy.
- Reusing the parent `AgentConversation`: this would mix child output and
  budgets into the parent transcript and violate isolation.
- Adding a second provider/tool protocol: the lifecycle boundary should not
  duplicate or weaken the existing Runtime and Provider contracts.
