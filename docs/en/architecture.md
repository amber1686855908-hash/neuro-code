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

Managed shell work uses an application `BackgroundTaskSupervisor` and an
isolated `BackgroundTaskManager` registry per conversation binding. `bash` can
return a task ID without waiting; `task_output` reads or briefly waits for a
bounded snapshot, `wait_tasks` waits through completion events for any or all
of at most 20 IDs, and `kill_task` terminates the owned tree through the ordinary
permission boundary. A binding can address only its own task IDs. Replacing a
binding closes its scope; the composition root always closes the supervisor on
exit. Records are memory-only and do not become durable session context. See
[ADR 0021](adr/0021-owned-background-shell-tasks.md) and
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md). See
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md) for multi-wait
conditions, timeout, cancellation, and output bounds.

At each explicit model step, `AgentRuntime` queries that scope for unreported
terminal tasks. It appends a model-only, metadata-only reminder capped at 20
records and acknowledges the batch after a valid provider completion. Terminal
`task_output`, `wait_tasks`, and `kill_task` results acknowledge the same IDs
first, preventing duplicate delivery. The reminder is excluded from
`SessionItem` persistence;
only its bounded audit event is durable. Idle completion waits for user input
and never starts a model turn itself. See
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md).

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

The presentation adapter owns one fixed neutral-dark theme instead of exposing
Textual's unrelated theme and command-palette surfaces. The built-in palette is
disabled, provider and session discovery use the explicit application commands,
and session queries are rendered as literal plain text. In full-screen terminal
mode, a low-frequency viewport reconciliation reads the actual TTY dimensions
and posts the normal Textual resize event only when the active screen is stale.
Headless tests, inline mode, and web mode do not install that fallback.

For the active conversation scope, local `/tasks` renders bounded task metadata
without command text or output and a periodic read-only poll emits one notice
per terminal transition. It cannot mutate task state; `kill_task` remains on the
ordinary model tool and permission path. See
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md).

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

The same controller exposes a workspace-scoped `SessionOption` catalog and
serializes session selection with turns. The composition root filters recent
SQLite summaries by filesystem identity, then `AgentConversation.open`
revalidates the selected ID. Resume prefers a ready source-named profile and
otherwise uses the current ready profile while retaining the stored provider,
model, and affinity origin for fail-closed native-context projection. The TUI
replaces scrollback with a bounded visible-message projection that omits
reasoning, native records, arguments, image URLs, and raw tool-result content.
See
[ADR 0018](adr/0018-workspace-scoped-interactive-session-resume.md).

The same catalog has a separate ranked-search path. `SessionStore` returns
typed title/content hits from a synchronized SQLite FTS5 projection; the
composition root applies filesystem-identity workspace filtering before the
controller creates `SessionOption` values. `/sessions QUERY` displays the saved
or deterministic first-prompt title plus an optional literal-text snippet.
System messages, provider-preserved items, assistant private reasoning, tool
arguments/metadata, raw tool-result content, and image URLs never enter that projection. See
[ADR 0025](adr/0025-session-title-and-full-text-search.md).

Manual rename follows the same boundary. `SessionStore.update_session_title`
returns the updated canonical summary and changes the SQLite title, update
timestamp, and synchronized FTS document atomically. The TUI composition root
permits rename only for the current filesystem-identity workspace, while the
controller serializes it with model turns. CLI callers can rename an explicit
ID in the selected state database.

The operating-system sandbox is also part of session identity. Native sessions
persist the canonical creation profile. Explicit-ID startup performs an
immutable read-only SQLite metadata lookup before process sandbox enforcement;
the saved value is restored unless a canonically different explicit CLI or
environment request causes a conflict. In-process TUI resume cannot replace an
irreversible process sandbox, so a different-profile option is disabled and
requires restart. `AgentConversation.open` verifies the profile again after the
ordinary summary load. See
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md).

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
- `ShellSandbox`: turns a shell string into an argv-safe, platform-enforced
  launch without exposing namespace implementation details to tools.
- `BackgroundTaskSupervisor`: creates isolated conversation task scopes and
  terminates every live tree during application shutdown.
- `BackgroundTaskManager`: starts owned shell/exec trees and exposes bounded
  snapshot/single-or-multi-wait/kill and pending-completion acknowledgement
  operations within one conversation scope.
- `PermissionManager`: returns allow, deny, or ask before any side effect.
- `PermissionApprover`: optionally resolves an `ask` asynchronously without
  overriding policy denial.
