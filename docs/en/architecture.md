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
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

The target package boundaries are `domain` for pure values and rules,
`application` for orchestration, `application/ports` for required abstractions,
`infrastructure` for concrete outbound adapters, `interfaces` for inbound
adapters, `bootstrap` for configuration/factories/assembly, and `shared` for
small cross-cutting primitives. Bootstrap is the only layer allowed to depend
on interfaces, application, and infrastructure together. Domain and
application must not import concrete infrastructure implementations.
Canonical process entry points live in bootstrap. The few inbound-to-bootstrap
compatibility and launch edges are individually recorded by the AST guard; they
do not permit an interface to assemble concrete dependencies itself.

Stage 1 established `neuro_code.shared.{errors,async_utils,redaction}` and
`neuro_code.application.ports.*` as canonical paths. The development-stage
breaking cleanup removed the root shared compatibility modules
`neuro_code.{errors,async_utils,redaction}` and `neuro_code.ports`; shared
primitives and port contracts are available only from their canonical paths.
Stage 2A establishes
`neuro_code.application.settings.ApplicationSettings` and
`neuro_code.bootstrap.composition.ApplicationComposition` as canonical paths.
`neuro_code.application` retains only its lazy `ApplicationSettings` package
export; composition must be imported explicitly from `bootstrap.composition`,
so an ordinary `application.ports` import does not load bootstrap or concrete
infrastructure. Approval interaction contracts now live only in
`neuro_code.application.permissions.contracts`. The development-stage breaking
cleanup removed the root `PermissionApproval`, `PermissionApprovalKind`,
`PermissionRequest`, and `build_permission_request` re-exports;
`neuro_code.permissions` retains only synchronous permission policy
implementation.

Stage 2B establishes `neuro_code.bootstrap.entrypoints` as the canonical CLI
and TUI launcher, and the console scripts plus `python -m neuro_code` now use
it directly. It selects application composition, SQLite session storage, the
historical session importer, TUI settings/catalog/preferences ports, and
workspace identity behavior only when the corresponding command needs them.
`neuro_code.cli` retains parsing, dispatch, rendering, and exit-code handling;
its injected `run` function is invoked by the canonical bootstrap entrypoint.
Importing the CLI does not load bootstrap, adapters, providers, or create
resources.

Stage 2C keeps `neuro_code.acp` in place as the ACP/JSON-RPC inbound adapter,
but gives it only `application.acp` contracts and an ACP-specific application
service. The service exposes binding creation and safe resume preparation,
session aliases and listing, workspace validation, protocol metadata, and a
session-lazy MCP tool context. `bootstrap.entrypoints` adapts
`ApplicationComposition`, the session store, workspace identity checks, and
the concrete stdio MCP collection to those contracts, then starts the server.
`serve_acp` accepts only the resulting `AcpApplicationService`; it no longer
adapts an `ApplicationComposition` caller. ACP no longer imports MCP or workspace implementations, or reads composition
configuration or storage directly; importing ACP does not load bootstrap, the
MCP adapter, SQLite storage, or providers.

Application runtime behavior currently lives in the explicit canonical
submodules of `neuro_code.application.runtime`:
`background_task_reminders`, `agent`, `conversation`, `profile_conversation`,
`terminal_sessions`, `approval`, `instruction_tracker`, and `skill_tracker`.
The development-stage breaking cleanup removed `neuro_code.runtime`; runtime
application behavior is available only from these explicit canonical
submodules. `neuro_code.application.runtime.__init__` currently remains minimal
and provides no aggregate API, and internal production code imports the
canonical submodules directly.

`neuro_code.config` currently owns `AppConfig` and `ProviderProfile`, TOML and
CC Switch configuration, environment overrides, routing, managed overlays,
sandbox policy, stored-credential injection, and HTTP proxy policy. The
synchronous managed JSON reader in
`neuro_code.configuration.managed_provider_settings` owns schema, protocol,
and dialect checks, the file-size limit, metadata/credentials merging,
structural validation, and `ManagedProviderSettings` construction.
`neuro_code.adapters.provider_settings` owns `JsonProviderSettingsStore`,
asynchronous persistence, atomic writes, and POSIX private permissions. It uses
the canonical reader through a private binding and no longer re-exports it.
`neuro_code.config` likewise uses the reader through a private binding and no
longer imports the provider-settings adapter; `ProviderProfile` and `AppConfig`
replace its removed `ProviderConfig` alias for this boundary.
The active temporary allowlist is empty. The only remaining raw forbidden edge
is the canonical package-executable entrypoint,
`neuro_code.__main__ -> neuro_code.bootstrap.entrypoints`; it is not
compatibility debt.

`ApplicationComposition` in `bootstrap.composition` resolves configuration and
provider overrides, performs session-sandbox preflight, initializes SQLite,
creates providers/tools/permission managers and conversation-scoped
background-task registries, and owns supervisor shutdown. Stage 2A changes only
its structural ownership: the initialization and failure-cleanup ordering is
unchanged. CLI, TUI, and ACP continue to share the same service and typed
runtime event stream.

See [ADR 0049](adr/0049-progressive-architecture-boundaries.md) for the complete
dependency rules, compatibility migration policy, and allowlist discipline.

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

## Repository-level AGENTS.md instruction discovery

Repository-level AGENTS.md files are project-owned, non-system instructions that
guide agent behaviour within the workspace boundary. They are never loaded from
the network, never executed, and never allowed to impersonate system or user
messages. All discovery is deterministic, bounded, fail-closed, and walks only
from the workspace root toward the target directory.

The discovery service is defined by the `InstructionDiscovery` port; the
default adapter is `FilesystemInstructionDiscovery`. The composition root
constructs the adapter through an `InstructionDiscoveryFactory` and installs a
per-binding `instruction_provider` closure for each conversation. File tools
move a binding-local target, and the closure re-discovers from the workspace
root toward that target before each model step. The bounded filesystem work
runs outside the event-loop thread, so AGENTS.md changes take effect on the
next step without restarting the process. Discovery results are not cached
across sessions, do not enter the durable `SessionItem` history, and the
injected instruction message is a transient, per-step synthetic item.

