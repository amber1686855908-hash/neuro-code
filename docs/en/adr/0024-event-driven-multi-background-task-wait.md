# ADR 0024: wait for multiple background tasks through completion events

[简体中文](../../zh-CN/adr/0024-event-driven-multi-background-task-wait.md) · **English**

- Status: accepted
- Date: 2026-07-18
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

The first managed-background-command slice exposed `task_output`, which can
briefly wait for one task. A model coordinating parallel tests or builds would
otherwise need repeated sequential calls or sleep-based polling. The fixed Rust
baseline accepts up to 20 IDs, supports `wait_any` and `wait_all`, and registers
completion waiters instead of polling task state.

A multi-wait must retain the existing conversation boundary. It must also tear
down every helper waiter when the tool returns, times out, or is cancelled;
leaving a detached waiter could consume a later completion or keep a turn-owned
task alive invisibly.

## Decision

`BackgroundTaskManager.wait` accepts unique normalized IDs, a
`BackgroundTaskWaitMode`, and a finite timeout. It returns a frozen
`BackgroundTaskWaitResult` containing known snapshots, IDs absent from the
current scope, and whether the requested completion condition timed out.

`LocalBackgroundTaskManager` waits on each task record's existing
`asyncio.Event`. `wait_any` returns when at least one known task is terminal;
`wait_all` returns when every known task is terminal. An already satisfied
condition returns immediately. Unknown and cross-scope IDs are reported as
`not_found` and never reveal another scope's task metadata. On every exit path,
unfinished helper waiters are cancelled and joined.

The model-facing `wait_tasks` tool:

- accepts one to 20 IDs, trims them, and de-duplicates them in first-seen order;
- requires `wait_any` or `wait_all`;
- uses a 30-second default and maximum `timeout_seconds` budget, with zero
  retaining the legacy default-wait meaning;
- returns snapshots and bounded output for known tasks plus per-ID `not_found`
  results;
- bounds the combined text with `ToolContext.output_byte_limit`, while metadata
  omits captured output; and
- acknowledges every terminal snapshot it returns, just like terminal
  `task_output`, so a later completion reminder cannot duplicate it.

Waiting and acknowledgement are observational lifecycle bookkeeping, so the
tool is read-only for permission purposes. Starting and terminating processes
remain side-effecting operations.

## Consequences

- Parallel work can be coordinated without serial polling delays.
- Timeouts return partial state rather than cancelling the underlying tasks.
- Cancellation cannot leave zombie waiters or suppress a completion the model
  has not seen.
- Scope isolation and output limits apply to the whole multi-task surface.
- The Python API uses seconds consistently with `task_output`; the historical
  Rust surface uses milliseconds.
- Subagents are not yet in this task namespace. Auto-wake, persistent full
  output files, and cross-process task recovery remain separate slices.

Source evidence is the historical `WaitTasksTool`, `WaitMode`,
`MAX_MULTI_WAIT_IDS`, `wait_any_event_driven`, and `wait_all_event_driven`
behavior at the pinned commit.
