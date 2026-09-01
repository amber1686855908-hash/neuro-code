# ADR 0147: ACP Client I/O Adapter Boundary

- Status: Accepted
- Date: 2026-08-30
- Scope: third structural slice of V1 Interface Boundary Consolidation
- Depends on: ADR 0052, ADR 0053, ADR 0056, ADR 0145, and ADR 0146

## Context

The frozen PR #74 head is
`18a686222190f5251e269bb68e1ebfeb7744cede`. The top-level
`neuro_code.acp` adapter still contains several unrelated responsibilities.
The next cohesive boundary is the ACP client-side filesystem and terminal
adaptation that implements the existing application ports
`ClientFileSystem` and `ClientTerminal`.

This is a structural extraction. It must preserve the existing ACP wire
behavior, capability gates, session binding, private compatibility names,
bounds, cancellation behavior, background-task ownership, and cleanup. It
does not redesign either application port or ACP capability negotiation.

## Pre-change audit

The audit was performed against the exact PR #74 head before moving code.

### Filesystem adaptation

The filesystem-specific symbol was `_AcpClientFileSystem`. Its state was the
ACP SDK `Client`, the bound external ACP `session_id`, and two negotiated
booleans: `supports_read` and `supports_write`.

`read_text_file` first failed closed when read capability was absent, forwarded
the bound session ID, path, optional line, and optional limit to
`fs/read_text_file`, bounded the UTF-8 response to 1 MiB, propagated
`CancelledError`, and converted other client failures to the existing stable
`ToolError`. `write_text_file` applied the same 1 MiB UTF-8 bound before
calling `fs/write_text_file`, preserved cancellation and `ToolError`, and
converted other failures to the existing stable error.

### Terminal adaptation

The terminal-specific symbols were:

- `_AcpClientTerminalTask`;
- `_AcpClientTerminal`;
- `_client_terminal_command`;
- `_client_terminal_cwd`;
- `_client_terminal_limits`;
- `_client_terminal_background_limits`;
- `_client_terminal_wait_seconds`;
- `_client_terminal_id`;
- `_client_terminal_task_id`; and
- `_client_terminal_exit_status`.

The adapter-only constants were `MAX_CLIENT_FILE_BYTES`,
`MAX_CLIENT_TERMINAL_COMMAND_BYTES`, `MAX_CLIENT_TERMINAL_ARGUMENTS`,
`MAX_CLIENT_TERMINAL_ARGUMENT_BYTES`,
`MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES`,
`MAX_CLIENT_TERMINAL_ID_BYTES`, `MAX_CLIENT_TERMINAL_SIGNAL_BYTES`,
`MAX_CLIENT_TERMINAL_TASKS`, and `MAX_CLIENT_TERMINAL_RETAINED_TASKS`.
The shared 1 MiB terminal output bound remains owned by the existing
application terminal port; `MAX_BACKGROUND_TASK_WAIT_IDS` and the background
task result/status values remain domain-owned.

The terminal adapter owned the following state and lifecycle:

- its ACP `Client` and bound session ID;
- the retained task map and its async lock;
- the pending-start counter used in the running-task bound;
- the closed/shutdown flag;
- each task's opaque task ID, ACP terminal ID, command, cwd, output limit,
  timeout, status, output, maximum observed output bytes, truncation flag,
  exit status, finish time, kill and timeout/failure flags;
- each task's completion event, output lock, termination lock, and watcher;
- cancellation-safe terminal creation and cleanup;
- foreground wait, timeout/cancel kill, output retrieval, and release; and
- background watcher, timeout, kill, release, retention, and shutdown behavior.

### Agent call sites and capability gates

`NeuroCodeAcpAgent._client_file_system` continued to own the capability
decision. It returned no adapter when there was no client, no negotiated
filesystem capability, or neither `read_text_file is True` nor
`write_text_file is True`; otherwise it constructed the adapter with the two
booleans. `_client_terminal` continued to return no adapter unless a client
was connected, capabilities had been negotiated, and `terminal is True`.

`new_session`, `_activate_persisted_session`, and `fork_session` constructed
these session-bound adapters and transferred them into the service binding
only after successful publication. Their failure paths still shut down an
untransferred terminal. `_AcpSession` and `_cleanup_session` continued to
own the terminal reference and session cleanup. Capability negotiation in
`initialize` remained in `NeuroCodeAcpAgent`.

### Dependency and behavior audit