`InstructionTracker` separately records the last result actually injected
into a model step. Before `search_replace`, it compares the target directory's
current instructions with that snapshot by path and content. A new or changed
instruction aborts the write as an error, allowing the next model step to see
the new rules before retrying. Arbitrary Bash paths cannot be inferred safely,
so Bash writes retain this documented limitation.

Discovered instructions are not appended to the system message. They are
injected as a separate synthetic `User` message after the system message and
before any genuine user messages, tagged with
`SyntheticReason.PROJECT_INSTRUCTIONS`. This structured source marker ensures
repository content does not share the trust level of the application system
prompt. `Message.synthetic_reason` exists only in memory: synthetic items are
rebuilt per step and never enter storage, UI, or protocol conversation history.

Discovery walks root-to-target and returns instructions shallowest-first.
Filesystem work is capped at 20 directory levels, 10 loaded files, 64 KiB per
file, and 256 KiB total. UTF-8, C0/C1/DEL controls, regular-file identity, and
workspace boundaries are validated. All symlinks and Windows reparse points
are rejected; audit output distinguishes escape, circular/broken, and
otherwise in-bound links. Rejection paths escape control characters before
they reach a terminal or JSON output.

File reads are bounded and best-effort symlink-resistant: `lstat()` rejects
symlinks and Windows reparse points; `os.open()` with `O_NOFOLLOW` (POSIX)
opens the handle; handle-level `fstat()` verifies a regular file and
compares `st_dev`/`st_ino` with the lstat result to detect path
substitution between lstat and open; `os.read()` reads at most
`MAX_SINGLE_FILE_BYTES + 1` bytes (the +1 detects over-limit files); a
post-read `fstat()` verifies handle identity unchanged. This is not a
fully TOCTOU-safe implementation -- POSIX `O_NOFOLLOW` only protects the
last path component and Windows lacks `O_NOFOLLOW` -- but the combination
of lstat rejection, bounded read, and lstat-with-fstat identity comparison
provides strong defence against common attack vectors. A target directory
that escapes the workspace is rejected as a whole with `ESCAPES_WORKSPACE`,
not silently clamped to the root. All rejection reasons are enum values so
that inspect and audit surfaces can distinguish them.

The CLI `inspect` command renders loaded file paths, depths, content byte
counts, the fingerprint, and all rejections from the same discovery service.
JSON mode includes an `instructions` field; plain mode renders one item per
line. ACP and TUI inject the `InstructionDiscovery` instance via
`ApplicationComposition`; CLI inspect uses
`ApplicationComposition.default_instruction_discovery()` to construct an
instance from the same default factory. They share the same port contract
and default factory, so discovery rules do not diverge by interface, though
inspect does not use the same runtime instance as a live session. See
[ADR 0039](adr/0039-repository-instruction-discovery.md).

## Read-only skill file discovery

Project, repository, and user `SKILL.md` files are read-only metadata that
inform the model of available skills. Skill discovery follows the same
ports-and-adapters pattern as instruction discovery: a `SkillDiscovery` port,
a `FilesystemSkillDiscovery` default adapter, an
`ApplicationComposition`-injected factory, and a per-binding
`skill_provider` closure that re-discovers before each model step. The
adapter imports the filesystem safety helpers (`_toctou_safe_read`,
`_is_symlink_or_reparse_point`, `_resolve_within_workspace`) from the
instruction discovery adapter, so both share the same bounded, best-effort
symlink-resistant read pattern.

The adapter scans `.neuro/skills/`, `.agents/skills/`, `.grok/skills/`, and
`.claude/skills/` for `SKILL.md` files, walking up to
`MAX_SKILL_WALK_DEPTH = 5` directory levels. Skills are deduplicated by name
(first-seen wins, ordered by config-directory priority within each scope) and
ordered by scope priority (`LOCAL` > `REPO` > `USER`). All three scopes
are implemented: `LOCAL` (the moving target up through the workspace root),
`REPO` (every ancestor above the workspace through the git root), and `USER`
(the user home directory). Each `SKILL.md`
frontmatter block is handled by a bounded, dependency-free line parser for
common `key: value` scalars, quotes, and inline comments. Delimiters must
occupy complete lines. Missing or malformed metadata falls back to the skill
directory name and first prose body line.

The model receives a byte-bounded compact catalog -- skill name, description,
and when-to-use -- not full skill bodies. The listing is injected as a separate
synthetic `User` message tagged with
`SyntheticReason.AVAILABLE_SKILLS`, inserted after the instruction message
(or after the system message if no instructions were discovered). This
preserves the "repository content does not share system prompt trust level"
safety invariant. The `Message.synthetic_reason` field is an in-memory
marker only; skill listings do not enter `SessionItem` persistence and are
re-discovered on each model step. The runtime `SkillTracker` is
session-scoped and maintains a moving target (mirroring the
`InstructionTracker` design): when file-access tools touch a path,
`check_path()` updates the target so that `SKILL.md` files from the
accessed directory upward to the workspace root (inclusive) are
discovered in the next `current_result()` call. The adapter also scans every
repository ancestor above the workspace through the git root (`REPO` scope)
and user home (`USER` scope) on each call.

The CLI `inspect` command renders discovered skill file paths, scopes,
depths, rejections, and the SHA-256 fingerprint through the same
`application.skill_result` property used by the runtime. See
[ADR 0040](adr/0040-read-only-skill-discovery.md).

## Skill body loading tool

The `SkillTool` (`tools/skills.py`) allows the model to load the full body
of a discovered skill by name. The model first sees a compact skill catalog
via the `AVAILABLE_SKILLS` synthetic message; when it decides a skill is
relevant, it calls the `skill` tool with the skill name to load the
complete SKILL.md body.

