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
`neuro_code.shared.ui_language` now owns the cross-layer `UiLanguage` primitive;
the former `neuro_code.domain.ui_preferences` facade has been removed.
UI preference ports, persistence, TUI, and localized text use the shared owner
without changing language values or persistence behavior.
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
the former `neuro_code.permissions` module is removed; policy is available only
from `neuro_code.application.permissions.policy`.

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

Agent harness behavior currently lives in the explicit canonical submodules of
`neuro_code.application.runtime`: `background_task_reminders`, `agent`,
`conversation`, and the loop/context/tool/finalization modules. Interactive
approval coordination is owned by `neuro_code.application.permissions.broker`;
the former `neuro_code.application.runtime.approval` path is a one-way
compatibility facade.
Profile and interactive-terminal session coordination live in the canonical
`neuro_code.application.sessions` package. Binding-scoped instruction and
skill trackers live in the canonical `neuro_code.application.memory` package.
Read-only session catalog and inspection queries live in
`neuro_code.application.sessions.catalog`; the lifecycle service delegates
those projections without moving session writes or conversation ownership.
The typed single-turn boundary lives in
`neuro_code.application.sessions.turns`; `SessionApplicationService` retains
only the compatibility binding helper, while the turn runner continues to own
locking, persisted context, event delivery, and cancellation.
Shared provider-selection projections live in
`neuro_code.application.providers.contracts`. The profile conversation
controller still owns binding replacement and session selection, while provider
application services and interface/bootstrap consumers use the provider
contract seam. Historical profile and runtime imports remain
identity-preserving compatibility re-exports.
The typed session-binding contract lives in
`neuro_code.application.sessions.binding`. ACP, bootstrap, session application,
and runtime-facing consumers use its `ConversationBinding` and
`ConversationRunner` types; `ProfileConversationController` retains
profile-specific session selection and binding replacement. Historical profile
and runtime imports remain identity-preserving compatibility re-exports.
Immutable session-selection and interaction-policy projections live in
`neuro_code.application.sessions.contracts`. The TUI consumes
`SessionOption`, `SessionSelectionResult`, `ReasoningEffortSelectionResult`,
and `InteractionModeSelectionResult` from this seam, while
`ProfileConversationController` retains selection, policy application, locking,
and binding replacement. Historical profile and runtime imports remain
identity-preserving compatibility re-exports.
Interactive session listing, selection, and rename use the non-owning
`neuro_code.application.sessions.selection.SessionSelectionService` seam. The
profile controller remains the lifecycle owner; the TUI uses the facade for
these operations and retains only a compatibility controller reference for the
existing execution-record projection.
Typed durable session lifecycle commands use the canonical
`neuro_code.application.sessions.lifecycle.SessionLifecycleService` seam.
Runtime session creation, CLI import/rename, and ACP fork/delete consume its
validated request types; the legacy session application service preserves
identity-compatible delegation. Workspace visibility, binding replacement,
turn locking, protocol cleanup, and execution-record projection remain with
their existing owners.
Read-only session-task queries use the canonical
`neuro_code.application.sessions.task_queries.SessionTaskQueryService` seam.
The Runtime and `AgentConversation` consume its validated list/get requests,
while the broad session service keeps identity-compatible delegation for older
callers. Task creation, queueing, state transitions, permissions, execution,
locking, cancellation, and all SessionStore/SQLite writes remain with the
existing conversation/runtime owners.
Read-only session-summary queries use the canonical
`neuro_code.application.sessions.summary.SessionSummaryQueryService` seam.
Session resume, bootstrap configuration, ACP workspace validation, and
session-scoped tool-output artifact reads consume its validated request; the
broad session service keeps identity-compatible delegation for older callers.
Lifecycle writes, event/item reads, schema, transactions, Runtime, Provider,
Finalizer, and wire behavior remain with their existing owners.
Read-only execution-record projections use the canonical
`neuro_code.application.sessions.execution_queries.SessionExecutionQueryService`
seam. The session catalog and conversation resume/reload paths share its
single and bounded bulk requests, while the broad session service keeps
identity-compatible compatibility exports. Execution-record writes, schema,
transactions, Runtime, Provider, Finalizer, TUI, ACP, and wire behavior remain
with their existing owners.
Copied session-event projections use the canonical
`neuro_code.application.sessions.event_queries.SessionEventQueryService` seam.
Session export and session-scoped tool-output artifact reads share its typed
request and immutable outer mapping projection. Event rows remain untrusted
storage data rather than a second domain-event model; event writes, decoding,
transactions, Runtime, Provider, Finalizer, TUI, ACP, and wire behavior remain
with their existing owners.
Existing application consumers also import concrete canonical owners directly:
bootstrap composition and the TUI use `application.providers.service` and the
three `application.workflows.*` modules, while CLI/bootstrap/ACP and CLI
serialization use the concrete session lifecycle, service, and catalog modules.
Aggregate package exports remain compatibility paths; this import convergence
does not create a second implementation or change workflow, locking,
persistence, Runtime, Provider, Finalizer, TUI layout, ACP wire, or session
behavior. Plan/comment/export reads remain with their current owner because no
second production consumer or stable cross-interface contract exists.
The bounded tool-output artifact application boundary is likewise consumed
through `neuro_code.application.tools.service` by CLI, TUI, ACP, bootstrap, and
CLI serialization. The package aggregate is compatibility-only; artifact
handles, session visibility, redaction, byte limits, pruning, permissions,
storage, Runtime, and protocol behavior remain owned by the service and its
ports/adapters.
The development-stage breaking cleanup removed `neuro_code.runtime`; runtime
application behavior is available only from these explicit canonical
submodules. `neuro_code.application.runtime.__init__` currently remains minimal
and provides no aggregate API, and internal production code imports the
canonical submodules directly.

`neuro_code.configuration.app` owns `AppConfig` and `ProviderProfile`, TOML and
CC Switch configuration, environment overrides, routing, managed overlays,
sandbox policy, stored-credential injection, and HTTP proxy policy. The
synchronous managed JSON reader in
`neuro_code.configuration.managed_provider_settings` owns schema, protocol,
and dialect checks, the file-size limit, metadata/credentials merging,
structural validation, and `ManagedProviderSettings` construction.
The provider-settings value objects and persistence contract are owned by
`neuro_code.application.ports.provider_settings`; the former
`neuro_code.domain.provider_settings` facade has been removed. This keeps
configuration and infrastructure consumers on the application port boundary
without changing validation or persistence. `JsonProviderSettingsStore` is
owned by `neuro_code.infrastructure.providers.provider_settings`, including
asynchronous persistence, atomic writes, and POSIX private permissions. It uses
the canonical reader through a private binding.
The removed `neuro_code.config` facade no longer provides a compatibility
import; callers use `neuro_code.configuration.app` directly. `ProviderProfile`
and `AppConfig` replace the removed `ProviderConfig` alias for this boundary.
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
`AgentRuntime`. Its canonical implementation is
`neuro_code.application.sessions.conversation`; the former
`neuro_code.application.runtime.conversation` path remains only as a one-way
compatibility facade. It serializes turns and carries the ordered session
items, session identifier, and provider-origin metadata forward after each
durable commit. Opening an existing conversation validates that its recorded
workspace is the same filesystem location as the requested workspace. The
headless CLI and Textual interface compose the same controller, so resume and
provider replay rules cannot diverge by interface.

On failure or cancellation, `AgentConversation` reloads the canonical ordered
items and provider origin from `SessionStore` before releasing its turn lock.
The next prompt therefore reuses durable state instead of a stale in-memory
prefix. TUI prompts use an explicit pristine-rewind cancellation policy: before
any non-empty model output, completion, or tool activity, the runtime persists
the pre-turn item prefix and reports the rewind on `TURN_FAILED`; the audit event
still records that the user message was submitted. After output or tool activity,
the message remains durable. The TUI restores a safely rewound prompt to the
draft and may buffer up to four explicit follow-ups before the first non-empty
model token; that buffer is presentation state and is not durable context.

## Repository-level AGENTS.md instruction discovery

The pure instruction value objects are owned by
`neuro_code.domain.workspace.instructions`. The former
`neuro_code.domain.instructions` facade has been removed;
the filesystem discovery adapter remains in
`neuro_code.infrastructure.workspace.instructions`. This separates domain
projection values from filesystem side effects without changing the discovery
port or its security limits.

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

The pure skill metadata owner is
`neuro_code.domain.workspace.skills`. The former
`neuro_code.domain.skills` facade has been removed;
filesystem discovery remains in `neuro_code.infrastructure.workspace.skills`,
and the `SkillTool` remains an infrastructure-side read-only body loader.
This keeps parsing, bounded metadata projections, substitutions, fingerprints,
and synthetic-message construction independent from filesystem side effects.

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

The adapter scans `.neuro/skills/`, `.agents/skills/`, and `.claude/skills/`
for `SKILL.md` files, walking up to
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
re-discovered on each model step. The application-memory `SkillTracker` is
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
`domain/workspace/skills.py`. When the body contains no argument tokens but args are
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
scope, the config-directory priority (`.neuro` → `.agents` → `.claude`) still
applies. See
[ADR 0042](adr/0042-user-level-skill-discovery.md) and
[ADR 0044](adr/0044-repository-level-skill-discovery.md).

## Dynamic mid-session skill discovery

The `SkillTracker` maintains a moving target, mirroring the
`InstructionTracker` design. When file-access tools (`read_file`,
`read_files`, `list_dir`, `list_tree`, `grep`, `grep_many`) touch a path,
`check_path()` updates the target so
that `SKILL.md` files from the accessed directory **upward** to the
workspace root (inclusive) are discovered in the next `current_result()`
call. This finds skills located at any nesting depth in the workspace,
not just at the workspace root — for example,
`src/foo/.neuro/skills/commit/SKILL.md` is discovered when the model
reads a file in `src/foo/`.

The adapter walks **upward** from `target` to `workspace_root`
(inclusive), checking each ancestor directory for config dirs. Deeper
ancestors are scanned first so first-seen-wins name deduplication gives
precedence to more specific (deeper) skills over general (root) skills. When `target` is `None`
or equals the workspace root (e.g. CLI inspect, `rediscover_skills`),
the walk degenerates to scanning just the root level.

Sibling subtrees are isolated: switching from `src/foo/` to `src/bar/`
moves the target, and skills from `src/foo/`'s config dirs are no longer
included. The `SkillTracker.check_path()` is called by single and bounded
batch read/list/search tools alongside their existing
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

## Canonical structured filesystem targets

Structured local filesystem tools use one immutable `FilesystemAccessPlan` per
call. The tool adapter extracts every target from the validated tool grammar,
then `resolve_filesystem_access_targets()` canonicalizes each local path before
permission evaluation. Each target records its canonical path, owning primary or
additional workspace root, policy path, operation, existence state, and link-like
component proof. Primary roots use workspace-relative POSIX-style policy paths;
additional roots use absolute canonical policy paths normalized with platform case
rules and forward slashes. The raw spelling remains diagnostic only.

