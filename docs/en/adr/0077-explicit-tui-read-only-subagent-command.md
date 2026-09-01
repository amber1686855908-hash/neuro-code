# ADR 0077: Explicit TUI read-only subagent command

## Status

Accepted for Stage5CW.

## Context

The CLI and ACP now expose explicit bounded read-only subagent entry points
through the same application service. The TUI needs a small user-facing entry
without creating a second execution path or turning the interface into an
automatic delegator.

## Decision

Add `/subagent PROMPT` to the TUI slash-command surface. The command requires
an existing current session and refuses to start while another turn is
running. It invokes the composition-owned
`ReadOnlySubagentApplicationService` with its default bounded step limit,
displays a short status line, and renders only the returned safe response.

The operation runs in a cancellable TUI worker. It does not stream child
events, append child messages or temporary context to the parent session, or
display internal parent/task/child IDs. Missing services, invalid session
state, failures and cancellation remain visible as bounded UI status/error
messages.

## Consequences

CLI and ACP remain the other explicit entry points and all three share the
same application isolation and redaction boundary. This TUI subagent command
still performs no automatic delegation, retry, recursion, parallel child
execution, write-capable tool use, or parent-context reuse. A separate bounded
Automatic Ultracode entry is implemented by ADR 0141. A future TUI command may
add an explicit step option only after a stable user need is established.