The tool follows the same bounded, symlink-resistant read pattern as
discovery. It resolves `skill.root / skill.relative_path`, checks the relevant
LOCAL, REPO, or USER boundary, and verifies that the loaded content still
matches the discovery fingerprint. It strips BOM and YAML frontmatter, then
returns a bounded `<skill_content>` block. The bundled-file sample contains at
most 10 direct regular-file names; links, directories, control-character
names, and oversized directory listings are omitted.

The `ToolContext` dataclass depends on the `SkillContextTracker` port (the
concrete runtime tracker is not imported into the port layer), wired by
`ApplicationComposition.create_binding()`. The `SkillTracker` re-discovers
on each `current_result()` call, so skill file changes take effect on the
next tool invocation without a session restart. Variable substitution is
performed at load time: the `SkillTool` accepts an optional `args`
parameter and expands `$ARGUMENTS`, `$ARGUMENTS[N]`, `$N`, and
`${SKILL_DIR}` tokens in the body via `apply_skill_substitutions()` in
`domain/skills.py`. When the body contains no argument tokens but args are
non-empty, the args are appended as a `**ARGUMENTS:**` suffix for backward
compatibility. Arguments, substitution count, and rendered output are byte or
count bounded; unsupported positional tokens such as `$100` remain literal.
See
[ADR 0041](adr/0041-skill-body-loading-tool.md) and
[ADR 0045](adr/0045-skill-variable-substitution.md).

## User-level skill discovery

`FilesystemSkillDiscovery` accepts an optional `user_home: Path | None`
constructor parameter. When `None`, the adapter resolves the user home at
discovery time via `Path.home()`. LOCAL discovery uses the workspace as its
common boundary, REPO discovery uses the detected git root, and USER discovery
uses the resolved user home. When the workspace root and user home are the same path
(e.g. when the session is launched from the home directory), the USER pass
is skipped to avoid double-scanning. The candidate tuple carries both the
discovery root and the scope so the processing loop can compute
POSIX-relative paths and perform boundary checks against the correct root
for each candidate.

`SkillInfo` gained a `root: Path | None` field (defaulting to `None` for
backward compatibility) that stores the discovery root the skill was found
under. `SkillTool` resolves the absolute path via
`skill.root / skill.relative_path` (falling back to
`tracker.workspace_root` when `root` is `None`) and performs the boundary
check against the discovery root rather than the workspace root. This keeps
path resolution correct for both LOCAL skills (root = workspace) and USER
skills (root = user home) without changing the tool's public contract.

Cross-scope priority is scope-first: LOCAL candidates are collected and
processed before REPO candidates, which are collected before USER
candidates. The same first-seen-wins dedup by name ensures a LOCAL skill
shadows a REPO skill shadows a USER skill with the same name. Within each
scope, the config-directory priority (`.neuro` → `.agents` → `.grok` →
`.claude`) still applies. See
[ADR 0042](adr/0042-user-level-skill-discovery.md) and
[ADR 0044](adr/0044-repository-level-skill-discovery.md).

## Dynamic mid-session skill discovery

The `SkillTracker` maintains a moving target, mirroring the
`InstructionTracker` design. When file-access tools (`read_file`,
`list_dir`, `grep`) touch a path, `check_path()` updates the target so
that `SKILL.md` files from the accessed directory **upward** to the
workspace root (inclusive) are discovered in the next `current_result()`
call. This finds skills located at any nesting depth in the workspace,
not just at the workspace root — for example,
`src/foo/.neuro/skills/commit/SKILL.md` is discovered when the model
reads a file in `src/foo/`.

The adapter walks **upward** from `target` to `workspace_root`
(inclusive), checking each ancestor directory for config dirs. Deeper
ancestors are scanned first so first-seen-wins name deduplication gives
precedence to more specific (deeper) skills over general (root) skills,
matching the grok-build "deepest-first" model. When `target` is `None`
or equals the workspace root (e.g. CLI inspect, `rediscover_skills`),
the walk degenerates to scanning just the root level.

Sibling subtrees are isolated: switching from `src/foo/` to `src/bar/`
moves the target, and skills from `src/foo/`'s config dirs are no longer
included. The `SkillTracker.check_path()` is called by `ReadFileTool`,
`ListDirTool`, and `GrepTool` alongside their existing
`InstructionTracker.check_path()` calls. `SearchReplaceTool` does not move the
skill target (its instruction tracker has a separate write preflight), and
`BashTool` does not attempt to infer paths from arbitrary shell syntax. See
[ADR 0043](adr/0043-dynamic-session-skill-discovery.md).

## Repository-level skill discovery

When the workspace is a subdirectory of a git repository (e.g.
`myrepo/packages/frontend/`), the adapter detects the git root by walking
upward from the workspace root looking for a regular, non-link ``.git``
directory or file. It scans every ancestor above the workspace through the
git root, nearest first, and tags those skills with `SkillScope.REPO`. Thus a
package-level repository skill can shadow a git-root default while both remain
visible to a nested workspace.

The REPO scan is skipped when the git root equals the workspace root
(already covered by LOCAL discovery), or when no acceptable ``.git`` marker
is found within the bounded upward walk. The
`FilesystemSkillDiscovery.__init__` accepts an optional `git_root` parameter
(defaulting to `None` for auto-detection) following the same pattern as
`user_home`. Every REPO `SkillInfo.root` is the common git root, so paths from
intermediate ancestors remain unique and `SkillTool` resolves them against
one stable boundary. See
[ADR 0044](adr/0044-repository-level-skill-discovery.md).

## Partial ACP v1 adapter

`neuro-code acp` is a protocol adapter over `ApplicationComposition` and the
official `agent-client-protocol` Python SDK. Production framing, JSON-RPC
routing, newline-delimited stdio, `session/update` notifications, and
`session/request_permission` requests remain SDK-owned. The adapter declares
`loadSession: true` plus list/delete/fork/resume/close session capabilities and
implements `initialize`, `session/new`, `session/list`, `session/load`,
`session/delete`, `session/fork`, `session/resume`, `session/prompt`, the
`session/cancel` notification, and `session/close`. SDK 0.11 gates fork,
resume, and close behind `use_unstable_protocol`. Its generated schema includes
stable delete models but its Agent router omits that route, so Neuro Code adds
only the generated delete request to the official `MessageRouter`; SDK streams,
`Connection`, dispatcher, schemas, framing, and error normalization remain
unchanged.