The authority chain is deliberately ordered:

1. Resolve every target once, including all `apply_patch` source and destination
   paths. Existing parents/ancestors are proven for missing create leaves, and
   symlinks, junctions, Windows reparse traversal, parent escapes, and ambiguous
   Windows device/extended/ADS namespaces are rejected.
   Normal drive-absolute and UNC spellings remain eligible only when their
   canonical target is inside a configured workspace root; drive-relative paths
   are rejected before filesystem resolution.
2. `PermissionManager.decide_targets()` evaluates every canonical target
   independently. Explicit deny wins; an unresolved ask denies in headless mode;
   a path-scoped allow is an allowlist. A structured call is allowed only when
   every target is authorized.
3. Tool execution receives the same immutable plan and consumes canonical target
   entries by extraction index. It does not resolve the raw path again. A mixed
   allowed/denied `apply_patch` therefore stops before journaling or mutation.

This contract covers local structured tools: `read_file`, `read_files`, directory
listing, glob/search, `search_replace`, and `apply_patch`. Workspace identity,
permission, sandbox, and execution remain separate decisions; the plan does not
turn arbitrary Bash path interpretation, MCP calls, delegated ACP execution, or
opaque artifact handles into structured filesystem targets. ACP client paths stay
under the separate client authority: Neuro Code performs only lexical session-root
validation and never calls host `Path.resolve()`, existence, or link inspection for
those remote paths. The plan closes the raw-path authority gap at the local
structured tool boundary; it is not a blanket claim of race-free TOCTOU protection
for every process or provider capability.

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
existing `read_file` and `read_files` tools apply only lexical session-root
validation, then delegate the client-owned path and its bounded line range to
`fs/read_text_file`; the host does not resolve or inspect that path and never
falls back to a local operation. The ACP filesystem capability does not expose a
directory walk or search operation, so `list_tree` and `grep_many` retain the
same local workspace semantics as `list_dir` and `grep`. When the client
advertises both text read and write, `search_replace`
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
and task ID. Each local tool call retains stable call-ID state, while the TUI
projects consecutive calls as one activity group. The group is collapsed by
default, including edits, and summarizes state, bounded intent or aggregate
counts, key failure text, and elapsed time. Enter or click opens a fixed-height
Inline Peek for one selected call; Up/Down selects another call, Enter opens its
independent Tool Inspector, and Escape returns to the stable Summary. Clicking
an open Peek collapses it, and an app-level fallback preserves Escape collapse
after focus moves. While Inspector is open, live lifecycle events update the
selected presentation and target Conversation widgets through the persistent
base screen rather than the current modal. Running timers refresh each activity
group at most once per tick and skip open Peek/Inspector layouts. The Peek's
ten-logical-line presenter budget is backed by a twelve-row widget maximum so
terminal wrapping cannot grow Conversation without bound. Long Bash intent is
truncated, normal allow decisions remain out of Summary/Peek, and completion is
represented once by its check mark. See
[ADR 0014](adr/0014-minimal-event-stream-tui.md) and
[ADR 0029](adr/0029-auditable-in-place-tool-cards.md), with the presentation
refinement in [ADR 0108](adr/0108-editorial-tui-presentation.md).

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
values use literal `Text`. Conversation messages and local system, status,
activity, plan, and error records share one left reading axis and a 116-column
maximum; labels remain inline rather than reserving a fixed gutter. Semantic
hierarchy—not an object's type alone—selects restrained foregrounds, the single
interaction accent, and success/warning/error colors. Tool output and diffs are
literal application-styled text, never payload markup. Metadata-first Tool
Activity renderers project tree, grep, file-read, Bash, and generic previews;
formatted stdout is only a bounded fallback. Conversation never renders an
artifact or full tool output. The independent Inspector exposes scrollable and
copyable Output/Input/Meta documents, recursively redacts Input, allowlists
Meta, and only then lazily reads session-scoped output artifacts through the
existing 256 KiB, redacted, session-owned application boundary. Read/storage
truncation is explicit. Transcript Copy always projects the stable Activity
Summary, as specified by [ADR 0030](adr/0030-bounded-interactive-tool-card-details.md)
and [ADR 0067](adr/0067-tui-bounded-tool-output-details.md). Mermaid and media
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

The presentation adapter owns one compact neutral-dark semantic theme instead of
exposing Textual's unrelated theme and command-palette surfaces. Three background
levels, one border, three foreground levels, one restrained interaction accent,
semantic outcome colors, and shared spacing values define its hierarchy. The
built-in palette is disabled, provider and session discovery use the explicit
application commands, and session queries are rendered as literal plain text.
Below the prompt, one label-free runtime row keeps model, effort, and mode in a
bounded left region and context usage plus compact working path in a bounded
right region. Long model and path values ellipsize in narrow terminals. It
updates from controller state on localization, profile failover, and selection
rather than scraping transcript messages. The permanent shortcut row is
omitted; `/help` and F1 show the existing command reference on demand. A pure
collapsing-pulse state machine is advanced by a Textual timer
and rendered before the pending-assistant text only while waiting for model
output. Context
starts with a provider-neutral estimate over canonical
session items. Each model completion with token metadata emits
`CONTEXT_USAGE_UPDATED`, replacing that estimate with the provider-reported
input plus output count. The denominator is explicit profile metadata named
`context_window_tokens`; an absent value leaves only the known token-use count
visible rather than inventing a percentage. Managed-provider metadata exposes
the same positive field per profile.

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
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md). Neuro Code
validates this process-boundary shape with its own native terminal tests.

Above the native adapters, the application session owner
`neuro_code.application.sessions.terminal_sessions` implements the shared
`InteractiveTerminalManager` port. Creation crosses permission,
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
workspace changes, and terminal status retain their bounded call-ID state inside
the TUI's consecutive activity-group projection. For a side-effecting local
tool, the runtime compares bounded read-only
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

Ordered persisted conversation items have a dedicated application read owner,
`neuro_code.application.sessions.item_queries`. Session resume/reload and
explicit session export share its typed request and tuple projection; the
legacy session facade keeps identity-preserving compatibility exports. The
owner does not absorb plan, comment, lifecycle, event, or storage transaction
responsibilities.

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
semantics; a missing or legacy no-snapshot task reports no detail. The explicit
`/schedule-plan`/`/queue-plan` command stores at most four queued plan snapshots
per session without contacting the model. `/run-task TASK_ID` claims one queued
snapshot atomically through `SessionStore.start_session_task`, then reuses the
same plan-execution lifecycle as `/execute-plan`; queued tasks never auto-start,
retry, wake, or spawn subagents. There is deliberately no plan-file write or
subagent lifecycle in this slice. Current-plan comments are an intentionally
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
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md), plus
[ADR 0063](adr/0063-bounded-explicit-plan-task-scheduling.md).

Stage5CQ adds an explicit, bounded `SubagentExecutionService` application
workflow. It creates a metadata-only `SUBAGENT` session task before invoking an
injected `SubagentExecutor`, records exactly one terminal state, and preserves
the executor's result, failure, or cancellation. The request is bounded and
does not contain parent messages, tools, credentials, or output. The executor
must build a fresh child runtime/context; this service never reuses the parent
conversation. There is no queue, retry, automatic scheduler, ACP method, CLI
command, or TUI command in this slice. See
[ADR 0071](adr/0071-explicit-bounded-subagent-lifecycle.md).

Stage5CR adds the first concrete isolated read-only runtime behind that seam.
`IsolatedSubagentExecutionService` creates a fresh child session, persists a
metadata-only `SubagentLink` before execution, removes provider builtin tools,
and restricts the child registry to `read_file`, `read_files`, `list_dir`,
`list_tree`, `grep`, `grep_many`, and `skill`. Child steps and wall-clock execution are bounded, cancellation closes
the child, and parent deletion recursively removes linked child sessions. This
slice remains explicit and synchronous: it does not alter the normal
`AgentRuntime` loop, reuse parent context, expose CLI/TUI/ACP entrypoints, or
schedule/retry/recursively spawn children. See
[ADR 0072](adr/0072-isolated-read-only-subagent-runtime.md).

Stage5CS adds `ReadOnlySubagentApplicationService` as the narrow caller
boundary for that runtime.  It requires the persisted parent/child link and
projects the child run into a redacted, UTF-8-bounded `SubagentResultProjection`
containing only lifecycle IDs, terminal status, step count, optional typed
outcome, and response text.  Messages, events, tool arguments, credentials,
and raw child context do not cross this boundary.  The projection is returned
in memory only; it is not appended to the parent transcript or persisted as a
second result record.  See [ADR 0073](adr/0073-bounded-read-only-subagent-result-projection.md).

Stage5CT adds a read-only parent/child relationship query boundary through
`SubagentRelationshipQueryService`.  It projects existing `SubagentLink`,
`SessionTask`, and child-session summary records into a bounded
`SubagentRelationshipProjection` containing only lifecycle IDs, task status,
provider/model labels, timestamps, and capability labels for `resume`, `fork`,
and `delete`.  Active child tasks expose no lifecycle action labels; terminal
tasks expose labels only, while the existing lifecycle services remain the
owners of mutation and execution.  The query never loads messages, events,
tool output, prompts, credentials, or raw child context, adds no schema, and
does not create a CLI, TUI, ACP, scheduler, replay, or automatic-resume path.
See [ADR 0074](adr/0074-read-only-parent-child-subagent-relationship-projection.md).

Stage5CU adds one explicit CLI entry,
`neuro subagent --parent-session SESSION_ID PROMPT`, over the existing
composition-owned read-only subagent application service. The command performs
parent-session resume preflight, runs one fresh bounded child with the fixed
read-only capability set, and emits only the redacted
`SubagentResultProjection` (plain response or stable `--json` fields). It does
not reuse parent context, schedule/retry/recursively spawn, or add TUI/ACP
entrypoints. See [ADR 0075](adr/0075-explicit-cli-read-only-subagent-entry.md).

Stage5CV adds the explicit private ACP extension
`_neuro-code/session/subagent`. It accepts only an external session ID, a
bounded prompt, and a bounded step limit, resolves the parent through the
existing ACP alias boundary, and invokes the same composition-owned read-only
application service as the CLI. Its response omits internal IDs and child
transcript details, returning only bounded response/status/steps/truncation
and typed outcome fields. It is not a standard ACP capability and does not
add scheduling, retry, recursion, parallel children, or write-capable tools.
See [ADR 0076](adr/0076-explicit-acp-read-only-subagent-extension.md).

Stage5CW adds an explicit TUI `/subagent PROMPT` command. The TUI receives the
same composition-owned `ReadOnlySubagentApplicationService` used by CLI and
ACP, refuses to start without a current session or while another turn is
running, and renders only the bounded response and step/status metadata. The
child remains read-only, isolated, synchronous, and cancellable; its prompt,
events, internal IDs, and temporary context are not appended to the parent
transcript.

See [ADR 0077](adr/0077-explicit-tui-read-only-subagent-command.md).