Before extraction, `neuro_code.acp` directly contained the adapters and
imported the ACP SDK client/schema, application ports, domain background-task
types, and `ToolError`. The cohesive interface-layer dependency direction is
now:

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.client_io
        -> ACP SDK client/schema
        -> application ClientFileSystem / ClientTerminal ports
        -> domain background-task types
```

The canonical module must not import `neuro_code.acp`, bootstrap, concrete
infrastructure, providers, stores, or workspace implementations. It performs
no capability negotiation, session lookup, permission decision, workspace
validation, sandbox setup, tool registration, or provider call.

Existing behavior was already frozen by `tests/test_acp.py`, including
filesystem capability combinations, forwarded session/path/range values,
1 MiB file bounds, terminal capability gating, foreground create/wait/output/
release, invalid response handling, no-environment forwarding, background
start/get/wait/kill, timeout/cancellation, retention, and shutdown. Downstream
port consumers remain covered by `tests/test_tools.py`.

## Decision

`neuro_code.interfaces.acp.client_io` is the canonical owner of the ACP
client filesystem and terminal adapters, their adapter-only bounds, and their
validation helpers. The implementation is moved structurally without
changing method signatures or control flow.

`neuro_code.acp` remains responsible for capability negotiation, capability
gated construction, session binding/publication, lifecycle ownership, and
cleanup. The application ports remain the seam consumed by tools; no ACP SDK
type is passed into application tool code.

## Preserved filesystem behavior

The canonical filesystem adapter continues to:

- bind every request to the ACP session supplied at construction;
- expose read and write only when the corresponding negotiated capability is
  true;
- preserve the existing path, line, and limit forwarding;
- enforce the existing 1 MiB UTF-8 response/write bounds;
- preserve cancellation propagation and stable fail-closed errors; and
- leave final client-side filesystem semantics with the client.

## Preserved terminal behavior

The canonical terminal adapter continues to:

- accept a direct executable and bounded argument vector, never a shell
  command;
- validate command, arguments, cwd, output, timeout, IDs, and exit status
  with the existing messages and bounds;
- create, wait for, read, and release each foreground terminal;
- kill on foreground timeout, cancellation, wait/output failure, and cleanup;
- forward no configured Neuro Code environment values;
- retain at most eight running and 32 retained background tasks;
- preserve opaque task IDs, ordered wait results, missing IDs, timeout
  semantics, idempotent kill, output accounting, and status transitions; and
- wait for owned watchers and release terminals during task/session shutdown.

Interactive stdin, resize, cursor streaming, PTY framing, and backpressure
remain unsupported.

## State ownership and lifecycle

`client_io` owns only adapter-local state listed in the audit. It does not own
the `_AcpSession`, session registry, binding publication, capability snapshot,
permission broker, workspace, sandbox, or transport. The terminal task watcher
remains the owner of background completion state, and the session cleanup path
continues to invoke the adapter's idempotent shutdown.

## Compatibility

`neuro_code.acp` imports the moved private classes, helpers, and adapter-only
constants directly from `client_io`. These are identity-preserving private
compatibility aliases, not wrappers or duplicate definitions. Existing
private test and integration references therefore retain their behavior while
the classes and helpers report the canonical module as their `__module__`.

The previously imported shared `ClientTerminalResult` and terminal output
bound remain available from `neuro_code.acp` as compatibility imports from
the application port. They are not redefined in the canonical adapter.

## Authority, permission, and sandbox boundary

The extraction does not change permission authority, workspace validation,
sandbox policy, or tool registration. Ordinary side-effect permissions still
gate terminal starts and kills, and an enabled sandbox still prevents client
terminal exposure through the existing capability construction gate.

`permission authority non-migration = PROVEN within this slice`: this means
the extracted module does not acquire permission authority. It is not a claim
that the complete Neuro Code permission subsystem has been globally re-proven
by this ADR.

## Explicit non-goals

This slice does not change client ports, ACP capability negotiation, agent or
server adaptation, session lifecycle, permissions, workspace/sandbox policy,
MCP, transport, provider behavior, task semantics, output bounds, retry,
replay, checkpoint/rollback, automatic delegation, writable subagents,
parallel/dataflow execution, UI behavior, or interactive terminal features.

## Validation

Validation includes canonical-definition and alias identity contracts,
dependency/import contracts, the existing ACP unit/raw-stdio/E2E behavior,
documentation parity, all repository quality gates, and the final pull
request merge-ref CI. Acceptance requires the new merge-ref CI to be fully
green; the structural boundary is not considered frozen from local tests
alone.
