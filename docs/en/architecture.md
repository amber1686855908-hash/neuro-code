# Neuro Code architecture

[简体中文](../zh-CN/architecture.md) · **English**

## Intent

Neuro Code is a modular monolith. It preserves externally observable Grok
Build behavior where useful, but it does not mirror the upstream Cargo crate
graph. All interactive surfaces consume one typed runtime event stream.

## System boundary

Neuro Code owns local orchestration: CLI/TUI, agent turns, model adapters,
tools, permissions, workspaces, sessions, extensions, and protocol endpoints.
It does not own model hosting, training, proprietary cloud relays, Computer Hub
services, or web dashboard backends. Cloud-only capabilities enter through
explicit adapters and must report unavailable rather than simulate success.

## Dependency direction

```text
interfaces (CLI, TUI, ACP, WebSocket)
                    |
application (agent loop, sessions, commands, tasks)
                    |
domain (messages, events, tools, permissions, errors)
                    |
ports (model, storage, tool, workspace, sandbox, hooks)
                    |
adapters (providers, SQLite, MCP, Git, PTY, OS, HTTP)
```

Dependencies point downward. Domain and application modules must not import a
UI framework, provider SDK, database driver, or platform implementation.
Adapters implement typed ports and are selected only at the composition root.

## Runtime event model

One agent turn is an append-only stream of typed events:

1. user message accepted;
2. model text/reasoning deltas;
3. zero or more tool-call requests;
4. permission decision and tool lifecycle events;
5. tool results appended to model context;
6. another model step or terminal completion;
7. events committed to session storage.

The runtime owns step limits, cancellation, retries, and event ordering. A UI
may render events but may not mutate runtime state directly. Background tasks
must be owned by an `asyncio.TaskGroup` or an explicit registry with a shutdown
contract; unreferenced fire-and-forget tasks are prohibited.

## Stable ports

- `ModelProvider`: turns normalized messages and tool schemas into model events.
- `Tool`: publishes a JSON schema and executes with a scoped `ToolContext`.
- `ToolRegistry`: resolves canonical tool names and rejects duplicates.
- `PermissionManager`: returns allow, deny, or ask before any side effect.
- `SessionStore`: appends versioned events and reconstructs a session.
- `PlatformAdapter`: encapsulates PTY, process, signal, path, clipboard, and sandbox differences.

Protocol models are versioned at external boundaries. Internal state prefers
frozen dataclasses and enums. Unstructured dictionaries must not cross module
boundaries except as validated JSON payloads.

Provider adapters normalize text, reasoning, tool calls, completion reasons,
and token usage. Provider-only state that must survive a tool round trip is
stored in the optional `ToolCall.metadata` mapping under namespaced keys and is
persisted with the message; application code treats it as opaque.

## Safety invariants

- Deny rules override allow rules and bypass modes.
- Headless execution converts an unresolved `ask` into denial.
- Writes resolve and validate their target before mutation; a workspace-scoped
  tool cannot escape through `..` or symlinks.
- Explicit sandbox requests fail closed when the platform cannot enforce them.
- Secrets never appear in inspect output, logs, session events, or exceptions.
- Cancellation terminates child processes and commits a terminal event.
- Shell commands execute in an owned process group. Timeout and cancellation
  attempt graceful tree termination first, then force termination after a
  bounded grace period; output is drained with a fixed in-memory limit.
- Restrictive Bash rules inspect every safely decomposable command segment,
  including common wrappers and nested `bash -c` scripts. Unclassifiable
  scripts fail closed when a deny/ask policy could apply.
- Legacy Grok state is imported read-only and never modified in place.

## Persistence

SQLite is the canonical transactional store for sessions and their ordered
events. JSON and Markdown are interchange/export formats. The database exposes
an integer schema version; every change requires forward migration, fixture
coverage, and a documented compatibility decision. Rust sessions are parsed by
a separate import adapter and converted into the canonical model.

## Platform policy

Linux, macOS, and Windows are first-class CI targets. Platform-specific code is
isolated behind adapters. A small native helper or system facility is allowed
for kernel sandboxing and process containment, but business and orchestration
logic remains Python. Unsupported security guarantees must be reported at
startup, never silently weakened.
