# ADR 0110: Cross-platform local-process lifecycle capability contract

## Status

Accepted. Phase 1–3 add an explicit lifecycle capability contract to the
canonical local-process port. This ADR does not implement a macOS Seatbelt
adapter and does not change the existing Linux or Windows security boundary.

## Context

`LocalProcessLifecycle` previously named its cancellation operation
`TERMINATE_PROCESS_TREE`, but did not state what descendant boundary the
selected platform could actually own. The same request shape therefore
covered a strong Linux/Windows boundary and a POSIX process group that a
`setsid()` descendant can leave.

Filesystem and network authority are separate from descendant lifecycle
ownership. A platform may enforce workspace, environment, private-directory,
or network policy while only offering best-effort process-group cleanup.

## Decision

Add `LocalProcessLifecycleCapability` with two values:

- `STRONG_DESCENDANT_OWNERSHIP`
- `PROCESS_GROUP_BEST_EFFORT`

The capability order is explicit and is implemented only by
`lifecycle_capability_satisfies()`; adapters must not compare `StrEnum` values
or strings directly:

```text
STRONG_DESCENDANT_OWNERSHIP >= PROCESS_GROUP_BEST_EFFORT
```

`LocalProcessLifecycle.required_capability` is the caller's minimum
requirement. Ordinary Bash, background Bash, MCP stdio, and interactive PTY
requests explicitly require `PROCESS_GROUP_BEST_EFFORT`. A strong adapter may
satisfy that requirement. A best-effort adapter must reject a strong
requirement with `SandboxError` before creating an OS child.

`LocalProcessSandbox` exposes its actual capability, and `OwnedLocalProcess`
exposes the capability attached to the created child. The local terminal seam
(`TerminalPlatform` and `TerminalPlatformSession`) exposes the same capability
so `LocalInteractiveTerminalManager` can observe it without involving remote
or client-delegated terminal abstractions.

The local `BackgroundTaskManager` and each conversation task scope expose the
selected sandbox adapter's capability as runtime metadata. Task snapshots and
durable domain/session state remain unchanged; the capability is never persisted.

The canonical cancellation name is `TERMINATE_OWNED_SCOPE`. The old
`TERMINATE_PROCESS_TREE` enum value remains only as a deprecated compatibility
member; termination algorithms and grace/force bounds are unchanged.

## Capability matrix

| Adapter | Filesystem/network policy | Lifecycle capability |
| --- | --- | --- |
| Linux enabled Bubblewrap | Enforced by the existing child-scoped adapter | `STRONG_DESCENDANT_OWNERSHIP` |
| Windows Job Object / ConPTY Job | Existing Job and handle boundaries | `STRONG_DESCENDANT_OWNERSHIP` |
| POSIX `ProcessTree` (`off`) | No OS sandbox claim | `PROCESS_GROUP_BEST_EFFORT` |
| macOS Seatbelt adapter (ADR 0111) | Enforced filesystem/network/access control | `PROCESS_GROUP_BEST_EFFORT` |

The adapter is implemented by the later [ADR 0111](0111-macos-seatbelt-local-process-sandbox.md).
Endpoint Security, a System Extension, a privileged helper, and any other
hardened macOS architecture remain future candidates and are not lifecycle
solutions in this phase.

## Call-site and compatibility boundaries

- Bash, background, MCP stdio, and PTY request builders set the default
  product requirement explicitly; callers do not branch on `sys.platform`.
- Legacy background request builders retain their API but construct an
  explicit best-effort requirement instead of an ambiguous empty lifecycle.
- Linux Bubblewrap and Windows Job Object guarantees are unchanged. Their
  adapters report strong capability and continue to fail closed on their
  existing preflight and creation gates.
- POSIX process groups continue their bounded TERM-to-KILL behavior. A
  detached descendant is not described as owned.
- Capability is runtime adapter metadata only. It is not written to durable
  session or domain persistence.
- `SandboxProfile` remains unchanged and continues to describe filesystem and
  network policy, not lifecycle strength.

## Consequences

Callers can now request a minimum lifecycle guarantee and inspect the actual
guarantee without inferring it from a cancellation label or platform name.
Adding a weaker adapter cannot silently satisfy a strong workload. Strong
Linux and Windows adapters may safely provide more than the ordinary
best-effort request while the observed capability remains available to the
caller.

The contract is deliberately small: it does not add process enumeration,
PID-reuse claims, kqueue/launchd/libproc behavior, or a new macOS primitive.
