# ADR 0125 — Subagent capability closure

[简体中文](../../zh-CN/adr/0125-subagent-capability-closure.md) · **English**

## Status

Accepted for the current pre-alpha runtime.

## Context

Neuro Code has two child-runtime workflows: the scope-aware scheduler and the
explicit read-only subagent service. The scheduler already resolved a child
request against parent and global capability manifests. The explicit service
historically rebuilt a read-only manifest from the root composition, which
left no architectural proof that a nested child stayed within its actual
parent binding.

Durable subagent relationship operations (`resume`, `fork`, and `delete`) are
session lifecycle operations. They do not recreate a child `AgentRuntime`.
The legacy arbitrary `SubagentExecutor` seam also remains useful to existing
tests, but it is not a capability-aware production boundary.

## Decision

- `ConversationBinding.capabilities` is the canonical parent authority for
  production child creation. The CLI creates a parent binding for the
  headless command; TUI reads the active binding; ACP requires an active parent
  binding. Missing metadata fails closed.
- Both child workflows use the composition-owned global capability ceiling.
  The explicit service first converts its fixed read-only tool names into a
  request, then calls `SubagentCapabilitySet.resolve_child(parent, requested,
  global_policy)` before creating the child task or binding.
- The resolved immutable manifest is passed to the child factory and to
  `ApplicationComposition.create_binding(capabilities=...)`. Construction
  recomputes the concrete binding manifest and rejects mismatches; the runtime
  fingerprint is checked before execution.
- `READ_ONLY_SUBAGENT_TOOL_NAMES` is a request policy, not an authority. It is
  intersected with the parent manifest and cannot restore tools, workspace
  roots, sandbox strength, MCP, terminal, or network capability.
- `SubagentExecutionService` and its arbitrary executor factory are retained
  only as an explicitly marked test/internal compatibility seam. The
  composition root rejects ordinary production binding of that seam.
- Child relationship resume/fork/delete remains lifecycle-only. A normal ACP
  session fork is an independent session binding, not recursive subagent
  construction.

## Consequences

The invariant proven by this decision is only:

> Any production child runtime capability is no broader than the actual
> parent capability.

It does not prove the complete permission, workspace, operating-system
sandbox, MCP transport, provider transport, or agent security systems. The
explicit child workflow remains synchronous and read-only; automatic
delegation and unrestricted child tools remain outside this decision.
