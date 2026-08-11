# ADR 0109: Canonical local-process sandbox boundary

## Status

Accepted. PR 1 established the canonical port; PR 2 delivered the Linux
child-scoped Bash migration; PR 3 delivered the Linux child-scoped stdio MCP
migration; PR 4 routed local PTY/ConPTY creation through the same port; PR 5
removed the former controller-wide Bubblewrap re-exec and namespace
attestation.

## Context

Neuro Code's trusted controller hosts AgentRuntime, providers and HTTP
connections, session persistence, credentials, permissions, and inbound UI or
ACP interfaces. Model-controlled local commands must not own raw operating
system process primitives from those layers.

Historically Bash, managed background Bash, and stdio MCP each reached
`ProcessTree` through separate paths. POSIX PTY and Windows ConPTY / Job Object
paths are also concrete process owners. This made a future child-scoped
platform sandbox difficult to audit and risked an accidental bypass.

## Decision

Introduce the application `LocalProcessSandbox` port and its typed
`SandboxedProcessRequest`. A request declares product purpose, working
directory, explicit workspace access, sandbox profile, requested network and
environment policy, stdio mode, and bounded process-tree lifecycle. The port
returns an `OwnedLocalProcess`, rather than a raw `ProcessTree` or subprocess.

`ProcessTreeLocalProcessSandbox` is the temporary infrastructure bridge for
pipe-based Bash, background Bash, stdio MCP, and unsandboxed local PTY/ConPTY
creation. It preserves existing POSIX process-group and Windows Job ownership
semantics. Direct process creation is guarded by an AST architecture test: only
`infrastructure/sandbox/` may call `ProcessTree.spawn_*`, subprocess creation,
native `CreateProcessW`, common `os`/`pty`/`multiprocessing` process families,
or the lower-level terminal `spawn_exec` adapter.
`interfaces/tui/clipboard.py` is the sole audited host helper exception for a
user-requested desktop clipboard command; it is not a model-controlled process
launcher.

The trusted controller remains outside a child sandbox. Permission approval is
still independent from the operating-system boundary and cannot relax the
request's profile or declared policy.

The AST guard is a repository contract for built-in production code, not a
Python runtime security monitor. Infrastructure sandbox adapters and the one
listed clipboard helper are reviewed trusted code. `additional_tools`, injected
subagent executors, and any future in-process Python plugin execute with
controller authority and are therefore trusted extensions; a malicious one can
call dynamic Python, `ctypes`, or native code outside this port. Supporting an
untrusted plugin requires a separate process/capability boundary and cannot be
claimed by `LocalProcessSandbox`.

## Migration boundaries

This decision centralizes ownership. PR 2 now provides a child-scoped Linux
boundary for pipe-based Bash and background Bash:

- `LinuxBubblewrapLocalProcessSandbox` creates one Bubblewrap child per
  foreground or managed background Bash process. It starts from an empty root,
  mounts only the runtime needed to execute the child plus explicitly declared
  workspace roots, and never mounts the controller state directory.
- Every enabled child receives private `HOME` and temporary directories,
  `--clearenv` plus a small environment allowlist, and a profile-matched
  workspace mount. `READ_ONLY` and `STRICT` requests additionally require a
  network namespace; failure to establish or preflight that boundary fails
  closed.
- The adapter accepts `BASH`, `BACKGROUND_BASH`, `MCP_STDIO`, and
  `INTERACTIVE_TERMINAL` requests through their respective pipe, protocol, or
  PTY transports. Unsupported profile/transport combinations are rejected
  rather than silently running on the host.

PR 5 completes the local-process migration. The controller is never re-execed
inside Bubblewrap and no process-wide namespace marker or mount attestation is
used. `LinuxBubblewrapLocalProcessSandbox` performs the authoritative
fail-closed preflight and creates an independent child boundary for each
Bash, background Bash, stdio MCP, or enabled-profile PTY request. The
`ProcessTreeLocalProcessSandbox` bridge remains the explicit `off`-profile
implementation and is not represented as operating-system isolation. An
enabled request without a profile-capable child launcher fails closed.

Enabled Linux children use an independent PID namespace plus Bubblewrap's
parent-death lifecycle, so a child-created `setsid()` descendant cannot escape
termination with the namespace owner. `off` on POSIX retains only original
process-group cleanup and makes no stronger arbitrary-descendant guarantee.
The Linux adapter also rejects multiply hardlinked controller-state files before
mounting an authorized workspace, preventing a workspace inode alias from
reintroducing controller-private data.

## Consequences

Every newly added model-controlled local process must enter through
`LocalProcessSandbox`, and its purpose and lifecycle are reviewable in one
canonical request. Platform adapters may fail closed when a request's profile,
transport, or capabilities cannot be honored. Remote ACP terminal operations
remain client-delegated and are not treated as local child processes.

The transition intentionally retains compatibility methods on the background
task manager. They now project legacy arguments into an explicit canonical
request, preventing callers from regaining direct `ProcessTree` ownership.

Dedicated CI gates are intentionally separate from portable unit tests: Linux
must run real Bubblewrap filesystem, environment, network, hardlink, timeout,
cancellation, shutdown, grandchild, and detached-descendant tests without skips;
Windows must run native Job Object ownership and ConPTY lifecycle tests. A host
that cannot establish the requested namespace fails its security job rather than
turning lack of evidence into a green result.