Stage5CX adds an explicit TUI `/subagents` read-only view over the existing
`SubagentRelationshipQueryService`. It displays only bounded parent-task and
child-session identifiers, provider/model labels, task status, timestamps, and
capability labels; it never executes resume, fork, or delete and never loads
child transcript, prompt, tool arguments, or output. Missing sessions,
unavailable services, and empty relationships fail closed without starting a
model turn. See [ADR 0078](adr/0078-explicit-tui-subagent-relationship-view.md).

Stage5CY adds `SubagentRelationshipLifecycleService` as the application owner
for explicit `resume`, `fork`, and `delete` actions. It validates the
parent-owned relationship and terminal `SUBAGENT` task before delegating to the
existing session lifecycle service. Resume returns only a validated child
selection and does not run a model; fork returns a new session ID without
opening it; delete targets only the child session. The TUI exposes these
actions as `/subagents ACTION TASK_ID`, never accesses SQLite, and keeps
validation separate from mutation without claiming cross-process atomicity.
See [ADR 0079](adr/0079-explicit-subagent-lifecycle-actions.md).

Stage5CZ exposes the same lifecycle owner through the bounded headless command
`neuro subagents ACTION TASK_ID --parent-session SESSION_ID`. The CLI validates
the parent through the composition resume boundary and delegates the typed
application request; it never starts a model turn, replays tools, or reads
SQLite directly. Plain output is a short lifecycle message, while `--json`
contains only bounded lifecycle identifiers, the canonical action, and an
optional forked-session ID. See
[ADR 0080](adr/0080-explicit-cli-subagent-lifecycle-actions.md).

Stage5DA exposes the same owner through the private ACP extension
`_neuro-code/session/subagents`. Its strict request contains only an external
parent session alias, a bounded parent task ID, and one of `resume`, `fork`, or
`delete`. Resume and fork return external ACP aliases rather than internal
session IDs; delete returns only a bounded deleted flag. The adapter never
starts a model turn, replays tools, exposes child context, or claims alias
allocation and lifecycle mutation are one transaction. See
[ADR 0081](adr/0081-explicit-acp-subagent-lifecycle-extension.md).

Stage5DB hardens that response boundary. The ACP adapter verifies that a
lifecycle owner returned the same parent session, parent task, and action that
were requested, and the serializer validates non-delete external aliases for
bounded UTF-8 size and control characters. Invalid owner results or aliases
fail closed without changing valid wire responses. See
[ADR 0082](adr/0082-fail-closed-acp-subagent-lifecycle-projection.md).

## Subagent capability closure

The canonical parent authority for every production child-runtime creation
path is the actual `ConversationBinding.capabilities` manifest. The headless
CLI opens a parent binding before starting its explicit child; TUI reads the
active binding; and the private ACP child extension requires an active parent
binding. Missing metadata fails closed. The composition-owned global policy is
shared by the scheduler and explicit service.

The explicit read-only workflow treats
`READ_ONLY_SUBAGENT_TOOL_NAMES` as a requested capability only. It resolves
`parent ∩ requested ∩ global_policy` through
`SubagentCapabilitySet.resolve_child()` before creating the child task or
binding, passes that exact manifest into the factory and
`ApplicationComposition.create_binding(capabilities=...)`, and verifies the
runtime fingerprint. This prevents a restricted child from regaining root
workspace roots, tools, sandbox strength, MCP, terminal, or network authority.
The legacy arbitrary `SubagentExecutor` binding remains only as a marked
test/internal compatibility seam and is rejected by the normal composition
boundary. Subagent relationship `resume`, `fork`, and `delete` do not recreate
a runtime; a normal ACP fork is an independent session binding. This closure
proves only `child capability <= actual parent capability`, not the complete
permission, workspace, sandbox, MCP, provider-transport, or agent-security
system. See [ADR 0125](adr/0125-subagent-capability-closure.md).

`ProfileConversationController` in
`neuro_code.application.sessions.profile_conversation` wraps the active
`AgentConversation` for the interactive composition. The former
`neuro_code.application.runtime.profile_conversation` path is a compatibility
facade. It serializes selection with turns and exposes only
redacted `ProviderOption` data to the TUI. Selecting a different configured
profile composes a new provider/runtime/conversation binding with no resumed
session; the old SQLite session remains untouched. This strict boundary avoids
cross-provider replay of encrypted reasoning, hosted-tool state, dialect
metadata, and profile-affine context. See
[ADR 0017](adr/0017-safe-interactive-profile-selection.md).

The controller also owns one process-local `ReasoningEffort` selection and
serializes changes with turns. It reapplies the requested value whenever a
profile or session replacement installs a new conversation binding. `low`,
`medium`, `high`, `xhigh`, and `max` map to application review guidance;
`max` is the deepest ordinary single-agent policy, and `ultracode` has an
explicit effective value of `max` until workflow orchestration exists. The TUI
exposes the selection through `Ctrl+E`, `/effort`, and `/reasoning`; the CLI
exposes `--effort`. Selection does not rewrite provider configuration or session
identity.

At each model step, `AgentRuntime` adds the selected guidance to a request-only
system message and places the typed requested value on `ModelContext`. The
guidance is not added to canonical `SessionItem` history. Provider adapters may
inspect the typed value. The explicit Kimi K3 and GLM 5.3/5.2 dialect mappings
send the configured native `max` field for `max`; other dialects omit a native
effort field while retaining the application guidance. See
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
- `LocalProcessSandbox`: owns every model-controlled local child boundary,
  including pipe-based commands, stdio MCP, and local PTY/ConPTY sessions;
  terminal callers submit a typed `SandboxedProcessRequest` rather than
  invoking a platform spawn adapter directly.
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
DeepSeek V4's DSML tool-call stream is an explicit `openai-chat` dialect selected
through `dialect = "deepseek-v4"`; it is never inferred from a provider name,
model name, or hostname.
The generic Responses adapter is implemented at
`neuro_code.infrastructure.providers.openai_responses.OpenAIResponsesProvider`; xAI behavior
is selected through `dialect = "xai"`, not through a separate Python provider
class. The development-stage breaking cleanup removed
`neuro_code.providers.xai_responses` and `XAIResponsesProvider`; Architecture
Freeze v1 then removed the obsolete `neuro_code.providers` package and its
provider submodule facades. ADR 0072 records that import-boundary decision.
Credentials are environment references or a validated loopback-proxy
placeholder for manual TOML profiles. The TUI additionally uses a
`ProviderSettingsStore` port for user-managed profiles. Its JSON adapter writes
non-secret metadata and credentials to separate atomic owner-private files;
one global proxy default plus optional per-profile overrides are non-secret metadata,
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
explicitly to wire behavior: OpenAI Responses uses `openai-responses`, Compatible
Chat uses `openai-chat` with the standard dialect, and DeepSeek uses `openai-chat`
with `dialect = "deepseek-v4"`. The provider detail screen runs
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
The request, bounded-result, classified-error, and port types are owned by
`neuro_code.application.ports.provider_catalog`; the former
`neuro_code.domain.provider_catalog` facade has been removed. There is no
second implementation.
See [ADR 0046](adr/0046-global-cli-and-managed-provider-settings.md) and
[ADR 0047](adr/0047-recoverable-managed-provider-proxy-settings.md). Read-only
connection discovery is defined by
[ADR 0048](adr/0048-bounded-provider-connection-discovery.md).

The managed provider value objects (`ManagedProviderProfile`,
`ManagedProviderSettings`, and `ManagedProxyPolicy`) and the
`ProviderSettingsStore` contract are canonical at
`neuro_code.application.ports.provider_settings`. The historical
`neuro_code.domain.provider_settings` facade has been removed; it did not
contain a second implementation. See ADR 0074.

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

Provider transport and protocol failures cross a typed boundary before they
reach resilience. `ProviderFailure` in `shared.errors` is an immutable,
redacted, bounded fact containing kind, safe detail, optional status/
`Retry-After`, provider/model identity, lifecycle phase, and evidence origin
(`provider`, `transport`, `local`, or `unknown`). It never contains retry,
circuit, or failover decisions. The five model HTTP adapters use a conservative
generic HTTP fallback and then classify exact provider-owned structured fields;
generic 404 is not asserted to be a missing model, generic 429 is not made
retryable without an explicit rate code, and generic 413 is an invalid request.
Timeout/network failures are transport facts, malformed provider streams are
protocol facts, and unexpected non-transport runtime failures are local facts.
`ConfigurationError` remains separate, and cancellation propagates unchanged.

`ProviderFailurePolicy` owns retry, circuit, and failover independently. Server,
timeout, and network facts are transient circuit inputs; an unambiguous rate
limit may retry or isolate a candidate without marking it unhealthy; permanent
request, authentication, authorization, model, and context failures do not
poison the transient circuit. Provider/transport unknown facts do not retry or
count toward the circuit but may fail over before output; local unknown facts
stop at the current candidate. Invalid requests do not fail over, while
protocol failures use their explicit conservative policy. After the first model
event, both retry and failover are disabled. `consecutive_failures` means the
number of consecutive pre-output circuit-eligible failures since the last
success or circuit-ineligible failure. `ProviderHealth.last_failure_kind` and
the optional `failure_kind`/`status_code` fields on attempt events expose
stable bounded facts while retaining `last_error_type` and the original event
fields for compatibility. The protocol-owned Anthropic `rate_limit_error` and
Gemini Generate Content `RESOURCE_EXHAUSTED` envelopes are explicit rate-limit
facts; Anthropic `billing_error` remains authorization, and an unstructured or
future generic 429 remains unknown. Offline fixtures cover the listed official
envelopes; this does not claim full provider compatibility or live validation.
See [ADR 0126](adr/0126-provider-typed-failure-taxonomy.md).

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
- Each enabled local child is preflighted by its `LocalProcessSandbox` launcher;
  there is no controller-wide activation marker or mount attestation. The
  launcher still validates its trusted helper, explicit mounts, private state,
  and `strict` allowlist-root filesystem before exposing a child.
- Enabled Linux children use a PID namespace as the descendant lifecycle
  boundary, so `setsid()` cannot escape timeout, cancellation, or shutdown.
  The explicit POSIX `off` profile provides only original-process-group cleanup
  and no filesystem, network, controller-state, or arbitrary-descendant isolation.
- The process-creation architecture guard audits built-in production code. Same-process
  Python extensions (`additional_tools`, injected executors, and future plugins)
  run with controller authority and are trusted; an untrusted plugin requires a
  separate process/capability boundary.
