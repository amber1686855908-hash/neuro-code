# ADR 0149: ACP ConversationBinding Resource Closure Authority

- Status: Accepted
- Date: 2026-08-30
- Scope: bounded correctness closure after the V1 ACP interface-boundary slices
- Depends on: ADR 0145, ADR 0146, ADR 0147, and ADR 0148

## Context

The exact frozen PR #76 head is
`791ceb16e74c7e9e0fbab5882c11882417166648`. Its ACP agent still owns session
publication, persisted-session activation, fork activation, active-session
cleanup, and connection shutdown.

The pre-change audit found four ACP cleanup paths that called
`binding.background_tasks.shutdown()` directly: new-session publication
failure, load/resume activation failure, fork failure, and active-session
cleanup. That bypassed the `ConversationBindingResourceScope` already created
by the application composition. In the production composition that scope also
owns the binding's LSP manager, so the direct call could leave the LSP manager
registered until composition shutdown.

## Decision

`ConversationBinding.close()` is the only ACP authority for closing a binding.
ACP decides when a binding loses ownership and calls the canonical close method
under `asyncio.shield`. ACP does not inspect `resource_scope`, reconstruct its
callback, or close `background_tasks` directly.

`ConversationBinding` remains the application-owned resource authority. Its
existing idempotent and cancellation-resistant `ConversationBindingResourceScope`
continues to close the LSP manager and binding task scope exactly once; its
existing background-task fallback remains unchanged for bindings without a
resource scope.

The ownership transfer remains:

```text
before publication: ACP locals own binding, MCP tools, and client terminal
after publication:  _AcpSession owns binding, MCP tools, and client terminal
```

Successful publication clears the local owners. Failed new-session, resume,
and fork activation paths close every resource still owned by their locals.
Active close, delete, and connection shutdown use the existing aggregate
cleanup lock and order: prompt task, MCP tools, client terminal, then binding.
Fork durable-copy rollback remains unchanged.

## Dependency direction

```text
neuro_code.acp
        -> application ConversationBinding.close()
        -> ConversationBindingResourceScope
        -> application-owned LSP and background-task resources
```

The ACP adapter depends only on the binding close contract. It does not depend
on the concrete LSP manager, background-task implementation, or composition
resource callback.

## Non-goals

This closure does not move `_AcpSession`, add a session runtime/controller,
change capability negotiation, alter MCP or terminal protocols, change prompt
or permission behavior, or redesign cleanup error aggregation. The CLI's
separate parent-binding cleanup and the composition root's own cleanup remain
outside this ACP-owned lifecycle slice.

## Validation

Focused tests cover publication, resume, fork, active cleanup, cancellation,
exactly-once binding/MCP/terminal cleanup, and the real
`ApplicationComposition -> ACP service -> ACP agent` path. The production
composition assertion verifies that closing an ACP session closes its real LSP
manager and removes it from the composition registry. A small architecture
guard prevents ACP from bypassing `ConversationBinding.close()` or inspecting
the binding resource scope.

The V1 session runtime/controller remains unimplemented by this ADR.
