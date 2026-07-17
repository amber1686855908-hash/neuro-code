# Neuro Code architecture

[简体中文](../zh-CN/architecture.md) · **English**

## Intent

Neuro Code is a modular monolith. It preserves useful external behavior, but it
does not mirror the historical upstream Cargo crate graph. All interactive
surfaces consume one typed runtime event stream.

All public project-owned identifiers follow the Neuro Code namespace defined
by [ADR 0013](adr/0013-neuro-code-namespace.md).

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
2. optional provider-attempt failure/selection events, followed by model
   text/reasoning deltas;
3. zero or more provider-hosted tool lifecycle events and/or local tool-call
   requests;
4. permission decision, optional asynchronous approval, and local tool
   lifecycle events;
5. local tool results appended to model context;
6. another model step or terminal completion/failure;
7. events and the recoverable ordered context committed to session storage.

The runtime owns step limits, cancellation, retries, and event ordering. A UI
may render events but may not mutate runtime state directly. Background tasks
must be owned by an `asyncio.TaskGroup` or an explicit registry with a shutdown
contract; unreferenced fire-and-forget tasks are prohibited.

## Conversation and interactive interface

`AgentConversation` is the reusable application boundary above one-turn
`AgentRuntime`. It serializes turns and carries the ordered session items,
session identifier, and provider-origin metadata forward after each durable
commit. Opening an existing conversation validates that its recorded workspace
is the same filesystem location as the requested workspace. The headless CLI
and Textual interface compose the same controller, so resume and provider replay
rules cannot diverge by interface.

On failure or cancellation, `AgentConversation` reloads the canonical ordered
items and provider origin from `SessionStore` before releasing its turn lock.
The next prompt therefore reuses durable state instead of a stale in-memory
prefix. A cancelled user message remains part of that history; pre-token rewind
is a separate, unimplemented interaction policy.

The minimal TUI is a presentation adapter over `AgentEvent`. It owns prompt
input, scrollback, a live text surface, and local slash commands. It reduces
provider and tool lifecycle events to status messages and deliberately does not
render raw reasoning, general tool argument mappings, or tool results in the
transcript. See [ADR 0014](adr/0014-minimal-event-stream-tui.md).

The TUI keeps its prompt available while a worker-owned turn runs. `Ctrl+C` and
local `/cancel` cancel that worker; an approval modal gives `Ctrl+C` the narrower
meaning of denying the pending request. Runtime-owned recovery and tool-result
balancing are defined in
[ADR 0016](adr/0016-recoverable-turn-cancellation.md).

`ProfileConversationController` wraps the active `AgentConversation` for the
interactive composition. It serializes selection with turns and exposes only
redacted `ProviderOption` data to the TUI. Selecting a different configured
profile composes a new provider/runtime/conversation binding with no resumed
session; the old SQLite session remains untouched. This strict boundary avoids
cross-provider replay of encrypted reasoning, hosted-tool state, dialect
metadata, and profile-affine context. See
[ADR 0017](adr/0017-safe-interactive-profile-selection.md).

Permission policy and user interaction are separate boundaries.
`PermissionManager` first returns a deterministic decision. An `ask` may then
flow through the optional asynchronous `PermissionApprover` port; the runtime
emits request/resolution audit events and cannot emit `tool_started` before an
allowed response. The TUI's session broker remembers only hashes of exact
tool/argument pairs in memory, and every later call is re-evaluated by policy so
deny precedence remains intact. Headless composition provides no approver and
continues to fail closed. See
[ADR 0015](adr/0015-async-interactive-tool-approval.md).

## Stable ports

- `ModelProvider`: turns an ordered `ModelContext` and tool schemas into model
  events. It exposes the selected profile identity and a non-secret affinity
  fingerprint; context carries the session's profile/model/affinity origin for
  adapter-owned replay decisions.
- `Tool`: publishes a JSON schema and executes with a scoped `ToolContext`.
- `ToolRegistry`: resolves canonical tool names and rejects duplicates.
- `PermissionManager`: returns allow, deny, or ask before any side effect.
- `PermissionApprover`: optionally resolves an `ask` asynchronously without
  overriding policy denial.
- `SessionStore`: appends versioned events, preserves ordered `SessionItem`
  values, and exposes both the canonical sequence and an ordinary-message
  projection.
- `PlatformAdapter`: encapsulates PTY, process, signal, path, clipboard, and sandbox differences.

Protocol models are versioned at external boundaries. Internal state prefers
frozen dataclasses and enums. Unstructured dictionaries must not cross module
boundaries except as validated JSON payloads.

## Provider profiles and compatibility gateways

The composition root selects a named `ProviderProfile`; the agent runtime never
branches on a commercial provider name. Profiles separate wire protocol
(`openai-chat`, `openai-responses`, `anthropic-messages`, or
`gemini-generate-content`) from optional dialect behavior such as xAI Responses.
Credentials are environment references or a validated loopback-proxy
placeholder, never persisted secrets.

CC Switch is an optional configuration source and HTTP gateway, not an
application dependency. Its exported active profile is translated in memory at
the configuration boundary. Project configuration overrides it, and no CC
Switch database or process-control API crosses into the domain/application
layers. See [ADR 0010](adr/0010-provider-profiles-and-cc-switch.md).

An optional routing wrapper owns an ordered, lazily constructed provider
candidate chain. The first emitted provider event is the commit point: a
configuration or provider failure before that point may advance to the next
candidate, while any later failure is terminal for the model step. Successful
selection is monotonic for the rest of the process run. Attempt failures and
selections remain explicit runtime events rather than being hidden in logs.
This behavior is independent of whether a candidate reaches a direct endpoint
or a CC Switch gateway. See
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md).