- `read-only` removes and independently rejects the workspace edit tool.
  `read-only` and `strict` local-process descendants run without the parent agent's
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
or task-control operation. Schema v10 adds one foreign-keyed last-safe-terminal execution record
per source session. It must reference an already-persisted `TURN_COMPLETED`
event and retains only the typed status, reason code, finalized/recoverable
flags, event sequence, and timestamp. It deliberately excludes prompts, tool
arguments/results, evidence, workspace diffs, supervisor snapshots, FTS,
export/import, and fork copying. A later successful ordinary completion
replaces a prior recoverable terminal result, so resume can safely distinguish
the latest completed turn from a paused one without treating it as replayable
model context. Runtime terminal success paths use the typed
`SessionStore.finalize_turn` boundary: the `TURN_COMPLETED` event, final
append-only ordered session items, synchronized title/FTS projection, and an
optional user-turn execution record are committed together in one short SQLite
transaction under the store write lock. Background auto-wake passes no record,
so it cannot replace a prior user execution record. This boundary does not
make earlier turn events, provider/tool work, or cross-process runtime actions
atomic. Within the execution-record boundary, SQLite serializes writes and
rejects older event sequences or conflicting data for the same sequence, so a
stale process cannot overwrite a newer terminal result. Schema v12 adds the
foreign-keyed `subagent_links` table. Each link stores only the parent session
ID, parent `SUBAGENT` task ID, child session ID, and creation timestamp; the
child ID is unique and parent deletion recursively removes linked children.
Saving a link is its own short SQLite transaction and validates that the parent
task is running and the child session exists. This does not make child creation,
model execution, task completion, and session events one transaction. Schema
v13 adds the foreign-keyed `session_compaction_items` table. A row keeps
only bounded provider/window metadata, source counts and a half-open candidate
range, an opaque source fingerprint, summary token metadata, a timezone-aware
timestamp, and an already-redacted bounded summary. It is excluded from FTS,
session export/import, and fork copying; deleting a session cascades to its
rows. Saving the same ID with identical data is idempotent, while conflicting
IDs or duplicate source ranges fail closed. `CompactionResumeRebuilder` applies
only non-overlapping records whose source count, provider origin, and
fingerprint match the current context, producing transient synthetic summary
messages. It does not run a provider, replay tools, mutate storage, or claim
whole-turn atomicity. Existing sessions without rows resume unchanged. Rust
sessions are parsed by a separate read-only adapter. It validates format
versions 0 and 1, reads
bounded JSONL records, converts supported legacy/current records into an
ordered `SessionSnapshot`, and reports corrupt or unsupported records instead
of silently inventing content. The SQLite adapter inserts that snapshot in one
transaction and preserves its ID, workspace, model, and timestamps; an existing
ID fails without mutation. Resume authorization compares the recorded and
requested workspaces by filesystem identity, with canonical normalized paths as
a fallback, so platform aliases are accepted without admitting a different
workspace. Source session files are never opened for writing.

## Durable turn crash recovery

Each persisted `AgentRuntime.run()` allocates a unique opaque `turn_id` and
accepts a small row in `session_turn_attempts` before a provider request or
tool body can start. The row is the canonical recovery index; the ordered
`events` table remains append-only audit evidence. Recovery facts and their
events are written together, so restart classification is derived from sticky
facts rather than from the absence of an event.
For plan execution, acceptance and task ownership are one SQLite transaction.
A new `RUNNING` plan task is inserted with the exact `attempt.task_id`, or the
exact `QUEUED` task is validated and transitioned to `RUNNING` with that same
identity. The recovery projection never infers ownership from the latest task,
an input fingerprint, or an event. If the transaction fails, neither the
attempt nor the task activation is visible.

The write-ahead boundaries are explicit. `MODEL_REQUEST_STARTED` is committed
before entering the Provider stream. On the first observable text, reasoning,
backend-tool, tool-call, or completion event, `MODEL_OUTPUT_STARTED` is
committed before the event is handled. `TOOL_STARTED` is committed before the
tool body and records whether the tool is side-effecting. Provider request
bodies, headers, credentials, full context, tool arguments, and unbounded
outputs are not copied into this recovery index.

The existing `SessionStore.finalize_turn()` and
`finalize_turn_with_compaction()` transaction is the only `COMMITTED` point:
completion event, final session items, title/search projection, optional
execution record, task terminalization, and attempt resolution share the same
SQLite transaction. Failure and cancellation use the corresponding atomic
terminalization path. A normal `FAILED` or `CANCELLED` attempt is execution
history, not an orphaned crash attempt, and is excluded from recovery UX. The
default recovery inspect view is unresolved/open only; committed and explicitly
abandoned history is available through an audit-specific view.

The derived statuses are `COMMITTED`, `SAFELY_RETRYABLE`, `INDETERMINATE`, and
`ABANDONED`. Exact user-owned input is stored only when it is reconstructable
and at most 256 KiB; background wake input is deliberately not reconstructable.
An open attempt with no observable output, no tool start, exact input, and no
fact conflict may be explicitly retried when it is a non-plan user attempt.
Observable output, any tool start, possible side effects, missing input,
background wake, or contradictory facts are `INDETERMINATE` and never
auto-replayed. Plan attempts may remain `SAFELY_RETRYABLE` when no output or
tool effect was observed, but `retry_available` is false because plan execution
retry is unsupported; their explicit recovery action is abandon. `ABANDONED`
is written only by an explicit recovery action. A retry abandons the old
attempt and creates a new turn identity rather than continuing the old one.

CLI and TUI expose bounded recovery metadata and explicit `inspect`, `retry`,
and `abandon` operations. For a linked `RUNNING` plan task, abandon atomically
transitions the task to `CANCELLED` and writes `SESSION_TASK_CANCELLED` before
`TURN_ABANDONED`; ordinary user attempts without a task keep the existing path.
ACP exposes the same application service through
the private `neuro-code/session/recovery` extension and returns machine-readable
bounded projections. Resume blocks a new turn while an unresolved attempt is
present. This layer does not implement mid-turn continuation, tool
compensation, background-child reconciliation, plan execution retry, or
workspace rollback. In particular, `EXECUTION_SEGMENT_CHECKPOINTED` remains a
progress/audit marker: crash recovery is not a workspace rollback point.

See [ADR 0127](adr/0127-durable-turn-crash-recovery.md).

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

## Explicit serialized writable-subagent workspace

The existing `/subagent` capability remains read-only. The first writable
subagent is a separate, explicit internal vertical slice constructed by
`ApplicationComposition.create_writable_subagent_service()`. It is not wired
to `/subagent`, CLI, TUI, ACP, or automatic delegation, and it does not start
checkpoint/rollback orchestration. The standalone service remains serialized;
only the bounded Task DAG may create separate worker services through its
typed factory.

`WritableSubagentApplicationService` serializes one child at a time. It first
records an `ALLOCATING` lease, reads the parent's exact committed HEAD, creates
a Neuro-owned managed branch worktree outside the parent's workspace roots, and
captures a `READY` baseline checkpoint. Only then does it derive the typed
`ManagedChildWorkspaceGrant`, whose fingerprint binds the parent capability,
repository identity, exact base SHA, immutable `WorktreeHandle`, managed
worktree ID/path, creation time, and baseline checkpoint. The child receives a
fresh session and binding with exactly that worktree as cwd and sole root.

The derived child has only the bounded read set (`read_file`, `read_files`,
`list_dir`, `list_tree`, `glob`, `grep`, `grep_many`, `skill`, and optional
read-only `lsp`) plus `search_replace` and `apply_patch`. `lsp` is present only
in the actual-parent/global/worker-policy intersection. The child has no Bash,
terminal, background, MCP, network, Git/worktree/checkpoint/rollback, or
subagent authority. Parent and global policy must both prove write tools, write
authority, and a writable sandbox. Generic
`SubagentCapabilitySet.is_subset_of()` is unchanged; the typed grant is the
narrow boundary that binds the child to a new managed workspace. The normal
Permission, canonical filesystem-target, execution, and sandbox pipeline
remains active for every child write.

The durable lease uses `ALLOCATING`, `WORKTREE_READY`, `BASELINE_READY`,
`ACTIVE`, `PRESERVED`, `ORPHANED`, and `FAILED`, with immutable identity,
insert-only ownership, and generation CAS. Success, provider failure,
cancellation, or uncertain final inspection preserve the worktree and
baseline; there is no automatic removal, rollback, merge, commit, copy-back,
or cleanup. Reconciliation verifies worktree and checkpoint evidence after a
crash without deleting uncertain data. The bounded result projection exposes
only lifecycle/workspace identities, redacted response, bounded outcome and
fingerprints, not a diff, transcript, raw arguments, or file contents. The
composition root captures parent authority from the actual active
`ConversationBinding`, including its runner session ID and capability
fingerprint; a caller-reported parent manifest is not trusted, and a request
whose parent ID differs from the binding is rejected before allocation.
Session-store schema 16 rebuilds and preserves schema-15 lease rows, uses
`RESTRICT` for both lease session foreign keys, and refuses recursive session
deletion whenever any session in the deletion closure is referenced by a
writable lease. Shared owner liveness uses a real POSIX probe or a Windows
process-handle wait and treats unproven access/API failures as alive. The
complete contract is in [ADR 0131](adr/0131-managed-writable-subagent-workspace.md).

### Worker-scoped read-only LSP runtime

The managed grant derives a `WorktreeWorkspaceBinding` from its immutable
handle. Writable runtime composition rejects unless the binding cwd, effective
capability cwd, workspace-binding primary root, LSP manager root, and canonical
managed child root are equal and additional roots are empty. The existing
per-binding instruction and skill trackers therefore also discover only from
the managed child root.

Each worker keeps the existing per-binding `LanguageServerManager`; managers,
clients, routes, document/diagnostics caches, versions, and restart counters
are not shared with the parent or another worker. The manager re-reads the
canonical child document before a semantic request, so an explicit
`search_replace`/`apply_patch` write is followed by `didOpen` or versioned
`didChange` using the new child bytes. Input paths and server-returned URIs
still pass through the LSP canonical target and visibility boundary.

`ConversationBindingResourceScope` gives the binding one idempotent,
cancellation-safe asynchronous close task. Worker success, provider failure,
cancellation, and timeout close the LSP client/process and release its caches;
application shutdown remains a fallback for still-open bindings. Worktree,
checkpoint, lease, and session evidence remain durable and preserved. LSP
process/cache state is ephemeral, is not stored in SQLite, and is reconstructed
by a future binding. See
[ADR 0132](adr/0132-worker-scoped-lsp-runtime-integration.md).

### Bounded parent context relay

The writable workflow now derives one `ParentContextRelay` only from the
durable items of the session bound to the actual parent `ConversationBinding`.
It deterministically selects recent genuine plain-text USER/ASSISTANT content,
applies the composition-owned configured redaction, and enforces 10-item,
4-KiB-per-item, 24-KiB-projected, and 32-KiB-rendered UTF-8 bounds. System,
tool-role, synthetic, tool-call-bearing, media-bearing, preserved reasoning,
and preserved backend-call structures are excluded; assistant visible prose is
separable from and never carries its `reasoning_content`.

Session schema 23 retains the schema-17 one-to-one insert-only READY relay per
writable lease, the durable Task DAG tables described below, the schema-20
predecessor-result relay table, and the schema-21 Task DAG recovery-claim
fence; schema 22 adds bounded DAG capacity and scoped Writable lease policy,
and schema 23 adds per-node execution-owner identity. It also retains the durable Leader
attempt/decision projections described after the DAG. Its
identity binds the parent/task/child, lease, worktree, baseline checkpoint,
base commit, capability/grant fingerprints, and child-task digest. Source,
content, and complete-record fingerprints are verified on load; inconsistent
rows fail closed. Projection and durable verification happen after the
`SubagentLink` and before child runtime creation, so no child model request can
occur before relay publication. Failure preserves the existing durable worker
identities rather than deleting or rolling back them.