- `SessionStore`: appends versioned events, preserves ordered `SessionItem`
  values, exposes canonical and ordinary-message projections, and returns
  typed, paginated session-title/content search pages.
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
- A sandbox activation marker is insufficient evidence by itself. Linux
  composition attests root, workspace, and state mount flags before tools are
  exposed; `strict` also attests its allowlist-root filesystem type.
- `read-only` removes and independently rejects the workspace edit tool.
  `read-only` and `strict` shell descendants run without the parent agent's
  network namespace, while provider HTTP remains available to the parent.
- Secrets never appear in inspect output, logs, session events, or exceptions.
- Bash descendants do not inherit configured provider API-key variables or
  standard/explicit proxy variables; secret access requires a future explicit
  capability rather than ambient process state.
- API and proxy credentials are environment references; resolved proxy URLs
  remain adapter-local and are removed from network errors.
- Provider failover may occur only before the candidate's first model event;
  after that boundary, errors propagate without replaying the step elsewhere.
- Interactive profile switching is serialized between turns and starts a fresh
  conversation. It never relabels or replays the previous session under the new
  profile.
- Interactive session selection lists only filesystem-identical workspaces,
  revalidates the ID while opening, and replaces the active binding only after
  a successful resume. Stored provider/model/affinity origin is retained even
  when ordinary messages resume through the current profile.
- A session with sandbox metadata always resumes under its creation profile.
  Canonically different explicit CLI/environment requests and in-process
  different-profile selection fail before a model turn or tool action; corrupt
  and unsupported stored values also fail closed.
- Restored TUI history never renders persisted reasoning, native provider
  records, tool arguments, image URLs, or raw tool-result content.
- Session search indexes only its visible local projection. Interactive hits
  remain workspace-scoped and saved queries/titles/snippets are rendered as
  literal text rather than UI markup.
- Cancellation terminates owned child processes, commits a terminal failure,
  saves balanced context, and reloads it before the next conversation turn.
- Shell commands execute in an owned process group. Timeout and cancellation
  attempt graceful tree termination first, then force termination after a
  bounded grace period; output is drained with a fixed in-memory limit.
- Background shell commands remain owned by the application supervisor and
  visible only through their conversation scope. Their combined output preview,
  running-task count, retained records, wait interval, and lifetime are bounded;
  binding replacement or application exit terminates the affected live trees.
- Model completion reminders contain only JSON-escaped IDs/status metadata, are
  capped per model boundary, exclude commands/output/cwd, and are acknowledged
  only after provider completion or a canonical terminal task-tool result.
- Restrictive Bash rules inspect every safely decomposable command segment,
  including common wrappers and nested `bash -c` scripts. Unclassifiable
  scripts fail closed when a deny/ask policy could apply.
- Legacy upstream state is imported read-only and never modified in place.

## Persistence

SQLite is the canonical transactional store for sessions and their ordered
events. JSON and Markdown are interchange/export formats. The database exposes
an integer schema version; every change requires forward migration, fixture
coverage, and a documented compatibility decision. Schema v3 adds a nullable
canonical sandbox profile: new sessions store a value, while migrated legacy
sessions retain `NULL`. Schema v4 adds stable optional titles and a
trigger-synchronized external-content FTS5 projection. Migration derives a
ten-word title from the first visible user message when no imported title
exists and backfills escaped conversation content without indexing private
provider items. Startup can inspect the sandbox field through an
immutable read-only connection before any database creation, migration, or
process sandbox activation. Rust sessions are parsed by
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
export schema 4 includes both projections, the session sandbox profile, and the
optional title.
Provider adapters validate image
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

The first concrete implementation re-executes Linux runs under bubblewrap for
`workspace`, `read-only`, and `strict`; `off` remains the portable default.
Filesystem mounts cover in-process Python tools and descendants. A separate
`ShellSandbox` launch plan places Bash descendants of `read-only` and `strict`
inside a nested network namespace. macOS and Windows currently reject explicit
non-`off` profiles rather than advertising unenforced behavior. See
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) and
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md).

Foreground and managed-background shell commands share `ProcessTree`. POSIX
waiting observes the owned process group after its shell leader exits, while
termination uses a bounded TERM-to-KILL sequence. Windows still uses a process
group plus `taskkill /T /F`; Job Object ownership is required for full parity.
See [ADR 0021](adr/0021-owned-background-shell-tasks.md) and
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md). Model-visible
completion metadata is defined by
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md), and
event-driven multi-task waits by
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md).