One ACP connection is bound to the normalized launch workspace. Each accepted
session owns a stable random ACP ID, one `AgentConversation`, one background
task scope, one active-prompt slot, and independent approval/cancel/close
state. The internal SQLite ID stays separate and is recorded lazily when the
first prompt starts; SQLite schema v5 persists a unique namespaced alias so a
later process can load the same ACP ID. Session creation publishes nothing
until every resource is ready. Load reserves the requested ACP ID, revalidates
workspace, fixed sandbox, and provider affinity, reconstructs the conversation
and background scope, replays history, and only then publishes the session.
Resume follows the same checks and reconstruction but does not replay history.
Fork copies a persisted ordered context and its provider/sandbox affinity into
new internal and external IDs, then builds an independent session without
replaying history; source prompts must be idle, and failed publication deletes
the copied row. Delete first closes active resources and then removes the
workspace-local durable session, whose events, alias, and search rows cascade.
Close first applies cancel semantics, waits for required terminal tool updates
and the prompt response, closes the scope, drops the runtime binding, and
leaves durable history and the alias intact. EOF or connection failure runs
the same idempotent cleanup for every active or creating session.
See [ADR 0050](adr/0050-acp-session-lifecycle.md).

`additionalDirectories` may declare at most four existing, absolute,
non-overlapping directory roots for a particular new, loaded, resumed, or
forked ACP binding. They are validated after the connection workspace and are
not persisted with the durable session; clients must declare them again for a
later binding. File tools accept paths within the primary or one of these
explicit roots, while instruction and skill discovery remain confined to the
primary workspace. Change reports snapshot each declared root and label extra
root paths absolutely. `off` sessions retain their ordinary permission flow.
Every enabled sandbox rejects non-empty additions because its mount namespace
was fixed before the ACP request; this also avoids treating a platform's
writable temporary or state mounts as a late-declared directory root. This
preserves the explicit sandbox boundary rather than adding late host mounts.

When an ACP client explicitly advertises `fs.readTextFile`, a session receives
a narrow `ClientFileSystem` application port bound to that ACP session. The
existing `read_file` tool still resolves every path through the selected primary
or additional workspace roots, then delegates the absolute path and its bounded
line range to `fs/read_text_file`; it never falls back to an unadvertised client
operation. When the client advertises both text read and write, `search_replace`
uses the same port to read, preserve the existing exact-match/ambiguity and
instruction preflight rules, then write the result through `fs/write_text_file`.
The tool is not exposed for a read-only client. Client responses and writes are
each limited to 1 MiB, client failures are rendered as stable fail-closed tool
errors without raw details, and the client remains responsible for the final
write's filesystem semantics.

When an ACP client explicitly advertises `terminal: true`, an `off`-profile
binding also receives a session-bound `ClientTerminal` port. The separate
`terminal_exec` tool accepts one executable and a bounded argument vector; it
does not reinterpret the existing local `bash` tool as a remote shell. Each
call creates, waits for, reads, and releases one client terminal, limits output
to 1 MiB, requests kill on timeout or cancellation, and forwards no configured
Neuro Code environment values. The same session-bound port also exposes
standard ACP background direct-executable tools: `terminal_start`,
`terminal_output`, `terminal_wait`, and `terminal_kill`. They expose opaque task
IDs, allow at most eight running and 32 retained tasks, and kill/release work on
timeout or session cleanup. Ordinary side-effect permissions still gate starts
and kills. Every enabled sandbox omits the tools and direct use fails closed, so
a client terminal cannot weaken an explicit local sandbox. Interactive
input/resize, cursor streaming, and PTY framing/backpressure remain unsupported.

Non-empty `mcpServers` accept ACP stdio, Streamable HTTP (`http`), and legacy
SSE (`sse`) shapes; ACP-transport servers are rejected deterministically.
Every server is initialized and its bounded, paginated tool catalog is
validated before the session is published; duplicate server names, invalid tool
names, collisions between remote tools or with built-ins, protected environment
overrides, unsafe URL/header input, and oversized configuration fail the
complete session creation. Remote URLs must be absolute HTTP/HTTPS endpoints
without embedded credentials or fragments. Header names, counts, values, and
total bytes are bounded; framing and routing headers cannot be overridden. The
same ephemeral MCP configuration may be supplied when loading a durable ACP
session, but it is not persisted as session history or authority.

The official `mcp>=1.28.1,<2` SDK owns MCP schemas, `ClientSession`, version
negotiation, JSON-RPC dispatch, and tool result types. Stdio uses a
project-owned newline-delimited `ProcessTree` bridge because the official SDK's
post-spawn Windows Job attachment cannot meet Neuro Code's atomic Job-list
requirement. Streamable HTTP and SSE use the SDK clients with an application
HTTP client that disables environment proxies and redirects, retains TLS
verification, and caps every response body at 1 MiB. Frames, schemas, tool
counts, JSON depth/nodes, arguments, output, and timeouts are bounded; MCP
stderr is drained without entering ACP stdout; `_meta`, image/audio/embedded
bodies, and unbounded raw values are never projected. ResourceLink results
remain metadata and are not dereferenced. Explicit server environment/header
values and application credentials are redacted from model-visible text.

MCP annotations are untrusted hints, so every projected MCP tool is marked
side-effecting. `ApplicationComposition` installs an exact ASK rule above
bypass/always-approve behavior while retaining explicit local DENY precedence.
The ordinary runtime therefore emits pending, requests ACP permission, and
only then emits in-progress and calls the server. A declined request never
executes. Stdio cancellation terminates the whole owned process tree before the
tool failure update and `cancelled` prompt response complete. For a remote
server, cancellation closes the SDK connection and makes it unavailable for
later calls; no local process ownership is claimed, so an indeterminate remote
side effect is never reported as successfully cancelled. Close, load failure,
creation failure, EOF, and disconnect close the same session-owned collection
idempotently. MCP resources, prompts, sampling, elicitation, dynamic tool-list
refresh, and ACP transport remain unsupported.