`ContextBuilder` injects exactly one immutable
`SyntheticReason.PARENT_RELAY` USER message after project instructions and
skills and before genuine child history on every model request. It is not
stored as child conversation history and remains byte-stable across model,
tool, and LSP steps. Relay strings are evidence only: they are not parsed into
tools, roots, sandbox, network, LSP, worktree, or checkpoint authority. The
existing capability intersection and child-root instruction/skill discovery
remain the sole authority owners. Durable compaction-summary reuse, live
context sharing, unbounded parallel workers, Swarm/Ultracode orchestration, and automatic
delegation remain absent. The bounded Task DAG and serialized Leader slices are
specified separately by [ADR 0134](adr/0134-durable-serialized-task-dag.md).
See [ADR 0133](adr/0133-bounded-parent-context-relay.md) for the relay
boundary.

### Bounded durable Task DAG

ADR 0134 adds an explicit internal orchestration boundary for one bounded DAG
whose node definitions are supplied by the caller as typed values. The first
slice admits at most eight nodes, sixteen dependency edges, four dependencies
per node, and only `WRITABLE_SUBAGENT` nodes. Definition validation rejects
unknown references, duplicate edges, self-dependencies, and cycles before
publication. Topological order and ready-node selection are deterministic by
declaration ordinal and node ID; dependency edges are control-only and never
forward predecessor prompts, transcripts, tool output, or workspace data.

The DAG service derives the parent session only from the actual
`ConversationBinding`. It reuses the existing `SessionTask` owner and the
existing `WritableSubagentApplicationService` for every node. `max_parallel`
is immutable, defaults to one, and is bounded by the shared application limit
of four. Before a worker starts, the node durably records one generated parent
task ID plus an execution owner PID/token. A single `BEGIN IMMEDIATE`
transaction counts durable `RUNNING` node rows, checks capacity, and performs
the exact generation CAS from `READY` to `RUNNING`. Ready selection is
ordinal/node-ID deterministic. The DAG uses a structured `TaskGroup`, never
`SubagentScheduler.run_many()` or an unbounded gather over nodes.

The canonical active execution model is the set of node rows with
`state=RUNNING`. The legacy `task_dags.active_node_id` column is a compatibility
projection only: it is populated only when exactly one node is running and is
never used for scheduling or capacity. A live per-node owner PID is observed
during the short pre-evidence allocation window; only a dead owner enters
per-node crash classification.

Parallel nodes receive fresh Writable application services from a typed
`TaskDagWritableWorkerFactory`. This preserves the frozen per-worker
`asyncio.Lock` while giving each node independent binding, lease, worktree,
checkpoint, child session, Parent Relay, and worker-scoped LSP state.

Session schema 23 stores immutable DAG definitions and bounded node runtime
projections in `task_dags` and `task_dag_nodes`, plus insert-only
`task_dag_dependency_relays` and the separate `task_dag_recovery_claims`
cross-process ownership fence. Definitions and relay publications are
insert-only; graph and node lifecycle updates use generation CAS. A successful
node records the exact worker task, child session, writable lease, worktree,
baseline checkpoint, Parent Relay, and bounded result projection. A missing or
inconsistent success correlation is not treated as success.

The three orchestration context channels remain separate. The Parent Context
Relay carries a bounded parent-session snapshot into one child. The DAG
predecessor-result relay carries only completed direct-predecessor projections
into the dependent worker. The Leader evidence envelope carries bounded DAG
state into the zero-tool Leader. A root node receives no predecessor relay;
for a dependent node, the relay follows the declaration order of its direct
edges and is published after the node's exact `RUNNING` generation claim but
before child runtime or provider execution. Each entry is bound to the exact
predecessor generation, parent task, child session, preserved writable lease,
worktree, baseline checkpoint, and Parent Relay. The relay is limited to a
4-KiB UTF-8 result per predecessor, 16 KiB of aggregate source content, and a
24-KiB rendered message. It contains redacted result text and opaque
fingerprints only: no transcript, reasoning, tool calls, workspace bytes,
capability grants, paths, or authority instructions cross the edge. Missing,
stale, tampered, non-completed, or mismatched evidence fails closed before a
worker request; an exact duplicate publication is idempotent, while a
different publication for the same target generation is rejected.

The lifecycle is `PENDING -> READY -> RUNNING -> COMPLETED/FAILED/CANCELLED/
INDETERMINATE`; dependency-blocked descendants become `SKIPPED`. A failed or
cancelled dependency blocks only its descendants while independent branches
continue. Restart reconciliation is non-worker-starting and classifies an
active node as `ACTIVE_WORKER`, `SAFE_NOT_STARTED`, `RECOVERY_OWNED`, or
`INDETERMINATE`.
`SAFE_NOT_STARTED` requires the exact active `RUNNING` node and `parent_task_id`,
the same READY relay loaded by DAG/target/generation with exact definitions,
direct dependencies, and fingerprints, no matching `SessionTask`, writable
lease, or subagent link, and no live recovery owner. A later DAG step first
acquires the exact durable claim; only the winner may call Writable.
`RECOVERY_OWNED` is the read-only classification for a live or unproven claim
owner, including the partial window where a lease exists but `SessionTask` does
not. The loser performs no provider/resource allocation and does not write
`FAILED` or `INDETERMINATE`. A dead owner proven before the first lease insert
may be replaced by version CAS with the same generation, parent task, and relay
identity. After lease ownership begins, existing Writable reconciliation is
fail-closed and never automatically reruns the worker. Missing relay, stale
identity, or other uncertainty remains `INDETERMINATE` and is never
automatically rerun.
Completed/failed/cancelled worker tasks map to the same DAG terminal meaning.
No automatic retry, crash rerun, merge, copy-back, rollback, cleanup, dynamic
or unbounded dataflow execution, UI surface, Swarm, or Ultracode behavior is
added. Bounded independent-node execution is supported; the bounded direct
predecessor-result relay described above remains the only
dataflow behavior in this slice; it does not transfer authority or workspace
state. The bounded Leader controller is specified separately by [ADR 0135]
(adr/0135-bounded-serialized-leader-controller.md).
Existing Worktree,
Checkpoint, Parent Relay, and worker-scoped read-only LSP contracts remain the
authority owners. See [ADR 0134](adr/0134-durable-serialized-task-dag.md).

### Bounded serialized Leader controller

ADR 0135 adds one explicit Leader controller over one already-published Task
DAG. The Leader is a decision authority only: it reconciles the current DAG,
constructs a bounded redacted deterministic evidence envelope, asks a
dedicated zero-tool model for one typed decision, and calls the existing
Task-DAG one-step seam. It never creates or mutates the graph definition,
dependencies, prompts, capabilities, roots, worker, Worktree, Checkpoint,
Relay, LSP process, or child session directly.

The only model decisions are `SELECT_NODE`, whose node ID must be in the exact
current READY set, and terminal-only `FINALIZE`. The model response is strict
JSON; prose, unknown actions, extra fields, blocked/stale node IDs, and
instructions embedded in node text are data or fail closed. Evidence contains
only bounded node definitions and durable outcome metadata, with redacted
previews and fingerprints; it never carries raw transcript/reasoning/tool
arguments/output, Relay payloads, workspace bytes, checkpoint bytes, Git diffs,
secrets, or arbitrary paths.

The current Session Store schema 23 retains the schema-19 `leader_attempts` and
`leader_decisions` projections. An attempt binds the exact DAG generation,
definition/evidence/objective fingerprints, Leader session, controller owner,
turn identity, and durable lifecycle. SQLite write transactions and CAS-like
state transitions ensure one controller owns a model request for one exact
snapshot. The controller must durably fence `CLAIMED` as
`PROVIDER_FENCED` with the exact owner/session/turn immediately before the
provider call, and that session must equal the actual model binding's session.
An expired claim is rebased to a fresh session/turn only when no output,
decision, or matching old-session turn evidence exists; lease expiry alone is
not proof of process death. A live stale controller therefore fails its fence,
while a post-fence restart fails closed rather than guessing whether the
provider ran. A committed model response is parsed and reused after restart;
historical session/turn provenance is retained and the Leader never
automatically replays a provider request after an observable turn. An
unresolved session turn is conservatively `INDETERMINATE` and needs explicit
recovery. A published decision may be applied through the DAG CAS by another
controller, so a crash after decision publication does not create a second
model request or worker allocation.

The Leader loop is bounded and serialized. It selects one ready node, waits
for the existing Writable Subagent/DAG result, then constructs the next
snapshot. It does not auto-execute a second node inside the one-step seam.
Final synthesis is requested only after a terminal DAG snapshot and is kept
in the dedicated Leader session rather than appended to the parent transcript.
Model-generated DAG creation, replan, retry, parallel/dataflow execution,
merge, rollback, UI/ACP exposure, Swarm, Ultracode, and automatic delegation
remain outside this slice. See [ADR 0135](adr/0135-bounded-serialized-leader-controller.md).

## Platform policy

Linux, macOS, and Windows are first-class CI targets. Platform-specific code is
isolated behind adapters. A small native helper or system facility is allowed
for kernel sandboxing and process containment, but business and orchestration
logic remains Python. Unsupported security guarantees must be reported at
startup, never silently weakened.

The first concrete implementation uses child-scoped Bubblewrap for Linux
`workspace`, `read-only`, and `strict` local-process requests; `off` remains
the portable default. The trusted controller is never re-executed inside the
namespace. Each Bash, background Bash, stdio MCP, or enabled-profile PTY
request receives its own child boundary with explicit workspace mounts,
private HOME and temporary directories, and a minimal environment. Read-only
and strict children additionally use an isolated network namespace. macOS uses
the child-scoped Seatbelt adapter, while Windows enabled non-PTY requests use
the W3 native restricted-token runtime described below; unsupported requests
still fail closed. See
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) and
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md).

The W0 Windows AppContainer investigation separates viable primitives from
production readiness. AppContainer filesystem/ACL, named-pipe, runtime, and
standard-user primitives were exercised, but the current stock Git for Windows
runtime still fails its complete non-admin repository workflow while protected
ancestor ACL expansion is unavailable and unacceptable. The classic stable
unpackaged AppContainer architecture therefore remains unsupported for enabled
Windows `workspace`, `read-only`, and `strict` profiles and fails closed; the
Windows `off` path continues to use the existing Job Object/ConPTY lifecycle.
Evidence PRs #33--#39 remain unmerged and are recorded in
[ADR 0112](adr/0112-windows-appcontainer-sandbox-feasibility-decision.md).

