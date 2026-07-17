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
- `SessionStore`: appends versioned events, preserves ordered `SessionItem`
  values, and exposes an ordinary-message projection to the agent runtime.
- `PlatformAdapter`: encapsulates PTY, process, signal, path, clipboard, and sandbox differences.

Protocol models are versioned at external boundaries. Internal state prefers
frozen dataclasses and enums. Unstructured dictionaries must not cross module
boundaries except as validated JSON payloads.

Provider adapters normalize text, reasoning, tool calls, completion reasons,
and token usage. Provider-only state that must survive a tool round trip is
stored in the optional `ToolCall.metadata` mapping under namespaced keys and is
persisted with the message; application code treats it as opaque. Streamed
assistant reasoning that is part of a provider's tool-call continuity contract
is stored separately in optional, assistant-only `Message.reasoning_content`.
The OpenAI-compatible adapter replays that field only when the same assistant
message contains tool calls; no-tool reasoning is never echoed to the provider.

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
a separate read-only adapter. It validates format versions 0 and 1, reads
bounded JSONL records, converts supported legacy/current records into an
ordered `SessionSnapshot`, and reports corrupt or unsupported records instead
of silently inventing content. The SQLite adapter inserts that snapshot in one
transaction and preserves its ID, workspace, model, and timestamps; an existing
ID fails without mutation. Source session files are never opened for writing.

The canonical sequence is a union of ordinary `Message` values and opaque but
validated `PreservedContextItem` values. Message content parts retain text/image
ordering and image URLs. Reasoning and backend-tool payloads retain their
provider JSON and relative order. The agent runtime consumes only the message
projection; when it resumes an imported session, storage permits append-only
extension but rejects rewriting the preserved prefix. JSON export schema 2
includes both projections. Provider adapters validate image references and use
native multimodal blocks only where the wire role and URI form are supported;
all other images become a visible placeholder without adapter-side media I/O.
Preserved context items are not yet sent. See
[ADR 0004](adr/0004-ordered-session-items.md) and
[ADR 0005](adr/0005-provider-native-image-replay.md). Newly generated
thinking-mode tool turns use the typed message path instead; see
[ADR 0006](adr/0006-thinking-tool-continuity.md).

The Rust boundary also performs a bounded, in-memory upgrade for legacy
assistant records. Context-bearing entries in `raw_output`, singular
`reasoning`, and v0 `reasoning_content` are lifted immediately before their
assistant. A stream-scoped set of standalone backend-tool IDs suppresses only
duplicate embedded copies; reasoning items remain ordered and are never
collapsed. Malformed and unknown embedded entries are counted separately
without rejecting an otherwise valid assistant row.

## Platform policy

Linux, macOS, and Windows are first-class CI targets. Platform-specific code is
isolated behind adapters. A small native helper or system facility is allowed
for kernel sandboxing and process containment, but business and orchestration
logic remains Python. Unsupported security guarantees must be reported at
startup, never silently weakened.
