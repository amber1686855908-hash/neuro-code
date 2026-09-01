# ADR 0150: ACP Session Runtime Ownership Boundary

- Status: Accepted
- Date: 2026-08-31
- Scope: the bounded ACP session-runtime slice stacked on PR #77
- Depends on: ADR 0145, ADR 0146, ADR 0147, ADR 0148, and ADR 0149

## Context

The exact frozen PR #77 base is
`d7dbbc645b15daf987128b7f5264cd29172b1bf8`. At that base,
`neuro_code.acp` had already extracted prompt/content, update projection,
client I/O, and MCP declaration conversion, and had made
`ConversationBinding.close()` the binding cleanup authority. The remaining
`_AcpSession` dataclass still mixed all mutable per-session interface state,
resource references, synchronization, prompt coordination, approval
presentation, internal identity, and aggregate cleanup into the connection
adapter.

The audit found these per-session fields and owners:

| State | Mutability | Pre-change access | Canonical access after this ADR |
|---|---|---|---|
| external `session_id` | immutable | Agent registry and protocol responses | `AcpSessionRuntime.session_id` |
| binding, MCP tools/names, client terminal | resource references; names are immutable snapshots | construction, MCP, protocol operations, cleanup | runtime construction plus bounded resource/MCP snapshots |
| approval broker and context-window snapshot | broker reference is stable; context snapshot is immutable | construction and prompt/permission coordination | runtime-owned references and read-only snapshots |
| internal session ID | mutable, identity-constrained | alias binding, prompt, fork/artifact lookup | synchronized begin/commit/abort identity methods |
| prompt task, event mapper, cancel flag | mutable | prompt, cancel, permission callbacks, cleanup | task-owner prompt gate and cancellation methods |
| pending approval ID | mutable | permission callback and prompt finalization | owner-checked approval begin/finish methods |
| closing/closed flags and locks | mutable synchronization state | Agent lifecycle and cleanup | runtime lifecycle and cleanup locks |

Connection-scoped client state, capability negotiation, the registry, pending
reservations, list cursors, and transport remain Agent-owned.

## Decision

Introduce `neuro_code.interfaces.acp.session.AcpSessionRuntime` as the one
canonical per-session runtime owner. It has no back-reference to
`NeuroCodeAcpAgent`, no application service locator, and no knowledge of
bootstrap, providers, stores, or transport.

The runtime provides narrow operations for:

- active-state and binding snapshots;
- one task-identity-safe prompt gate and prompt finalization;
- cancellation of the exact current prompt task;
- one pending ACP approval presentation and owner-safe release;
- a two-phase, synchronized in-memory internal-session identity transition;
- bounded MCP reference/name snapshots and refresh-name publication;
- aggregate cleanup of the prompt task, MCP tools, client terminal, and
  binding.

`NeuroCodeAcpAgent` continues to own the client connection and negotiated
capabilities, session registry and registry lock, reservations/publication,
outer `new`/`load`/`resume`/`fork` routing, list/delete/close dispatch,
`ext_method` dispatch, live MCP orchestration, and transport. The application
`SessionTurnService`/`ConversationRunner` remains the authority for actual
turn execution, turn locking, durable history, and recovery; no
`PromptController` or duplicate execution state machine is introduced.

The private `_AcpSession` name remains an identity-preserving alias to the
canonical runtime for behavior-focused compatibility tests. It is not a second
class or public export.

## Resource ownership

Before successful registry publication, the Agent's local construction path
retains rollback ownership of the binding, MCP context, and client terminal.
After publication, the `AcpSessionRuntime` is the sole active-session cleanup
owner, and the local references are cleared. Failed construction/publication
continues to close only resources still held by the local path.

The runtime retains the established order: cancel/wait for the prompt task,
close MCP tools, shut down the client terminal, then call
`ConversationBinding.close()`. The runtime never calls
`binding.background_tasks.shutdown()` and never inspects a binding resource
scope. Binding-owned LSP and background resources therefore remain governed by
the PR #77 close authority.

## Locking and concurrency

The Agent registry lock protects membership and reservation/publication only.
The runtime state lock protects per-session mutable state. The runtime cleanup
lock serializes aggregate cleanup. No runtime state lock is held while
awaiting a provider/model turn, MCP operation, terminal shutdown, binding
close, or client permission request.

Prompt finalization compares the finishing task with the currently stored
owner, so a stale task cannot clear a later prompt. Cancellation captures the
same current task under the state lock and the Agent cancels it after releasing
the lock. Closing marks cancellation before cleanup and prevents new interface
operations. Approval finalization clears only its own call ID. These
transitions do not claim that every stale reference held by an already-started
outer operation is eliminated.

MCP refresh remains Agent-orchestrated. The runtime prevents a closed session
from publishing a new MCP name snapshot, but an operation that captured an MCP
reference before close can still overlap the underlying refresh/close boundary.
That race remains explicit hardening debt rather than an unproven claim.

## Permissions and identity

The runtime may hold the application-owned `SessionApprovalBroker` and the
pending ACP presentation ID, but it does not own `PermissionManager`, policy,
or scoped-grant authority. Durable alias writes remain performed by the Agent
through the application service. The runtime only reserves and commits the
in-memory identity under its state lock, rejecting a different identity for
one external ACP session.

## Non-goals

This ADR does not move or redesign ACP transport, capability negotiation,
`ext_method`, MCP infrastructure, client I/O adapters, the application runner,
CLI/TUI/domain/persistence boundaries, or checkpoint/rollback. It does not
introduce retry, replay, cleanup-error aggregation, or a general session
repository. Existing cleanup error propagation is preserved: if an earlier
resource close raises, exhaustive later cleanup remains future hardening work.

## Validation

Focused runtime and architecture tests prove canonical class identity, no
Agent back-reference or forbidden concrete imports, registry typing, prompt
and approval ownership, cancellation/task identity, synchronized identity
binding, close-state rejection, concurrent cleanup idempotence, resource
ordering, and continued `ConversationBinding.close()` authority. Existing ACP,
raw stdio, WebSocket, E2E, client-I/O, MCP, permissions, and PR #77 resource
closure tests remain part of the validation set.