The W1/W2 Windows native foundation is recorded in
[ADR 0113](adr/0113-windows-native-restricted-token-sandbox-architecture.md).
It adds a platform-neutral filesystem/network security-capability model, an
in-memory restricted-token/SID boundary, and an installation-only setup
authority. W1/W2 actual runtime filesystem/network capabilities remain all
`UNSUPPORTED` because enabled Windows profiles still fail closed. The separate
native-backend target is read `LIMITED`, write `STRONG`, and network `STRONG`; a
strong-read request must not be satisfied by a limited provider. Process
lifecycle remains the independent `LocalProcessLifecycleCapability` contract,
with existing Job Object/ConPTY paths reporting
`STRONG_DESCENDANT_OWNERSHIP`.

W2 setup maintains dedicated real local users `NeuroSandboxOffline` and
`NeuroSandboxOnline`, their resolved account SIDs, one installation-scoped
synthetic restricting SID, DPAPI-protected actual account credentials, and
explicit read/primary-user-write/restricting-write/read-only-deny/sensitive-deny
ACL plans. The synthetic SID is only a write-only restricted-token principal;
it is never a read or network identity. Native reconciliation uses `SetEntriesInAclW` so explicit denies are
canonicalized before allows while unrelated controller ACEs and owner data are
preserved. The credential file receives exact deny ACEs for both sandbox users.
Offline outbound blocking is scoped to the real Offline account SID; the
managed block rule remains installed while either dedicated identity is used
and only explicit cleanup removes it. It never targets the Online or
controller user.
Setup state is `READY`, `NEEDS_SETUP`, `NEEDS_REPAIR`, or `UNSUPPORTED`, and
setup/repair/cleanup may require administrator authority while runtime work
does not. W2 does not launch children, connect MCP, add a command runner,
modify Git/Python integration, rewrite Job Object/ConPTY, or change the
foundation's actual capability constant.

W3 adds the Windows non-PTY runtime for Bash, background Bash, and MCP stdio in
`CAPTURE`, `MERGED_CAPTURE`, and argv-safe `PROTOCOL` modes. Each request is
preflighted through W2 and fails before child creation unless setup is `READY`.
The controller starts a trusted workspace-independent runner as the selected
Offline or Online account; the runner creates the final child with a
`WRITE_RESTRICTED` token whose restricting set is exactly the installation
synthetic write SID, plus a kill-on-close Job Object. Controller and runner
use separate controller-to-runner control and runner-to-controller event
named pipes with specific rights that exclude `FILE_CREATE_PIPE_INSTANCE`.
Python `-I` and the explicit environment are necessary but not sufficient
provenance controls: before `CreateProcessWithLogonW`, the resolved
interpreter, runner module, Neuro Code package root, and dependency root must
be outside every model-writable root. Everyone, logon,
sandbox-user, and controller SIDs remain object-ACL principals only. The runner
attests that `DISABLE_MAX_PRIVILEGE` preserved `SeChangeNotifyPrivilege` and
does not call `AdjustTokenPrivileges`. `ISOLATED` selects Offline and `INHERIT` selects
Online without changing the persistent Offline Firewall rule. The fully wired
W3 runtime declares the focused-acceptance-certified provider contract of read
`LIMITED`, write `STRONG`, and network `STRONG`. The W1/W2 foundation
actual-capability constant remains `UNSUPPORTED`, and the target constant is
not used for runtime admission. `strict` fails closed because it requires
strong read isolation. Gates 1–5 execute seven native acceptance tests with
zero skips and prove final-child identity, filesystem/network enforcement,
binary/protocol transport, normal wait, explicit termination, controller-loss
cleanup, and runner kill-on-close ownership. PTY/ConPTY remains W4, and the
existing `off` path is unchanged. The accepted W5 workload matrix (run
`32374860136`) passes Python and child Python, PowerShell, Git, Node/npm, curl,
NUL read/write modes, and dynamic BCrypt startup through both W3 and W4; future
developer tools still require their own bounded evidence rows.

Enabled Linux startup performs a bounded controller-state hardlink audit before
mounting any authorized workspace. It fails closed when a private regular file
has another inode name, preventing a pre-existing workspace hardlink from
reintroducing credentials or session state without scanning the whole workspace.
Dedicated Linux CI must execute the real namespace tests without skips; dedicated
Windows CI must execute the native Job Object and ConPTY lifecycle tests.

Foreground and managed-background shell commands share `ProcessTree`. Unsandboxed POSIX
waiting observes the owned process group after its shell leader exits, while
termination uses a bounded TERM-to-KILL sequence; a descendant that creates a
new session is outside that `off`-profile process-group contract. On Windows, a lazy ctypes
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

The lifecycle capability contract is separate from filesystem and network
authority. `LocalProcessSandbox` and its owned child/terminal seams report
`STRONG_DESCENDANT_OWNERSHIP` for enabled Linux Bubblewrap and Windows Job
Object paths, and `PROCESS_GROUP_BEST_EFFORT` for plain POSIX ProcessTree.
Ordinary Bash, background Bash, MCP stdio, and PTY requests require the latter
minimum; a best-effort adapter must fail closed before child creation when a
workload explicitly requires strong ownership. Enabled macOS profiles use the
Seatbelt adapter to enforce filesystem/network/access control while always
reporting `PROCESS_GROUP_BEST_EFFORT`. See
[ADR 0110](adr/0110-cross-platform-lifecycle-capability-contract.md) and
[ADR 0111](adr/0111-macos-seatbelt-local-process-sandbox.md).

## Stage5DC ACP lifecycle alias compatibility

The private subagent lifecycle adapter bounds external alias allocation to four
attempts and resolves each allocated alias through the ACP namespace before it
is placed on the wire. An unavailable, unresolvable, or wrong-owner alias is
retried and then fails closed. Storage-backed `get_or_create` preserves one
alias for a child across repeated `resume` requests and ACP client reconnects.
This does not change lifecycle ownership, child execution, schema, standard ACP
capabilities, or the explicit single-child read-only boundary. See
[ADR 0083](adr/0083-acp-subagent-alias-reconnect-compatibility.md).

## Stage5DD deterministic context-compaction assessment

`neuro_code.application.memory.compaction` is the canonical owner of the
typed `ContextCompactionPlanner`, usage snapshot, policy, decision, and plan.
The planner uses known capacity thresholds and protected/recent item counts to
produce a bounded half-open candidate range. Unknown capacity is explicitly
`UNAVAILABLE`. The plan never contains conversation items, prompt text, tool
output, credentials, summaries, or provider payloads.

This is an assessment contract only. It does not mutate `ModelContext`, create
durable summary items, call a provider, alter `AgentRuntime`, or change session,
CLI, TUI, ACP, or persistence behavior. Provider-aware summarization,
provider-affinity replay, durable compaction items, and the runtime transaction
boundary remain a later capability. See [ADR 0084](adr/0084-context-compaction-assessment-contract.md).

## Stage5DE provider-aware summary request boundary

The canonical memory module now also owns `ProviderContextWindow` and
`ContextSummaryRequest`. A window records only bounded provider/model labels,
optional context affinity, and positive local capacity metadata. Usage may bind
to that window, and actionable plans project to a request with a bounded,
capacity-clamped summary budget and an index-only candidate range. Unknown
capacity, non-actionable plans, and empty candidate ranges fail closed.

This remains an application contract: it does not add a `ModelProvider`
parameter, call a provider, tokenize or summarize messages, mutate
`ModelContext`, persist compaction items, or change Runtime and interface
behavior. See [ADR 0085](adr/0085-provider-aware-context-summary-request.md).

## Stage5DF provider-aware redacted summary input

The canonical memory module now provides `ContextSummaryInputBuilder` and the
typed `ContextSummaryInput`, `ContextSummaryItem`, and
`ContextSummarySourceKind` projections. The builder accepts one immutable
`ModelContext` and a `ContextSummaryRequest`, projects only the candidate range,
and never copies tool arguments, reasoning content, or preserved provider
payloads. Those values are represented by bounded fixed markers.

Explicit and shape-based redaction runs before control-character sanitization
and UTF-8 byte truncation. An injected local token estimator bounds the input
to the provider window's remaining budget after the summary reservation. The
builder caps the number of items and bytes per item, omits content that cannot
fit, and excludes item text from result representations.

This is still an input contract only. It does not call a Provider, select a
provider-specific tokenizer, build a prompt, mutate `ModelContext`, persist a
compaction item, or change Runtime/interface behavior. See [ADR 0086](adr/0086-provider-aware-redacted-summary-input.md).

## Stage5DH provider-backed bounded summary generation

The canonical memory module now also owns `ProviderContextSummaryGenerator`
and `ContextSummaryGenerationResult`. The generator accepts only a validated
`ContextSummaryInput`, builds a temporary prompt context from its bounded
projection, and performs exactly one `ModelProvider` request with no tools and
`ModelToolPolicy.DISABLED`. Provider/model identity is checked against the
request's window before the call.

Output deltas are buffered and `ModelCompleted.response_text` wins when it is
present. A missing completion, empty response, repeated completion, or remote
tool call fails with `ProviderError`; provider failures and cancellation are
not hidden. The output is redacted and bounded again, and the summary is never
written by this generator, sent to an event sink, or used to mutate the source
context. Automatic Runtime compaction, retries, provider-specific tokenizers,
and whole-turn transaction semantics remain future work. See [ADR 0088](adr/0088-provider-backed-bounded-context-summary-generation.md).

## Stage5DI explicit context-compaction persistence service

`neuro_code.application.memory.compaction_service` now owns the explicit
`ContextCompactionApplicationService`, `PersistContextCompactionRequest`, and
`ContextCompactionPersistenceResult` boundary. The service rebuilds a redacted
bounded input from the immutable source context, validates the expected source
fingerprint before contacting the Provider, calls the existing one-request
summary generator, builds a `DurableCompactionItem`, and persists it through
`SessionStore.save_compaction_item`.

The caller supplies the opaque compaction ID and expected source fingerprint.
Source-count or fingerprint drift fails before model generation. Duplicate-ID
idempotency and conflict behavior remain owned by the storage adapter. Provider
generation and the SQLite write are separate operations, and Provider,
cancellation, and storage failures propagate without retry. This is an
explicit application capability only: it does not trigger from `AgentRuntime`,
add events, alter session items, or claim whole-turn atomicity. See [ADR 0089](adr/0089-explicit-context-compaction-persistence-service.md).

## Stage5DJ compaction transfer and turn-finalization boundary

`DurableCompactionItem` remains an optimization record rather than canonical
conversation history. `SessionExport` intentionally excludes compaction rows,
so JSON/Markdown export and snapshot import preserve the existing export
schema and canonical session items without exposing summaries, source
fingerprints, or Provider-affinity metadata. An imported session starts with
no compaction rows.

Session forks likewise copy the canonical session projection but do not copy
compaction rows: the child may diverge from the parent's source range and
Provider window. Deletion still cascades through the session foreign key.

`SessionStore.finalize_turn()` remains atomic only for its completion event,
ordered session items, search projection, and optional execution record.
Compaction persistence is a separate short transaction and is not implicitly
saved, removed, or rolled back by turn finalization. A future runtime slice
that needs cross-operation atomicity must add an explicit storage contract;
sequential calls do not provide that guarantee. See [ADR 0090](adr/0090-compaction-transfer-and-turn-boundary.md).

