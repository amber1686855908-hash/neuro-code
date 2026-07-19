# ADR 0032: validate native Windows terminal lifecycle through ConPTY

[简体中文](../../zh-CN/adr/0032-native-windows-conpty-lifecycle-evidence.md) · **English**

- Status: accepted
- Date: 2026-07-19
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

Headless Textual tests prove application composition and shutdown, but they do
not create a Windows console session or exercise virtual-terminal input,
resizing, and teardown. The Linux/macOS standard-library PTY smoke test provides
process-boundary evidence on POSIX, yet it cannot establish that the production
CLI restores the alternate screen, cursor, focus tracking, and any available
parent console modes when hosted by Windows ConPTY.

ConPTY uses synchronous input and output channels. Microsoft documents that
output must remain drained on a separate thread during shutdown because
`ClosePseudoConsole` may emit a final frame and can block indefinitely on older
Windows versions when the output channel is not serviced. Process creation also
requires a `STARTUPINFOEX` attribute list containing
`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`.

## Decision

A private `windows_conpty` platform adapter uses only the Python standard
library. It lazily loads the Windows 10 version 1809-or-newer APIs from
`kernel32.dll`, creates two synchronous pipes, creates the pseudoconsole, and
starts the hosted process with a mutable command line, a sorted Unicode
environment block, `EXTENDED_STARTUPINFO_PRESENT`, and the pseudoconsole process
attribute. No Windows-specific runtime package is added.

The adapter owns the pseudoconsole, host pipe ends, process handle, and one
dedicated output-drain thread. Input writes handle partial progress; resize,
wait, non-zero exit codes, explicit termination, and close are observable
operations. Captured output keeps a bounded head and tail while the reader
continues draining all bytes. Every creation stage has cleanup tests. Normal
close first stops a live hosted process, closes host input, keeps the output
reader active across `ClosePseudoConsole`, then closes the output and process
handles. Cleanup is idempotent and reports the first failure only after
attempting all remaining releases.

This adapter first supported executable-boundary validation and now implements
the Windows side of the shared interactive-terminal platform port defined by
[ADR 0034](0034-bounded-owned-interactive-terminal-sessions.md). It is still
not exposed as an ACP method or tool. Portable fake-API tests verify pipe,
handle and Job ownership, partial writes, resize, wait/terminate, bounded
capture, callbacks, all major failure stages, and the exact pseudoconsole/Job
`STARTUPINFOEX` attributes and Unicode-environment contract.

The opt-in `terminal-smoke` CI matrix now includes Windows. Its native tests:

- start the real offline production CLI inside ConPTY;
- wait for application-mode entry, resize the pseudoconsole, send a real
  `Ctrl+C`, verify the idle TUI stays alive, then send a real `Ctrl+Q`;
- require a zero production exit code, paired and ordered alternate-screen,
  cursor, and focus-tracking teardown, unchanged parent console modes where
  console handles are available, bounded output, and no fixture credential;
- run a separate console probe that observes both initial and resized
  dimensions and preserves exit code 7.

The POSIX native test was tightened at the same time: only the headless test
uses Textual's automatic key hook. Linux/macOS now write the actual `Ctrl+Q`
byte through the PTY after observing application-mode entry.

## Consequences

- The three target platform families now have native process-boundary terminal
  lifecycle coverage in the repository instead of inferring Windows behavior
  from a headless driver.
- ConPTY output remains drained during teardown, avoiding a known older-Windows
  deadlock class, and all owned output is bounded even when a child is noisy.
- ADR 0034 adds the typed port, bounded cursor ring, permissions, workspace,
  sandbox integration and application ownership. Neuro Code still does not
  claim ACP interactive PTY protocol exposure.
- Ordinary Windows `ProcessTree` creation now uses
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` through the separate boundary in
  [ADR 0033](0033-atomic-windows-job-process-creation.md). Production ConPTY
  now combines the pseudoconsole and Job-list attributes in one creation call,
  while remaining separate from the owner of non-PTY shell streams.
- Native Windows results require a Windows runner. Linux development executes
  the portable API and structure contracts and reports the native tests as
  platform skips; CI supplies the Windows execution evidence.

The Win32 lifecycle follows Microsoft's
[pseudoconsole session guidance](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
and
[ClosePseudoConsole requirements](https://learn.microsoft.com/en-us/windows/console/closepseudoconsole).
Historical behavior evidence comes from the read-only pinned baseline at
`crates/codegen/xai-grok-pager-pty-harness/src/pty.rs`,
`crates/codegen/ptyctl/src/pty.rs`, and
`crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs`.