List is discovery-only and remains scoped to the connection workspace even
when `cwd` is omitted. It returns only durable ACP ID, absolute recorded cwd,
bounded title, and ISO update time. Sessions without an alias receive one
through an atomic schema-v5 get-or-create operation. SQLite keyset pages are
filtered through filesystem-identity workspace comparison. A request returns
at most 50 matches while scanning at most 5,000 rows; random connection-local
cursor tokens retain only the keyset position in memory, are capped at 256,
and reveal no internal ID. List never opens a conversation/background scope or
returns content, provider metadata, `_meta`, or additional directories.

Prompt conversion accepts ACP baseline Text, inline Image, ResourceLink, and
embedded `TextResourceContents` blocks in their supplied order. Text/resource
counts, per-field sizes, annotation serialization, ResourceLink aggregate
bytes, and total text bytes are bounded. An Image block accepts only validated
base64 for a fixed raster MIME allowlist: at most eight images, 5 MiB decoded
per image, and 10 MiB decoded in aggregate. Its optional URI, local files, and
remote links are never read, downloaded, or dereferenced. An embedded text
resource accepts only the supplied text: at most eight values, 64 KiB per
value, and 128 KiB in aggregate. It becomes a labeled text `ContentPart` with
a bounded URI and optional MIME type; its URI is never resolved, and block,
resource, and annotation `_meta` values are omitted. The canonical ordered
`ContentPart` values are persisted with the user message so provider adapters
can apply their own role, MIME, and request-size validation on the current turn
and a resumed session. Only `uri`, `name`, `title`, `description`, `mimeType`,
`size`, and standard annotation fields reach a model-visible ResourceLink
description; `_meta` is ignored. Audio and embedded `BlobResourceContents`
prompt blocks remain rejected.

Load history uses a second explicit projection. Visible user and assistant text
become standard message chunks with fresh UUID message IDs. Ordered image parts
become the existing safe image placeholder, never a raw data URI, image byte
payload, or remote URL. Embedded text resources remain their bounded, labeled
user text. Tool calls expose only bounded/redacted name, kind, allowlisted
path, and result content, with balanced pending-to-terminal
updates. System messages, reasoning, preserved provider context, arbitrary
arguments, `_meta`, and raw input/output are omitted. The complete replay is
validated before its first update and is bounded by stored-item, update-count,
per-field, and aggregate serialized-byte limits.

The event projection is an explicit allowlist:

| Runtime event | ACP projection |
|---|---|
| `TEXT_DELTA` | `agent_message_chunk` with one stable per-answer `messageId` |
| `TOOL_REQUESTED` | `tool_call` / `pending` |
| `TOOL_STARTED` | `tool_call_update` / `in_progress` |
| `TOOL_COMPLETED` | bounded, redacted `tool_call_update` / `completed` |
| `TOOL_FAILED` | bounded, redacted `tool_call_update` / `failed` |
| valid `CONTEXT_USAGE_UPDATED` | standard `usage_update` when the context window is known |
| `REASONING_DELTA`, `TURN_COMPLETED`, `TURN_FAILED` | no custom update |

The original prompt response carries `end_turn`, `max_tokens`,
`max_turn_requests`, `refusal`, or `cancelled`. Approval follows the existing
fail-closed permission manager: local deny/workspace/sandbox decisions remain
authoritative, a pending tool update precedes the client request, and execution
cannot start until approval returns. The negotiated client filesystem and
terminal capabilities are invoked only through their session-bound
application ports; no ACP SDK type reaches application code. See
[ADR 0035](adr/0035-partial-acp-v1-stdio.md) and
[ADR 0036](adr/0036-durable-acp-session-load.md) plus
[ADR 0037](adr/0037-workspace-scoped-acp-session-list.md), plus
[ADR 0038](adr/0038-session-owned-stdio-mcp-tools.md), plus
[ADR 0052](adr/0052-capability-gated-acp-client-filesystem.md) and
[ADR 0053](adr/0053-capability-gated-acp-client-terminal.md), and
[ADR 0054](adr/0054-bounded-acp-inline-image-prompts.md), and
[ADR 0055](adr/0055-bounded-acp-embedded-text-resources.md), and
[ADR 0056](adr/0056-bounded-acp-client-background-terminals.md).

The minimal TUI is a presentation adapter over `AgentEvent`. It owns prompt
input, scrollback, a live text surface, and local slash commands. It never
renders raw reasoning or unrestricted argument/result mappings. A bounded
allowlist supplies invocation previews such as path, command, pattern, query,
and task ID. Each local tool call then owns one stable card, keyed by call ID,
which is updated with its permission path, redacted result preview, elapsed
time, and any bounded workspace-change report. Read-like calls project a
one-line completed summary until the user opens the existing bounded details;
edit reports open their changed slices automatically. Diff roles use both
foreground and tinted-background styling. See
[ADR 0014](adr/0014-minimal-event-stream-tui.md) and
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md).

The scrollback is a vertical conversation of stable message widgets rather
than a pre-rendered log plus a temporary streaming surface. User prompts and
assistant responses have distinct layouts. A pending assistant widget remains
the final conversation node while lifecycle notices are inserted before it;
text deltas and the terminal response update that same node. Auto-follow occurs
only while the viewport is already at the end. See
[ADR 0026](adr/0026-stable-localized-tui-conversation.md).

Assistant widgets use Rich's Markdown document model with an application-owned
semantic theme and disabled hyperlink activation; model output is never passed
through Rich/Textual markup parsing. User content and application/external
values use literal `Text`. Local system, status, tool, and error records are
two-column grids with a fixed label gutter and a folding body. Semantic value
classes—not arbitrary payload markup—select restrained colors for provider,
model, tool, session, path, outcome, duration, mode, effort, and error fields.
Tool output and diffs are literal application-styled `Text`, never payload
markup. Bounded details are focusable and can be collapsed or expanded without
fetching new data, as specified by
[ADR 0030](adr/0030-bounded-interactive-tool-card-details.md). Mermaid and media
remain outside this renderer. See
[ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md).