## Stage5DK explicit context-compaction trigger boundary

`neuro_code.application.memory.compaction_trigger` now owns the typed
`ContextCompactionTriggerMode`, request, assessment, result, and stateless
`ContextCompactionTriggerService`. `DISABLED` is the default and only runs the
existing deterministic planner; it performs no Provider or storage work.
`EXPLICIT` may delegate a plan with a non-empty candidate range to the existing
context-compaction persistence service, but only after the caller supplies a
session ID, compaction ID, timezone-aware timestamp, and expected source
fingerprint. Stale-source, Provider, cancellation, and storage failures remain
fail-closed and are not converted into a no-op result.

The trigger is intentionally not wired into `AgentRuntime`. It has no normal
turn step counter, retry state, event emission, or cross-operation transaction
claim. Compaction generation and persistence remain separate operations, and a
future Runtime integration must define its safe boundary and budget semantics
explicitly. See [ADR 0091](adr/0091-explicit-context-compaction-trigger.md).

## Stage5DL explicit Runtime compaction boundary gate

`neuro_code.application.memory.compaction_runtime` now defines the boundary a
future Runtime caller must satisfy before invoking the Stage5DK trigger. The
only modeled safe points are `BEFORE_MODEL_REQUEST` and `AFTER_TOOL_BATCH`;
active model requests, active tool batches, and cancellation requests fail
closed without contacting a Provider or storage adapter.

The gate keeps compaction accounting separate from the ordinary turn budget:
the current contract permits exactly one model request, zero tool calls, and
never inherits turn limits. It returns a typed boundary decision and delegates
an actionable request to `ContextCompactionTriggerService` only when the
boundary is safe and the trigger is explicitly enabled. This is a contract and
test seam only: `AgentRuntime`, events, and automatic threshold triggering
remain unchanged. See [ADR 0092](adr/0092-runtime-compaction-safe-boundary.md).

## Stage5DM enforced Runtime compaction timeout

The runtime gate now enforces a finite wall-clock budget around an allowed
explicit compaction operation. `ContextCompactionRuntimeBudget` defaults to 30
seconds and cannot exceed 300 seconds; the limit covers both the one strict
no-tool summary request and its following persistence call. A deadline raises
the typed `ContextCompactionTimeoutError` and never returns a successful
trigger result. Provider errors, storage errors, and task cancellation remain
unchanged. Disabled, unsafe, cancelled, and non-actionable requests still make
no Provider or storage calls. This remains a boundary contract only: normal
`AgentRuntime` operation and automatic compaction are not enabled, and no
cross-operation Provider/SQLite transaction is claimed. See [ADR 0093](adr/0093-enforced-context-compaction-timeout.md).

## Stage5DN Runtime compaction failure projection

`neuro_code.application.memory.compaction_runtime` now exposes the bounded
`classify_context_compaction_failure()` policy projection. Only
`ContextCompactionTimeoutError` has a controlled-terminal projection:
`BUDGET_LIMITED` with `WALL_TIME_BUDGET`, `recoverable=True`, and
`finalized=False`. Its execution-record policy is `TURN_FINALIZATION`, so a
future turn owner may persist it only inside the existing turn-finalization
transaction. Cancellation, Provider errors, and storage errors remain
propagation-only projections with no outcome and no record request; unknown
exceptions remain unclassified.

The projection stores no exception detail and does not catch errors, modify
`AgentRuntime`, emit events, enable automatic compaction, or claim
Provider/SQLite cross-operation atomicity. See [ADR 0094](adr/0094-runtime-compaction-failure-projection.md).

## Stage5DO explicit Runtime compaction seam

`AgentRuntime` now accepts an optional `compaction_runtime_gate`, defaulting to
`None`, and exposes `trigger_context_compaction()` for a complete caller-owned
`ContextCompactionRuntimeRequest`. A missing gate fails closed with
`ConfigurationError`; an injected gate receives the immutable safe-boundary
request unchanged. The facade does not derive thresholds, mutate context,
increment ordinary turn steps, emit events, or write an execution record.

`AgentRuntime.run()` and ApplicationComposition remain unchanged, so automatic
compaction and production gate wiring are still disabled. Timeout, cancellation,
Provider, storage, and turn-finalization ownership continue to follow
[ADR 0094](adr/0094-runtime-compaction-failure-projection.md). See [ADR 0095](adr/0095-explicit-runtime-compaction-seam.md).

## Stage5DP application-owned explicit compaction caller

`ApplicationComposition.create_binding()` now assembles one
`ContextCompactionRuntimeGate` per binding from the existing Provider,
`SessionStore`, redaction values, and compaction trigger/persistence services.
The gate is injected into `AgentRuntime`, but remains opt-in: the normal Agent
loop performs no threshold check and no automatic compaction call.

`AgentConversation.trigger_context_compaction()` is the application-owned
caller. It runs under the conversation's existing turn lock, requires a
matching persisted session for an `EXPLICIT` request, and delegates the
immutable caller-supplied `ContextCompactionRuntimeRequest` unchanged. The
request context is a snapshot owned by the caller; its source fingerprint is
the stale-snapshot guard. The method does not mutate transcript items, emit
events, reload a turn, or claim atomicity with `finalize_turn()`. See [ADR
0096](adr/0096-application-owned-compaction-caller.md).

## Stage5DQ explicit atomic turn-finalization boundary

`SessionStore` now exposes the opt-in
`finalize_turn_with_compaction()` contract. The SQLite implementation commits
the `TURN_COMPLETED` event, session items, search projection, optional
`SessionExecutionRecord`, and one durable compaction item in the same
`BEGIN IMMEDIATE` transaction. Validation, duplicate-event, compaction
ownership/payload, uniqueness, index, and storage failures roll the whole unit
back. Identical existing compaction IDs remain idempotent.

`save_compaction_item()` and ordinary `finalize_turn()` retain their separate
short-transaction behavior. The contract does not include Provider generation,
does not enable automatic compaction, and is not consumed by the current
Runtime or explicit compaction gate. See [ADR 0097](adr/0097-atomic-turn-finalization-with-compaction.md).

## Stage5DR turn recorder compaction-finalization owner

`TurnEventRecorder.finalize_turn_completion()` accepts an optional validated
`DurableCompactionItem`. When supplied, the existing application completion
path requires a persisted session and delegates the event/items/record/item
commit to `SessionStore.finalize_turn_with_compaction()`; ordinary calls still
use `finalize_turn()`. Invalid input fails before the in-memory completion event
is appended, and persistence still completes before `TURN_COMPLETED` delivery.

The recorder owns only this final storage commit. It does not generate a
summary, invoke a Provider, alter the Agent loop, consume failure projections,
or enable automatic compaction. See [ADR 0098](adr/0098-turn-recorder-compaction-finalization-owner.md).

## Stage5DS typed compaction turn projection

