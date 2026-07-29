# ADR 0034: bounded, owned interactive terminal sessions

[简体中文](../../zh-CN/adr/0034-bounded-owned-interactive-terminal-sessions.md) · **English**

- Status: accepted
- Date: 2026-07-19
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

The native terminal smoke tests and Windows ConPTY owner established useful
platform evidence, but they did not provide an application contract that an ACP
endpoint or another interface could safely consume. Exposing the test helper
directly would bypass permission, workspace, sandbox, output-memory, process-tree,
and shutdown boundaries.

The terminal contract explicitly covers command, working directory, environment,
dimensions, bounded reads, input, resize, wait, and owned-process shutdown.

## Decision

Define provider- and interface-neutral terminal domain values and ports:

- `TerminalSize` validates positive platform-safe dimensions;
- `TerminalSignal` distinguishes interrupt, terminate and kill;
- `TerminalOutputChunk` returns bytes, a monotonic next cursor, the number of
  bytes dropped before that cursor, and an EOF flag;
- `InteractiveTerminalManager` creates exec-only sessions and owns shutdown;
- `InteractiveTerminalSession` exposes bounded read/write, resize, signal,
  timed wait and idempotent close; and
- `TerminalPlatform` is the synchronous OS adapter boundary fed by output,
  EOF, and failure callbacks.

`LocalInteractiveTerminalManager` keeps at most a configured number of live or
pending sessions. Each session has a random opaque ID and a thread-safe
cursor-addressed tail ring. The ring is capped at 16 MiB, one read or write is
capped at 1 MiB, and one blocking read request is capped at 60 seconds. A reader
that falls behind receives an exact dropped-byte count instead of silently
mistaking retained output for a complete transcript. Output is memory-only and
is not added to durable model/session history.

Creation follows this order:

1. validate argv, dimensions, capacity and environment;
2. resolve an existing directory inside the configured workspace;
3. evaluate the side-effecting `create_terminal` permission and, for `ask`,
   obtain asynchronous approval;
4. strip configured protected environment variables, replace terminal/pager
   controls with application-owned values, and put only an opaque environment
   fingerprint in the approval scope;
5. when a sandbox profile is enabled, require the matching `ShellSandbox` and
   obtain an argv-safe `exec_launch`; and
6. spawn through the selected `TerminalPlatform`.

There is no shell-string fallback. Approval denial, missing/mismatched sandbox
enforcement, workspace escape, unsupported platforms, or adapter failure all
fail before returning a session.

POSIX uses a native PTY, makes the leader a new session/process-group owner, and
targets the complete group for interrupt, terminate and kill. Windows projects
the existing ConPTY owner onto the shared port. Production ConPTY creation now
creates a kill-on-close Job and supplies both
`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` and
`PROC_THREAD_ATTRIBUTE_JOB_LIST` in the same `CreateProcessW` attribute list,
so no hosted instruction can run before process-tree ownership applies.
ConPTY output remains continuously drained while the Job, process, pipes and
pseudoconsole have one explicit close path. Interrupt is virtual-terminal
Ctrl+C input; terminate and kill target the complete Job.

Native spawn and platform operations run off the event-loop thread. If creation
is cancelled after the OS call begins, the runtime waits for that bounded call
to resolve and closes any returned owner before propagating cancellation.
Manager shutdown marks the registry closed, waits for pending creation cleanup,
and closes every registered session. No unreferenced fire-and-forget owner is
allowed.

## Consequences

- ACP and future interfaces can depend on one bounded lifecycle contract without
  importing POSIX, Win32, Textual, or test-only code.
- Output truncation is explicit and resumable by cursor, but this is not a
  durable full-terminal transcript service.
- Protected environment values do not reach the child or approval UI. Other
  command arguments remain visible because users must be able to review the
  process they are authorizing.
- Linux tests execute real PTY input, resize, signal and non-zero exit behavior.
  Portable fake/ctypes contracts cover Windows callbacks, Job ownership,
  attributes and cleanup. PR #6
  [CI run 29680149723](https://github.com/amber1686855908-hash/neuro-code/actions/runs/29680149723)
  supplied successful native Windows full-suite and ConPTY smoke evidence.
- This slice deliberately does not publish an ACP method. ACP stdio/WebSocket
  framing, session authorization and protocol-level backpressure remain the
  next M4 capability.