Application-owned TUI text is selected through `UiLanguage`. The injected
`UiPreferencesStore` port persists the language, requested reasoning effort, and
interaction mode,
with the JSON adapter using an atomic, user-only state file separate from
provider configuration. Invalid or absent values fall back independently to
English, `high`, and `normal`. English and Simplified Chinese catalogs have identical
keys. Switching language rerenders chrome and translatable local history, while
visible user/model text and already-sanitized tool previews remain untranslated
and are never sent to a translator.

The presentation adapter owns one fixed cool neutral-dark theme instead of exposing
Textual's unrelated theme and command-palette surfaces. The built-in palette is
disabled, provider and session discovery use the explicit application commands,
and session queries are rendered as literal plain text. A persistent runtime
bar above the prompt renders the active provider/model, compact working path,
context-window usage, requested/effective effort, and interaction mode from
controller state; it updates on
localization, profile failover, and selection rather than scraping transcript
messages. A pure collapsing-pulse state machine is advanced by a Textual timer
and rendered before the pending-assistant text only while waiting for model
output. Context
starts with a provider-neutral estimate over canonical
session items. Each model completion with token metadata emits
`CONTEXT_USAGE_UPDATED`, replacing that estimate with the provider-reported
input plus output count. The denominator is explicit profile metadata named
`context_window_tokens`; an absent value stays unknown.

Slash completion is a deterministic presentation catalog, separate from
command execution. It projects effort/mode choices and selectable redacted profile
names, shows placeholders for free-form arguments, and feeds both the inline
suggester and the visible hint row. The TUI's priority Tab action applies the
first candidate only while the main prompt contains a slash command; modal
focus traversal remains intact. In full-screen terminal mode, a low-frequency
viewport reconciliation reads the actual TTY dimensions and posts the normal
Textual resize event only when the active screen is stale. Headless tests,
inline mode, and web mode do not install that fallback.

Textual's platform driver owns raw/application mode and restores terminal state;
the application does not duplicate escape-sequence or `termios` ownership. The
CLI returns Textual's public `return_code` after `run_async`, and its composition
root shuts down the background-task supervisor from a `finally` block on normal
exit, a non-zero Textual result, or a launch exception. Opt-in production CLI
smoke tests drive a real `Ctrl+Q` through a standard-library PTY on Linux/macOS
and through ConPTY on Windows. They submit no model prompt and verify ordered
alternate-screen, cursor, and focus-tracking teardown; POSIX also compares full
`termios`, while Windows tests resize, inject idle `Ctrl+C`, preserve non-zero
exit codes, and compare any available parent console modes. The private
standard-library `windows_conpty` adapter owns synchronous pipes, extended
process creation, bounded capture, and a dedicated output-drain thread that
remains active across `ClosePseudoConsole`. See
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md). The
process-boundary shape follows the read-only pinned baseline evidence in
`crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs` without copying its Rust
implementation.

Above the native adapters, `LocalInteractiveTerminalManager` implements the
shared `InteractiveTerminalManager` port. Creation crosses permission,
workspace and matching-sandbox checks before spawn. A thread-safe bounded tail
ring exposes monotonic output cursors and exact dropped-byte counts; input,
resize, signals, wait and close share one owned lifecycle. POSIX targets the
complete PTY process group. Production Windows ConPTY creation combines the
pseudoconsole and Job-list attributes atomically, and terminate/close target
the complete Job. Cancellation waits for an in-progress native creation and
closes any resulting owner; shutdown waits for pending creations and closes all
registered sessions. The substrate is intentionally not exposed through ACP
until protocol framing, authorization and backpressure are defined. See
[ADR 0034](adr/0034-bounded-owned-interactive-terminal-sessions.md).

Runtime timing uses monotonic clocks. `MODEL_THINKING_COMPLETED` measures each
model step from dispatch to the first visible/actionable result; it does not
claim access to private provider reasoning telemetry. Tool terminal events carry
elapsed time, while `TURN_COMPLETED` places the whole-turn summary after the
stable assistant node. Tool invocation, permission path, output preview,
workspace changes, and terminal status are rendered in one bounded, in-place
tree. For a side-effecting local tool, the runtime compares bounded read-only
workspace snapshots taken after permission succeeds and immediately around the
execution. The report is audit metadata, not a permission or success signal.
`WorkspaceChangeObserver` is an application-composition dependency created per
binding by bootstrap; `AgentRuntime` construction is not promised as a stable
external Python API.
See [ADR 0028](adr/0028-timed-tool-feedback-and-interaction-modes.md) and
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md).

For the active conversation scope, local `/tasks` renders bounded live
background-task metadata alongside durable plan-execution task records. Neither
view includes command text or output. The periodic read-only poll emits one
notice per background-task terminal transition. `/tasks` cannot mutate either
kind of task; `kill_task` remains on the ordinary model tool and permission
path. `/view-task TASK_ID` is a separate, user-initiated exact read of the
current session's durable task; for a snapshot-bearing plan-execution record it
renders the full stored plan as reference only, without initiating a turn or
changing task state. See
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md),
[ADR 0058](adr/0058-durable-session-task-lifecycle.md), and
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md).

The TUI keeps its prompt available while a worker-owned turn runs. `Ctrl+C` and
local `/cancel` cancel that worker; an approval modal gives `Ctrl+C` the narrower
meaning of denying the pending request. Runtime-owned recovery and tool-result
balancing are defined in
[ADR 0016](adr/0016-recoverable-turn-cancellation.md).

`ProfileConversationController` also owns `InteractionMode`, serializes mode
changes with active turns, and reapplies the selected mode to replacement
bindings. `normal`, `accept-edits`, and `plan` map to deterministic permission
manager modes. `auto` defaults to the safe `accept-edits` preview until a safety
classifier exists; only an explicitly authorized `--always-approve` launch
retains bypass defaults. Prompt guidance describes the mode, but actual authority
comes exclusively from permission/workspace/sandbox adapters.