`neuro_code.application.memory.compaction_runtime` now exposes
`ContextCompactionTurnProjection` with explicit success and failure helpers.
Successful explicit compaction transfers only the already persisted and
validated `DurableCompactionItem`. A timeout transfers the bounded,
recoverable `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome for a future turn owner;
cancellation, Provider, and storage failures remain propagation-only, and
unknown exceptions remain unclassified. The projection stores no exception
details or raw summary and performs no persistence or event emission. It does
not call `TurnEventRecorder`, integrate the normal Agent loop, or enable
automatic compaction. See [ADR 0099](adr/0099-context-compaction-turn-projection.md).

## Stage5DT explicit compaction turn owner

`TurnEventRecorder.finalize_turn_from_compaction_projection()` is the opt-in
consumer of `ContextCompactionTurnProjection`. A successful projection must
provide the caller's ordinary turn outcome and uses the atomic
`finalize_turn_with_compaction()` path. A timeout projection supplies its own
bounded recoverable outcome and does not invent a compaction row.
Propagation-only and no-op projections fail closed before an in-memory
completion event is appended. The normal Agent loop, automatic compaction,
Provider generation, and session-lock ownership remain outside this seam. See
[ADR 0100](adr/0100-explicit-compaction-turn-owner.md).

## Stage5DU application compaction owner under the turn lock

`AgentConversation.run_context_compaction_with_owner()` is an explicit,
opt-in application seam that validates the caller-owned immutable request and
runs the Runtime compaction gate plus its typed owner callback under the
conversation's existing `_turn_lock`. Successful results transfer only a
persisted `DurableCompactionItem`; a bounded timeout transfers the existing
recoverable `BUDGET_LIMITED/WALL_TIME_BUDGET` outcome. No-op projections fail
closed before the owner is called, while cancellation, Provider, storage, and
unknown failures preserve their original exceptions.

The owner remains responsible for `TurnEventRecorder` and any finalization
transaction. This seam does not enter the normal Agent loop, trigger automatic
compaction, mutate transcript items, emit events, or claim that Provider
generation and SQLite persistence are one transaction. See [ADR
0101](adr/0101-application-compaction-owner-under-turn-lock.md).

## Stage5DV context usage snapshots and stale-source request construction

`neuro_code.application.memory.compaction_runtime` now provides
`build_context_usage_snapshot()` and
`build_explicit_context_compaction_runtime_request()` as side-effect-free
application helpers. The usage helper follows the existing context-usage event
convention when provider input/output counts are available, otherwise it uses
the bounded `ModelContext` estimator and marks the value estimated. A missing
provider capacity remains unknown rather than being inferred from a concrete
provider implementation.

The request builder performs deterministic assessment only. It computes an
opaque source fingerprint from the exact immutable context and actionable
candidate range, requires caller-owned persistence metadata only for an
actionable explicit request, and leaves non-actionable requests without a
fabricated digest. Provider/storage calls, session locking, execution-time
stale validation, and automatic compaction remain owned by the existing
application/runtime seams. See [ADR
0102](adr/0102-context-usage-snapshot-and-stale-source-builder.md).

## Stage5DW explicit live-context compaction command

`AgentConversation.run_explicit_context_compaction_with_owner()` is now the
narrow application command for an actionable explicit compaction. It acquires
the existing conversation turn lock before asking `AgentRuntime` to build a
request-scoped context snapshot with the same reasoning, interaction,
instruction, and skill guidance used by model requests. The configured
`ContextCompactionRuntimeGate` then reuses the usage snapshot and computes the
stale-source guard from that exact context.

The command allocates bounded identity/time metadata when necessary and reuses
the existing typed owner projection under the same lock. It requires a
persistent session, does not append transcript items, emit events, start a
normal model turn, or enable automatic thresholds. Provider generation and
compaction persistence remain outside one shared transaction. See [ADR
0103](adr/0103-explicit-live-context-compaction-command.md).

## Stage5DX explicit compaction command projection

`neuro_code.application.memory.compaction_runtime` now exposes the bounded
`ContextCompactionCommandResult` and
`project_context_compaction_command_result()` application/interface projection.
It distinguishes `completed`, `not_needed`, and the controlled
`budget_limited` timeout result. Successful results expose only the opaque
compaction ID, source/candidate counts, and summary token metadata; they never
expose the summary, source fingerprint, prompt, messages, tool output, or
exception details. Provider, cancellation, storage, and unknown failures
remain propagation-only exceptions. CLI and ACP serialization helpers share
the same bounded fields, without enabling a command, event, normal Agent loop,
or automatic compaction. See [ADR
0104](adr/0104-explicit-compaction-command-projection.md).

## Unified ordinary execution budget and transient replan guidance

`neuro_code.application.execution_policy` resolves named product profiles and
the legacy `max_steps` override into the existing domain `ExecutionBudget`.
Formal CLI, TUI, and ACP composition paths pass that same immutable value to
`AgentRuntime`; the loop hard cap and the per-turn supervisor therefore share
one model/tool budget. Finalizer attempts remain a separate bounded resource.

At a safe completed tool-batch boundary, a non-terminal `REPLAN` decision can
activate `SyntheticReason.RUNTIME_SUPERVISION` for the next request in
`FINALIZE_TERMINAL` mode. `ContextBuilder` owns that request-only injection and
the general batch-first evidence-gathering policy. Neither message is appended
to session items. When new progress resolves an active replan notice, the loop
appends a bounded resolution notice rather than rewriting an already-sent
request prefix. Tool execution order and the existing stuck detectors are
unchanged. See [ADR 0105](adr/0105-unified-execution-budget-and-replan-guidance.md).

## Bounded long-task Runtime guidance, compaction, and segments

The production `FINALIZE_TERMINAL` loop now projects its canonical
`ExecutionBudget` through `ExecutionBudgetUsage`. Request-only guidance and
`EXECUTION_BUDGET_UPDATED` expose bounded model/tool counts without prompts,
tool payloads, or supervisor fingerprints. The event remains available to
interested interface projections, while the standard TUI deliberately keeps
raw execution counters out of its runtime bar.

When a binding also has persistent session storage and a configured provider
context window, the existing compaction gate is assessed automatically at the
`BEFORE_MODEL_REQUEST` and `AFTER_TOOL_BATCH` safe points. Compaction keeps its
independent one-request/no-tool budget, preserves canonical transcript items,
never splits tool call/result pairs, and resumes from the newest compatible
durable projection. Provider/storage/cancellation failures keep their existing
semantics. Repeating the same range is suppressed; a projection that remains
above the hard context threshold finalizes as recoverable
`BUDGET_LIMITED/CONTEXT_WINDOW_BUDGET`.

Progressing long turns may also emit a durable, bounded
`EXECUTION_SEGMENT_CHECKPOINTED` event and receive one transient checkpoint
guidance message. Segment thresholds do not reset or replace the global turn
budget and do not promise crash recovery or workspace rollback. See [ADR
0107](adr/0107-bounded-long-task-runtime.md).

## Cache-friendly model request projection and usage

`ContextBuilder` owns the stable early request prefix: the request-scoped
system policy, deterministic tool definitions, and the current serialized
project-instruction and skill catalog discoveries. A discovery is refreshed on
each request so a real workspace change can take effect, but its ordered
serialization is stable while its source content is unchanged.

Mutable plan revisions, segment checkpoints, budget pressure, and replan
state are not folded back into the system message or inserted before durable
conversation items. `AgentLoopRunner` instead appends bounded synthetic
runtime notices after safe conversation boundaries. Budget guidance uses only
the discrete `CONSERVE`, `FOCUS`, and `FINAL_STAGE` pressure transitions; it
does not rewrite exact remaining counters on every model step. These notices
are excluded from session persistence, resume replay, and compaction source
items. A one-request background-completion reminder remains a deliberate tail
exception because it is acknowledged only after a successful provider
completion.

This preserves the intended shape of an unchanged long turn: request *N + 1*
is normally request *N* plus newly appended durable conversation items and, at
most, a newly relevant bounded runtime notice. It does not promise a cache hit:
providers may use different cache keys, tokenization, retention windows, and
eligibility rules, and a real project-instruction or skill change correctly
invalidates the affected prefix.

`ModelCompleted.usage` now carries the provider-neutral `ModelUsage` value:
provider-native input/output fields plus optional cache-read (also exposed as
`cache_hit_tokens`), cache-write, and cache-miss token counts. The input-token
semantics are explicit. Most providers report total input, while Anthropic
reports the uncached tail after its cache breakpoint; the Runtime derives a
complete processed-input total only when cache-read and cache-creation fields
make that calculation exact. `CONTEXT_USAGE_UPDATED` projects only those
bounded fields, so interfaces receive neither prompts, tool arguments, nor
hidden runtime context. OpenAI-compatible providers preserve reported
prompt-cache fields, OpenAI Responses preserves cached-input detail, Anthropic
uses native top-level automatic ephemeral cache control so its cache breakpoint
can advance with an append-only Agent conversation and preserves cache
creation/read usage, and Gemini preserves reported implicit cached-content usage.
Unreported fields remain `None`; the Runtime never infers a cache split or
claims a cache hit from an aggregate input total.

## Read-only Language Server Protocol boundary

The LSP vertical slice is an application-owned semantic read path. The stable
`lsp` tool is registered by `ToolRegistry` and executed by the ordinary
`ToolExecutor`, so its input path uses the same canonical
`FilesystemAccessPlan` and permission decision as other structured read tools.
The tool is never journaled as a mutation and its cross-file result projection
does not open an approval prompt.

`ApplicationComposition` creates one `LanguageServerManager` per binding. A
binding-owned resource scope closes short-lived worker managers immediately;
application shutdown closes any managers that remain. The manager routes by
canonical workspace root plus explicit `LanguageServerProfile`; it is not a
TUI singleton and does not own a second configuration system. Profile commands
are argv-only and are started through `LocalProcessSandbox` with
`LocalProcessPurpose.LSP_SERVER`, read-only workspace roots, explicit
environment, and bounded process lifecycle.

The MCP stdio adapter remains MCP-owned and newline-framed. LSP has a separate
Content-Length JSON-RPC framer and its own request correlation, cancellation,
server-request safety responses, diagnostics cache, bounded stderr, and close
handshake. LSP output is untrusted: only safe local file URIs within the
canonical workspace roots are projected, and permission DENY/ASK plus invalid,
outside, or link-like locations are omitted.

The implemented operations are definition, references, hover, document
symbols, workspace symbols, diagnostics, status, and bounded restart. Rename,
format, code actions, `workspace/applyEdit`, and arbitrary server edits remain
outside the LSP slice. Worktree and checkpoint capabilities are application-
owned seams described below. The LSP manager never mutates them; ADR 0132
composes a read-only manager into the explicit managed-worktree worker binding.

## Application-owned managed Git worktrees

The first worktree capability is an explicit application service rather than a
model-facing arbitrary Git tool. `ApplicationComposition` can construct one
`WorktreeApplicationService` backed by `GitWorktreePort` and a separate,
versioned `worktrees.db` ownership store. The service uses the existing
canonical filesystem resolver only for workspace binding; Git repository
identity is a separate value based on the canonical common Git directory,
source worktree, Git directory, and observed HEAD.

Managed paths live below the state directory at
`worktrees/<repository-id>/<worktree-id>`, outside the source checkout. A
create request names an explicit base revision, which is resolved to an exact
commit and preflighted for applicable external checkout filters before a
detached or `neuro/worktree/<id>` branch worktree is added.
Source dirty changes remain exclusively in the source checkout. Additional
workspace roots are not inherited by a worktree binding.

The local Git adapter submits argv-safe requests through the canonical local
process sandbox port, with bounded output, timeouts, and cancellation cleanup;
it does not create subprocesses directly. Every invocation overrides
`core.hooksPath` with an empty Neuro Code-owned directory and sets
`core.fsmonitor=false`. It also asks Git, using the exact target commit,
whether an applicable checkout filter has a configured `smudge` or `process`
driver; such a driver is rejected before checkout. The existing
`ProcessTreeLocalProcessSandbox` bridge with `SandboxProfile.OFF` is a
lifecycle bridge, not OS-enforced filesystem or network isolation, so the
capability does not claim that guarantee. Explicit remote operations remain
absent: it performs no fetch/pull/push/clone or repo-wide prune. Removal
requires durable managed ownership plus matching repository/path/HEAD/branch
identity and uses `git worktree remove` without `--force`; dirty and locked
worktrees refuse removal, while managed branches are retained.
The managed worktree capability requires Git 2.40.0 or newer because its
fail-closed filter preflight relies on `git check-attr --source=<tree-ish>`;
older Git is rejected during initialization.

SQLite intent and Git metadata are not treated as one transaction. The
worktree schema uses an insert-only ownership claim plus a durable generation
CAS: `WorktreeId` conflicts cannot overwrite an existing row, canonical paths
remain unique, and every later mutation requires the expected generation/state
and increments the generation. A stale writer receives
`CONCURRENT_MODIFICATION`; reconciliation rereads the winner instead of
overwriting it. Durable `CREATING`/`REMOVING` records are reconciled against
actual Git records after process death. Exact matches can become `READY`, an
absent record after a remove intent becomes `REMOVED`, and path reuse, missing
repositories, and identity mismatches become `ORPHANED` without filesystem
deletion. See [ADR 0129](adr/0129-managed-git-worktree-capability.md).

## Managed workspace checkpoint and rollback

Workspace checkpointing is a separate explicit internal capability and does
not reuse execution segment checkpoints or `session_turn_attempts`. The
segment event is a progress/audit marker; turn recovery is request/output/tool
durability; a workspace checkpoint is a source projection owned by one ready
managed worktree. `WorkspaceCheckpointApplicationService` is constructed only
through `ApplicationComposition.create_workspace_checkpoint_service()` and is
not a model-facing tool or an automatic policy.

Capture accepts a `WorktreeHandle`, proves the durable managed-worktree record,
and stores the exact per-worktree Git index plus tracked and non-ignored
untracked regular files/symlinks under a separate `checkpoints.db` and
state-owned content-addressed artifacts. It includes staged and unstaged
content, tracked deletions, binary bytes, modes, and safe symlink targets;
ignored files are out of scope. Unmerged stages, intent-to-add, sparse/split
indexes, submodules, nested repositories, special files, and unsafe link-like
parents fail closed. A deterministic SHA-256 fingerprint covers identity,
HEAD, index, modes, paths, and in-scope content.

Rollback is restricted to the same owned managed worktree and exact checkpoint
HEAD. It persists a separate `RollbackAttempt` before mutation, acquires a
unique Neuro Code Git worktree lock, enumerates exact checkpoint-after paths,
restores files and the index through the workspace adapter, and verifies the
final fingerprint. It never uses broad `git clean`, stash, reset, checkout,
branch-ref rewrite, history rewind, or arbitrary recursive deletion. Ignored
files remain untouched. Partial or uncertain operations are durable
`INDETERMINATE` and can be reconciled after process death; READY checkpoint
targets are immutable and CAS-protected. See [ADR
0130](adr/0130-managed-workspace-checkpoint-rollback.md).
