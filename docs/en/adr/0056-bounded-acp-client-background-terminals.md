# ADR 0056 — Bounded ACP client background terminals

[简体中文](../../zh-CN/adr/0056-bounded-acp-client-background-terminals.md) · **English**

## Status

Accepted

## Context

The capability-gated ACP client terminal port initially exposed only a
foreground `terminal_exec` call. The standard ACP terminal methods already
provide a safe lifecycle for a direct executable — create, output,
wait-for-exit, kill, and release — but they still do not define terminal input,
resize, cursor reads, or shell selection. A private PTY extension would make
the protocol claim misleading and would bypass the SDK's portable contract.

## Decision

For a client advertising `terminal: true` and an `off` sandbox binding, the
session-bound `ClientTerminal` port now also owns bounded background direct
executables. The tool surface is:

- `terminal_start` for an executable plus separate arguments;
- `terminal_output` for a bounded status/output snapshot;
- `terminal_wait` for bounded `wait_any`/`wait_all`; and
- `terminal_kill` for termination.

The interface creates opaque Neuro Code task IDs rather than exposing client
terminal IDs. It accepts at most eight running and 32 retained tasks per ACP
session, retains at most the client-provided output limit (never above 1 MiB),
and forwards neither configured environment variables nor credentials. A
watcher maps an exit to a bounded snapshot, requests kill for timeout, and
releases the remote terminal after collecting its final available output.
Session close, delete, disconnect, and shutdown kill and release every running
client terminal. Malformed or failing client responses fail closed without raw
client details. `terminal_start` and `terminal_kill` stay side-effecting, so
the ordinary local policy and ACP approval path still apply.

This is a separate client-terminal lifecycle. It does not change local `bash`
or its managed background tasks, does not provide a shell command proxy, and
does not add automatic completion reminders for client tasks.

## Consequences

A capable client can run and inspect direct background commands without
leaking a reusable client terminal handle or allowing orphaned work after the
ACP session ends. The documented interface remains honest: interactive input,
resize, PTY framing/backpressure, and general terminal protocol extensions are
still unsupported until ACP standardizes them or a separately negotiated
extension is designed.