`SessionPlan` is a bounded domain value owned by the active conversation rather
than by a provider or UI. The ordinary non-side-effecting `update_plan` tool
validates a complete replacement. `AgentRuntime` saves an accepted plan through
`SessionStore`, emits `PLAN_UPDATED`, and adds its provider-neutral rendering to
subsequent model requests. `AgentConversation.open` restores it before a resumed
turn, and a fork copies the stored value. The Textual interface only reads this
state: `/plan DESCRIPTION` switches safely to plan mode before submitting the
description, while `/view-plan`/`/show-plan` render the localized saved state.
After an explicit user command, `/execute-plan`/`/run-plan` changes only to
`accept-edits` and asks the application to execute the saved plan. The runtime
creates one opaque, metadata-only `SessionTask` and persists
`PLAN_EXECUTION_REQUESTED` before the canonical user message. It transitions the
task exactly once to completed, failed, or cancelled before the corresponding
turn terminal event. These records are durable for inspection but do not copy
when a session is forked and do not schedule or wake further work. The handoff
remains auditable without granting command, network, workspace, or sandbox
authority. `/tasks` keeps durable-record summaries bounded. Only an explicit
`/view-task TASK_ID` calls the active conversation's exact, current-session
`SessionStore.get_session_task` read and renders the stored immutable snapshot
as reference. That read neither enters the model context nor changes the current
plan, creates a turn, executes work, requests approval, or has scheduler
semantics; a missing or legacy no-snapshot task reports no detail. There is
deliberately no plan-file write, task scheduler, or subagent lifecycle in this
slice. Current-plan comments are an intentionally
separate, bounded feedback channel: `/comment-plan STEP COMMENT` stores user
text under a numbered plan step, `/view-plan` renders it, and the next model
request receives it as transient plan guidance. The comment is not a canonical
message, approval, task, or execution request. Its plan fingerprint prevents it
leaking to a replacement plan; replacement and clearing of a plan remove
obsolete comments. See ADR 0028,
[ADR 0057](adr/0057-durable-structured-session-plans.md),
[ADR 0058](adr/0058-durable-session-task-lifecycle.md), and
[ADR 0059](adr/0059-bounded-current-plan-comments.md), plus
[ADR 0060](adr/0060-plan-execution-revision-snapshots.md) and
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md).

`ProfileConversationController` wraps the active `AgentConversation` for the
interactive composition. It serializes selection with turns and exposes only
redacted `ProviderOption` data to the TUI. Selecting a different configured
profile composes a new provider/runtime/conversation binding with no resumed
session; the old SQLite session remains untouched. This strict boundary avoids
cross-provider replay of encrypted reasoning, hosted-tool state, dialect
metadata, and profile-affine context. See
[ADR 0017](adr/0017-safe-interactive-profile-selection.md).

The controller also owns one process-local `ReasoningEffort` selection and
serializes changes with turns. It reapplies the requested value whenever a
profile or session replacement installs a new conversation binding. `low`,
`medium`, `high`, and `xhigh` map to application review guidance;
`ultracode` has an explicit effective value of `xhigh` until workflow
orchestration exists. The TUI exposes the selection through `Ctrl+E`, `/effort`,
and `/reasoning`; the CLI exposes `--effort`. Selection does not rewrite
provider configuration or session identity.

At each model step, `AgentRuntime` adds the selected guidance to a request-only
system message and places the typed requested value on `ModelContext`. The
guidance is not added to canonical `SessionItem` history. Provider adapters may
inspect the typed value, but the current adapters do not translate it into
provider-private reasoning parameters. A future native mapping must declare and
test its capability explicitly. See
[ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md).

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
  adapter-owned replay decisions and the provider-neutral requested reasoning
  effort for explicit capability handling.
- `Tool`: publishes a JSON schema and executes with a scoped `ToolContext`.
- `ToolRegistry`: resolves canonical tool names and rejects duplicates.
- `ShellSandbox`: turns a shell string into an argv-safe, platform-enforced
  launch without exposing namespace implementation details to tools.
- `BackgroundTaskSupervisor`: creates isolated conversation task scopes and
  terminates every live tree during application shutdown.
- `BackgroundTaskManager`: starts owned shell/exec trees and exposes bounded
  snapshot/single-or-multi-wait/kill and pending-completion acknowledgement
  operations within one conversation scope.
- `InteractiveTerminalManager`: creates permission/workspace/sandbox-gated,
  bounded interactive exec sessions and owns their shutdown.
- `TerminalPlatform`: projects POSIX PTY or Windows ConPTY/Job input, output,
  resize, signal, wait and close behavior behind one synchronous adapter port.
- `PermissionManager`: returns allow, deny, or ask before any side effect.
- `PermissionApprover`: optionally resolves an `ask` asynchronously without
  overriding policy denial.
- `SessionStore`: appends versioned events, preserves ordered `SessionItem`
  values, owns bounded durable session-task metadata, exposes canonical and
  ordinary-message projections, and returns typed, paginated session-title/
  content search pages.
- `InstructionDiscovery`: deterministically, bounded, fail-closed discovers
  AGENTS.md instruction files within the workspace boundary, returning an
  ordered list of `InstructionFile`s, `InstructionRejection`s, and a stable
  fingerprint. Adapters must not read from the network, must not execute
  discovered files, and must not follow symlinks that escape the workspace.
- `SkillDiscovery`: deterministically, bounded, fail-closed discovers
  read-only `SKILL.md` skill files at LOCAL, REPO, and USER roots, returning
  an ordered list of `SkillInfo`s, `SkillRejection`s,
  and a stable body-sensitive fingerprint. Adapters must not read from the
  network, execute discovered files, or place full bodies in model context;
  all links and reparse points are rejected.
- `PlatformAdapter`: encapsulates PTY, process, signal, path, clipboard, and sandbox differences.