Each profile also resolves one `HttpClientPolicy` at adapter construction.
Environment mode delegates standard proxy/certificate variables to HTTPX,
direct mode disables HTTPX environment trust, and explicit mode reads one proxy
URL from a named environment variable. The resolved policy supplies identical
client options and error redaction to every provider adapter. Proxy URLs never
cross into domain events, inspection output, or persisted configuration. See
[ADR 0012](adr/0012-provider-http-proxy-policy.md).

Provider adapters normalize text, reasoning, tool calls, completion reasons,
and token usage. Provider-only state that must survive a tool round trip is
stored in the optional `ToolCall.metadata` mapping under namespaced keys and is
persisted with the message; application code treats it as opaque. Streamed
assistant reasoning that is part of a provider's tool-call continuity contract
is stored separately in optional, assistant-only `Message.reasoning_content`.
For newly generated turns, the OpenAI-compatible adapter replays that field
only when the same assistant message contains tool calls; completed no-tool
reasoning is not echoed. Provider-affine imported visible reasoning follows the
separate ordered projection in ADR 0007.

A terminal `ModelCompleted` event may also carry provider-native preserved
items and canonical response text. The runtime inserts those items before the
assistant message, uses terminal text as the persisted/model-facing truth, and
keeps streamed deltas as UI events. It then commits the complete `SessionItem`
sequence, not merely its message projection. This separates responsive
rendering from byte-stable context replay.

Provider-hosted tools and local tools have deliberately separate event kinds.
`backend_tool_started` and `backend_tool_completed` report work already owned
and executed by a provider; the application never routes them through
`PermissionManager`, `ToolRegistry`, or local result-message synthesis. Local
`tool_requested` through `tool_completed`/`tool_failed` events retain the
existing permission and workspace boundary. The xAI Responses adapter
deduplicates streamed lifecycle notifications and synthesizes a start/complete
pair from terminal backend output when intermediate events were absent.

## Safety invariants

- Deny rules override allow rules and bypass modes.
- Headless execution converts an unresolved `ask` into denial.
- A side-effecting tool cannot start while approval is pending or after denial
  or cancellation. Session approvals cover only an identical tool/argument
  digest, remain memory-only, and are subordinate to a fresh policy decision.
- Every local tool call persisted in an assistant message has exactly one tool
  result before the context is reused. Cancellation records an error result for
  the active call and every remaining call in the same model batch.
- Writes resolve and validate their target before mutation; a workspace-scoped
  tool cannot escape through `..` or symlinks.
- Explicit sandbox requests fail closed when the platform cannot enforce them.
- Secrets never appear in inspect output, logs, session events, or exceptions.
- API and proxy credentials are environment references; resolved proxy URLs
  remain adapter-local and are removed from network errors.
- Provider failover may occur only before the candidate's first model event;
  after that boundary, errors propagate without replaying the step elsewhere.
- Interactive profile switching is serialized between turns and starts a fresh
  conversation. It never relabels or replays the previous session under the new
  profile.
- Cancellation terminates owned child processes, commits a terminal failure,
  saves balanced context, and reloads it before the next conversation turn.
- Shell commands execute in an owned process group. Timeout and cancellation
  attempt graceful tree termination first, then force termination after a
  bounded grace period; output is drained with a fixed in-memory limit.
- Restrictive Bash rules inspect every safely decomposable command segment,
  including common wrappers and nested `bash -c` scripts. Unclassifiable
  scripts fail closed when a deny/ask policy could apply.
- Legacy upstream state is imported read-only and never modified in place.

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
ID fails without mutation. Resume authorization compares the recorded and
requested workspaces by filesystem identity, with canonical normalized paths as
a fallback, so platform aliases are accepted without admitting a different
workspace. Source session files are never opened for writing.

The canonical sequence is a union of ordinary `Message` values and opaque but
validated `PreservedContextItem` values. Message content parts retain text/image
ordering and image URLs. Reasoning and backend-tool payloads retain their
provider JSON and relative order. The runtime carries the complete ordered
sequence into each model step while application views continue to use the
ordinary-message projection. When it resumes an imported session, storage
permits append-only extension but rejects rewriting the preserved prefix. JSON
export schema 2 includes both projections. Provider adapters validate image
references and use native multimodal blocks only where the wire role and URI
form are supported; all other images become a visible placeholder without
adapter-side media I/O. Preserved context follows a fail-closed affinity policy.
The official xAI HTTPS Chat Completions endpoint may receive visible reasoning
and backend-tool summaries from a trusted upstream Rust import, while opaque
encrypted content and every non-affine target are filtered. The generic
Responses adapter uses `store: false`; its optional xAI dialect requests
encrypted reasoning and supports hosted tools. Opaque output is replayed only
for an exact stored profile-affinity match. Legacy Rust imports without a
fingerprint retain the stricter official xAI HTTPS/source-marker fallback. Output-only
reasoning status is stripped before replay. See
[ADR 0004](adr/0004-ordered-session-items.md) and
[ADR 0005](adr/0005-provider-native-image-replay.md). Newly generated
thinking-mode tool turns use the typed message path instead; see
[ADR 0006](adr/0006-thinking-tool-continuity.md). Imported-context affinity is
defined by [ADR 0007](adr/0007-provider-affine-context-replay.md); native
Responses replay is defined by
[ADR 0008](adr/0008-xai-responses-native-replay.md). Hosted xAI tool
configuration and lifecycle ownership are defined by
[ADR 0009](adr/0009-xai-hosted-tools.md); the generalized profile decision is
[ADR 0010](adr/0010-provider-profiles-and-cc-switch.md), and safe pre-output
failover is defined by
[ADR 0011](adr/0011-safe-pre-output-provider-failover.md). Provider HTTP
transport selection is defined by
[ADR 0012](adr/0012-provider-http-proxy-policy.md).

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
