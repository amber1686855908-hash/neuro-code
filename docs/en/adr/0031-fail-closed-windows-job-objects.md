# ADR 0031: fail closed with Windows Job Object process ownership

[简体中文](../../zh-CN/adr/0031-fail-closed-windows-job-objects.md) · **English**

- Status: accepted
- Date: 2026-07-19
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

`CREATE_NEW_PROCESS_GROUP` gives a Windows child a control-signal boundary, but
it does not provide a stable ownership handle for every descendant. Rediscovering
the tree later through `taskkill /T /F` depends on a recyclable PID, and waiting
only for the direct shell leader can report completion while a descendant is
still running. Those semantics are weaker than the POSIX process-group boundary
already used by foreground and managed-background commands.

The pinned Rust baseline creates an anonymous Job Object, enables
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, assigns each spawned leader, and uses the
Job handle for whole-tree termination. The Python slice needs the same user
capability without importing a Windows package into domain or application code.

## Decision

A private `windows_job` platform adapter uses `ctypes` and lazily loads
`kernel32.dll` only on Windows. It creates and configures an anonymous,
non-inherited Job Object before starting the subprocess. `ProcessTree` borrows
that handle for the atomic extended-creation boundary defined by
[ADR 0033](0033-atomic-windows-job-process-creation.md), so the leader belongs
to the Job before any of its code can run. `ProcessTree` strongly owns the Job
handle for as long as it owns the subprocess tree.

Job creation, limit configuration, atomic process assignment, accounting
queries, termination, and handle closure all have explicit error paths. A
creation failure closes the configured Job, while a post-creation stream or
handle failure terminates and reaps the direct child; kill-on-close remains the
failure-closed backstop. Neuro Code does not silently return to `taskkill` and
does not request `CREATE_BREAKAWAY_FROM_JOB` to escape host containment. If a
host Job hierarchy rejects nested assignment, command creation fails visibly.

Natural waiting first preserves the direct leader's exit code, then polls the
Job's `ActiveProcesses` count until every assigned descendant has exited. On
Windows, explicit termination calls `TerminateJobObject` immediately; the
cross-platform grace parameter does not promise a POSIX-style soft phase there.
Termination and close are idempotent, concurrent termination is serialized, and
the Job handle is closed exactly once. Foreground Bash, managed-background
tasks, binding replacement, and application shutdown all reuse this one
`ProcessTree` boundary.

Portable fake-API tests cover the Win32 access mask, limits, accounting, error
cleanup, atomic attribute values, restricted inheritance, and handle lifetime on
every development platform. Windows CI creates real descendants and proves that
a leader may exit while `wait()` remains pending and that termination prevents
a descendant from outliving its tree.

## Consequences

- Windows commands now have stable handle-based ownership before the leader
  begins executing; leader PID reuse is not part of teardown.
- A Job Object may be nested inside host containment on supported Windows
  versions. An incompatible host policy is reported instead of weakening the
  guarantee.
- The prior stdlib asyncio `spawn`-to-attachment window is closed by
  [ADR 0033](0033-atomic-windows-job-process-creation.md), without an asyncio
  private transport, `CREATE_SUSPENDED` repair sequence, or breakaway flag.
- Native Windows ConPTY lifecycle evidence is delivered independently by
  [ADR 0032](0032-native-windows-conpty-lifecycle-evidence.md); Job ownership
  itself still does not imply terminal-mode parity.

Source evidence is the Job Object `ProcessGroup` implementation in
`crates/codegen/xai-tty-utils/src/lib.rs` and the Windows local-terminal process
ownership paths at the pinned commit.