Protocol models are versioned at external boundaries. Internal state prefers
frozen dataclasses and enums. Unstructured dictionaries must not cross module
boundaries except as validated JSON payloads.

## Provider profiles and compatibility gateways

The composition root selects a named `ProviderProfile`; the agent runtime never
branches on a commercial provider name. Profiles separate wire protocol
(`openai-chat`, `openai-responses`, `anthropic-messages`, or
`gemini-generate-content`) from optional dialect behavior such as xAI Responses.
The generic Responses adapter is implemented at
`neuro_code.providers.openai_responses.OpenAIResponsesProvider`; xAI behavior
is selected through `dialect = "xai"`, not through a separate Python provider
class. The development-stage breaking cleanup removed
`neuro_code.providers.xai_responses` and `XAIResponsesProvider`.
Credentials are environment references or a validated loopback-proxy
placeholder for manual TOML profiles. The TUI additionally uses a
`ProviderSettingsStore` port for user-managed profiles. Its JSON adapter writes
non-secret metadata and credentials to separate atomic owner-private files;
proxy mode and an optional environment-variable name are non-secret metadata,
while the resolved proxy URL remains environment-only;
`ProviderProfile.stored_api_key` is excluded from representations and redacted
inspection, and explicit configured values are scrubbed at the runtime
tool-result boundary before they reach model context, events, or persistence.
The current file-backed secret store is not encryption and can be replaced by a
platform-keychain adapter.

Managed profiles are loaded after TOML. A same-name managed profile replaces
the whole provider table instead of deep-merging it, so a project cannot reuse
a stored key with a workspace-controlled endpoint, proxy, or tool option. TUI
save-and-use exits at a bounded application restart code; the composition and
all background scopes close before configuration and the provider binding are
rebuilt. First-run setup occurs before application composition, so an absent
provider never creates a partial runtime. Normal Settings routes through a
category screen to separate language/provider detail screens. Presets map
explicitly to wire behavior: OpenAI Responses uses `openai-responses`, while
Compatible Chat and DeepSeek use `openai-chat`. The provider detail screen runs
the same `HttpClientPolicy` resolver before persistence, requires a second
confirmation before deleting metadata plus credentials, and requests a safe
reload afterward. Startup preflight routes an invalid managed default back to
this focused screen with the redacted error and selected profile; explicit CLI
overrides and unmanaged configuration continue to fail at the CLI boundary.
An injected `ProviderCatalog` port gives the detail screen a separate,
user-triggered read-only network boundary. Its HTTPX adapter reuses the draft
`HttpClientPolicy`, sends credentials only in protocol-native headers, and maps
OpenAI-compatible/Responses, Anthropic, and Gemini profiles to their model-list
endpoints. It reads at most one MiB, returns at most 200 unique model IDs, never
renders an error response body, and classifies failures for localized recovery.
Catalog values live only in the current screen; neither credentials nor remote
responses are added to provider metadata. Manual model input remains available
for compatible services without a catalog endpoint.
See [ADR 0046](adr/0046-global-cli-and-managed-provider-settings.md) and
[ADR 0047](adr/0047-recoverable-managed-provider-proxy-settings.md). Read-only
connection discovery is defined by
[ADR 0048](adr/0048-bounded-provider-connection-discovery.md).

An optional positive `context_window_tokens` field records provider/model
capability metadata. It is propagated through redacted profile selection and
failover events for local budgeting, but is never serialized as an API request
parameter. The model endpoint itself enforces its real context limit.

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
provider items. Startup can inspect the sandbox field through an immutable
read-only connection before any database creation, migration, or process
sandbox activation. Schema v5 adds namespaced, foreign-keyed, one-to-one
external session aliases used by protocol adapters; it does not change JSON
export schema version 4. Schema v6 adds a bounded JSON plan column: it is
validated by the domain value, remains outside visible-content search and
session export, is restored before a resumed turn, and is copied only as part
of a durable session fork. Schema v7 adds a foreign-keyed session-task table
for opaque plan-execution lifecycle metadata. A task has one start time and an
optional terminal time; it contains no prompt, command, model output, or
credential, is not included in FTS or export/import, and is deliberately not
copied by a fork. Schema v8 adds a foreign-keyed `session_plan_comments` table
for at most 48 bounded comments scoped to the canonical fingerprint of the
current plan. It is neither indexed nor exported/imported; it is copied on a
plan-bearing fork with fresh opaque IDs and deleted when its plan is replaced
or cleared. Schema v9 adds an optional immutable plan snapshot to each
plan-execution task. The snapshot identifies the exact structured revision
handed off, remains outside FTS and export/import, and is deliberately not
copied by a fork; `/tasks` shows only its short fingerprint and completed-step
count. An explicit exact current-session task lookup may render that same stored
snapshot in the TUI as read-only reference, but it never becomes a model input
or task-control operation. Rust sessions are parsed by a separate read-only
adapter. It validates format versions 0 and 1, reads
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
termination uses a bounded TERM-to-KILL sequence. On Windows, a lazy ctypes
platform adapter creates a kill-on-close Job Object before process launch,
passes its borrowed handle through `PROC_THREAD_ATTRIBUTE_JOB_LIST`, and creates
the leader already assigned to the Job. The same `STARTUPINFOEXW` call restricts
inheritance to null input and the selected output-pipe handles through
`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`. Dedicated reader and waiter threads project
the synchronous Win32 handles into the existing `asyncio.StreamReader` and
process-wait contract without a private asyncio transport. Creation, attribute,
pipe, wait, accounting, and closure failures fail closed; no `taskkill`,
suspended-process race, or breakaway fallback weakens host containment.
See [ADR 0021](adr/0021-owned-background-shell-tasks.md),
[ADR 0031](adr/0031-fail-closed-windows-job-objects.md),
[ADR 0033](adr/0033-atomic-windows-job-process-creation.md), and
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md). Model-visible
completion metadata is defined by
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md), and
event-driven multi-task waits by
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md).
