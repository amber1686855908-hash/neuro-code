# ADR 0053: Capability-gated ACP client terminal execution

[简体中文](../../zh-CN/adr/0053-capability-gated-acp-client-terminal.md) · **English**

- Status: accepted
- Date: 2026-07-29

## Context

ACP defines client-owned `terminal/create`, `terminal/output`,
`terminal/wait_for_exit`, `terminal/kill`, and `terminal/release` requests.
The existing `bash` tool deliberately has local shell semantics, owned process
trees, background-task lifecycle support, and optional OS sandbox enforcement.
Replacing it with a client call would make shell choice platform-dependent and
could silently bypass an explicit sandbox.

At this ADR's acceptance, the standard ACP terminal surface was also narrower
than an interactive PTY: it had no portable shell-selection contract, terminal
input, resize, cursor reads, or a background-task lifecycle in the Neuro Code
adapter.

## Decision

- Add the canonical session-scoped `ClientTerminal` application port for one
  foreground executable, argument vector, bounded output, and terminal exit
  status. Application code does not import ACP SDK types.
- Bind that port only when the connected client explicitly advertises
  `terminal: true`, for new, load, resume, and forked sessions. Bootstrap passes
  it through the composition root.
- Register `terminal_exec` only for an `off` sandbox binding with that port. It
  accepts an executable plus separate arguments, not a shell command. Existing
  `bash` behavior and managed background tasks remain local and unchanged; there
  is no transparent fallback between the two tools.
- Normal side-effecting permission, workspace, event, and output-redaction
  paths remain in force. No configured Neuro Code environment variables or
  credentials are forwarded to the client terminal.
- A run performs create, wait, output, and release. Output is limited to 1 MiB;
  malformed or failed client responses become stable fail-closed `ToolError`
  messages without raw client details. Timeout or cancellation requests kill,
  and every opened terminal is released best-effort.
- Do not expose client terminal input, resize, interactive framing, or a general
  ACP request proxy in this foreground slice. ADR 0056 separately adds the
  bounded standard background direct-executable lifecycle; it does not add a
  shell proxy or interactive terminal semantics.

## Consequences

- A capable, unsandboxed ACP client can execute direct foreground commands in
  its own workspace process while users retain the ordinary approval flow.
- An explicit sandbox never gains an unconfined alternative execution path:
  `terminal_exec` is absent and direct calls fail closed.
- The result remains intentionally smaller than a cross-platform shell or
  interactive-terminal API. Interactive input, resize, PTY framing/backpressure,
  and a general terminal protocol proxy remain outside this decision and require
  separate capability and lifecycle design.
