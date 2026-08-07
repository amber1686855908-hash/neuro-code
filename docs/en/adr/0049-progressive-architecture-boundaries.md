# ADR 0049: Progressive modular-monolith architecture boundaries

[简体中文](../../zh-CN/adr/0049-progressive-architecture-boundaries.md) · **English**

- Status: accepted
- Date: 2026-07-22
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
- Provider-facade retention portions are superseded by ADR 0072 after
  Architecture Freeze v1.
- Adapter, tool, and flat-domain-facade retention portions are superseded by
  ADR 0074 after the compatibility cleanup audit.

## Context

Neuro Code already delivers vertical capabilities through domain values, typed
ports, application orchestration, and concrete adapters. Its current package
layout does not yet express those responsibilities consistently:
`application.py` selects concrete adapters as a composition root, application
runtime modules import some tool and platform implementations, and the CLI and
ACP interfaces construct or access infrastructure directly.

A one-shot package rewrite would mix import churn with behavioral changes and
would make regressions in sessions, permissions, sandboxing, credentials, ACP,
and process ownership difficult to isolate. The architecture therefore needs a
target dependency model and an executable baseline before implementation moves.

## Decision

Neuro Code remains one distribution and one import package organized as a
modular monolith using Ports and Adapters. The target responsibilities are:

- `domain`: pure domain values, invariants, and rules;
- `application`: agent turns, conversations, permissions, sessions, and
  workflow orchestration;
- `application/ports`: abstractions required by application behavior;
- `infrastructure`: model providers, SQLite, filesystems, processes, PTYs,
  sandboxes, tools, MCP, HTTP, and settings implementations;
- `interfaces`: CLI, TUI, ACP, and other inbound adapters;
- `bootstrap`: configuration loading, factories, lifecycle ownership, and
  dependency assembly;
- `shared`: errors, bounded asynchronous helpers, redaction, and similarly
  small cross-cutting primitives.

The allowed dependency direction is:

```text
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

More specifically:

- `domain` may depend only on the standard library, `domain`, and `shared`;
- `application` may depend on `domain`, `application/ports`, and `shared`;
- `infrastructure` may depend on `domain`, `application/ports`, `shared`, and
  other infrastructure internals, but not on interfaces or bootstrap;
- `interfaces` may depend on application-facing contracts, domain values, and
  shared helpers, but not construct concrete infrastructure;
- `bootstrap` is the only layer allowed to depend on `interfaces`,
  `application`, and `infrastructure` together;
- `shared` must not become an alternate composition root or a dependency bag.

Application and domain modules must not import concrete infrastructure
implementations. Side effects continue to cross typed ports and the existing
permission, workspace, sandbox, and platform boundaries.

Configuration loading belongs to bootstrap, but configuration value objects
used by multiple layers must not be defined in bootstrap: doing so would force
those layers to depend on the composition root. Their final ownership will be
decided during the dedicated configuration-splitting phase. Until then,
`neuro_code.config` is treated as an explicit transitional boundary rather than
being prematurely assigned to bootstrap.

Architecture migration is incremental:

1. add a canonical new module path;
2. keep the old path as a compatibility re-export of the same objects;
3. switch internal imports and verify behavior;
4. remove the old compatibility path only in a separate, explicitly approved
   and versioned change.

A file move and a behavior modification must not occur in the same migration
stage. A stage that moves code changes imports and wiring only; behavior changes
require their own vertical slice and tests.

Stage 0 adds a standard-library AST dependency test. Every currently known
forbidden direct import is recorded by source module, target module, and reason.
The active allowlist must exactly match the violations present in the tree and
must remain a subset of the frozen initial set. Removing a violation requires
removing its active allowlist entry; adding a new violation fails the test.
Changing the frozen baseline is an architecture decision, not routine allowlist
maintenance.

Stage 0 does not move implementations and does not change CLI arguments,
outputs, exit codes, runtime events, configuration precedence, database or
session formats, ACP behavior, permissions, sandboxing, or security semantics.

### Implementation status — 2026-07-28

1. Runtime application behavior has been canonicalized under explicit
   `neuro_code.application.runtime` submodules.
2. The development-stage breaking cleanup has removed `neuro_code.runtime`;
   runtime application behavior is available only from the explicit canonical
   submodules, and `neuro_code.application.runtime.__init__` remains minimal.
3. The development-stage breaking cleanup has removed `neuro_code.ports`; port
   contracts are available only from `neuro_code.application.ports.*`.
4. The development-stage breaking cleanup has removed the root shared
   compatibility modules `neuro_code.errors`, `neuro_code.async_utils`, and
   `neuro_code.redaction`; their primitives are available only from the
   corresponding `neuro_code.shared.*` modules.
5. The development-stage breaking cleanup has removed the package-level
   composition facade from `neuro_code.application`; its `ApplicationSettings`
   package export remains, and composition is available only from
   `neuro_code.bootstrap.composition`.
6. The development-stage breaking cleanup has removed `neuro_code.cli.main`.
   Console scripts and `python -m neuro_code` continue to use
   `neuro_code.bootstrap.entrypoints:main`, while injected `neuro_code.cli.run`
   remains the CLI core.
7. The managed-provider JSON reader has been separated into
   `neuro_code.configuration.managed_provider_settings`.
8. `neuro_code.config` no longer imports the provider-settings adapter.
9. The development-stage breaking cleanup has removed managed-provider loader
   re-exports from the adapter and config namespaces, and removed
   `neuro_code.config.ProviderConfig`; the public APIs for this boundary are
   the canonical reader, `JsonProviderSettingsStore`, `ProviderProfile`, and
   `AppConfig`.
10. The active temporary dependency allowlist is now empty.
11. The Stage 0 frozen baseline remains a historical upper-bound record and was
   not rewritten.
12. A general dynamic-import architecture guard now scans production sources.
   The development-stage breaking cleanup has removed the ACP composition
   facade: `serve_acp` accepts only `AcpApplicationService`. The only remaining
   Bootstrap narrow edge is the canonical `neuro_code.__main__`
   package-executable entrypoint, which is not compatibility debt.
13. The generic Responses adapter is implemented only at
   `neuro_code.providers.openai_responses.OpenAIResponsesProvider`. xAI remains
   an `openai-responses` dialect selected by `ProviderProfile`; the development-
   stage breaking cleanup removed `neuro_code.providers.xai_responses` and
   `XAIResponsesProvider`.
14. The development-stage breaking cleanup removed the root approval-contract
    re-exports from `neuro_code.permissions`. Request and response contracts are
    available only from `neuro_code.application.permissions.contracts`, while
    the root module retains synchronous permission policy.
15. Other compatibility-path removal remains a separate, versioned decision.

16. Stage 1 establishes `neuro_code.domain.conversation` as the canonical owner for
    messages, model context, agent events, and normalized model events. The former
    `neuro_code.domain.messages`, `events`, `model_context`, `model_events`, and
    `context_usage` modules remain compatibility facades that re-export the same
    objects. Production imports use the new canonical paths; removal of the facades
    remains a separate compatibility decision.
17. Stage 2A establishes `neuro_code.domain.execution` as a canonical package. Its
    public aggregate facade re-exports one implementation from `outcomes.py`,
    `tasks.py`, and `checkpoints.py`; validation helpers remain private to that
    package. The public `neuro_code.domain.execution` imports remain compatible,
    while the former flat `execution.py` implementation is removed rather than
    duplicated.
18. Stage 2B establishes `neuro_code.domain.plans` as a canonical package with
    `models.py` as its single implementation owner. The aggregate package keeps
    the existing public constants, plan value objects, fingerprint and update
    validation API; the former flat `plans.py` implementation is removed rather
    than duplicated. Session, tool and background-task boundaries remain separate
    follow-up slices.
19. Stage 2C establishes `neuro_code.domain.sessions` as a canonical package with
    `models.py` as its single implementation owner. The aggregate package keeps
    the existing title normalization, session summary, and session snapshot API;
    the former flat `sessions.py` implementation is removed rather than
    duplicated. Session search projections and storage adapters remain separate
    boundaries and are not moved by this slice.
20. Stage 2D establishes `neuro_code.domain.tools` as a canonical package with
    `models.py` as its single implementation owner for `ToolDefinition` and
    `ToolResult`. Tool registries, executors, permissions, sandbox, MCP, and
    background-task lifecycles remain outside this pure value-object package.
21. Stage 2E establishes `neuro_code.domain.session_tasks` as a canonical package
    with `models.py` as its single implementation owner for the bounded
    `SessionTask` state machine. Background wake ledgers remain a separate
    boundary because their restart, budget, and persistence semantics require
    an independent migration and transaction review.
22. Stage 2F establishes `neuro_code.domain.background_tasks` as a canonical
    package with `models.py` as its single implementation owner for task
    snapshots and the wake ledger value objects. The package move does not alter
    manager side effects, SQLite transaction boundaries, process-tree ownership,
    cancellation, or wake retry semantics.
23. Stage 2G establishes `neuro_code.domain.terminal` as a canonical package
    with `models.py` as its single implementation owner for terminal sizes,
    signals, output chunks, and limits. PTY, process, sandbox, permission, and
    terminal-manager implementations remain outside the domain package.
24. Stage 2H establishes `neuro_code.domain.sandbox` as a canonical package
    with `models.py` as its single implementation owner for the pure
    `SandboxProfile` policy value. Shell sandbox ports, bubblewrap/process
    adapters, permissions, and cancellation remain outside the domain package.
25. Stage 2I establishes `neuro_code.infrastructure.sandbox.process_tree` as
    the canonical owner of the concrete `ProcessTree` adapter. The former
    `adapters.process_tree` path remains a compatibility facade; process
    ownership, termination, Windows Job Object behavior, PTY, sandbox policy,
    and cancellation semantics are unchanged.
26. Stage 2J establishes the POSIX PTY and Windows ConPTY wrapper adapters under
    `neuro_code.infrastructure.sandbox`. The former `adapters.posix_pty` and
    `adapters.windows_pty` paths remain compatibility facades; native ConPTY,
    Job Object, bubblewrap, permission, and terminal-session behavior remains
    unchanged and is not duplicated.
27. Stage 2K establishes `neuro_code.infrastructure.sandbox.sandbox` as the
    canonical owner of the Linux bubblewrap `ShellSandbox` implementation.
    `adapters.sandbox` remains a compatibility facade, and the composition root
    imports the concrete factory from the infrastructure module. Native Windows
    ConPTY and Job Object implementations remain separate adapters for a later,
    platform-specific slice; sandbox policy, fail-closed checks, permissions,
    and cancellation behavior are unchanged.
28. Stage 2L establishes `neuro_code.infrastructure.sandbox.windows_process` as
    the canonical owner of the stateless Windows environment-block primitive.
    `adapters.windows_process` remains a compatibility facade, and the Job
    Object, ConPTY, and Windows process lifecycle implementations remain in
    their existing adapters until their own platform-specific slices. The
    sandbox package keeps concrete platform modules lazy to avoid import cycles;
    no process, permission, or cancellation behavior changes.
29. Stage 2M establishes `neuro_code.infrastructure.sandbox.windows_job` as the
    canonical owner of `WindowsJobObject`. The former `adapters.windows_job`
    path remains a compatibility facade; ProcessTree, JobProcess, and ConPTY
    consume the canonical object without changing Win32 creation, kill-on-close,
    handle ownership, termination, or cancellation behavior.
30. Stage 2N establishes `neuro_code.infrastructure.sandbox.windows_job_process`
    as the canonical owner of `WindowsJobProcess`. The former
    `adapters.windows_job_process` path remains a compatibility facade;
    ProcessTree consumes the canonical process wrapper while atomic process
    creation, inherited-handle policy, stream readers, close, termination, and
    cancellation semantics remain unchanged.
31. Stage 2O establishes `neuro_code.infrastructure.sandbox.windows_conpty` as
    the canonical owner of the native `WindowsPseudoConsoleSession`.
    `adapters.windows_conpty` remains a compatibility facade and the shared
    `windows_pty` wrapper consumes the canonical class. Pseudoconsole creation,
    resize, input/output draining, interruption, termination, close, and
    cancellation behavior remain unchanged.
32. Stage 2P makes the `neuro_code.infrastructure.sandbox` aggregate boundary
    an explicit import contract. Its `ProcessTree` export remains lazy and
    preserves canonical object identity; importing any platform-specific
    sandbox module must not eagerly import the process-tree implementation.
    These checks protect cross-platform import isolation without moving or
    changing any sandbox implementation.
33. Stage 2Q establishes `neuro_code.infrastructure.providers.provider_catalog`
    as the canonical owner of the bounded, read-only HTTP model catalog adapter.
    `adapters.provider_catalog` remains a compatibility facade and bootstrap
    consumes the canonical owner. Provider request/stream contracts, HTTP
    redaction, response bounds, error mapping, and model ordering remain
    unchanged; provider settings persistence is intentionally a separate slice.
34. Stage 2S gives `neuro_code.infrastructure.providers` a narrow lazy aggregate
    export for `HttpProviderCatalog` and `JsonProviderSettingsStore`. Accessing
    one adapter loads only that adapter, not the other or the model Provider SDKs;
    the aggregate does not own Provider stream behavior or configuration policy.
35. Stage 2T establishes `neuro_code.infrastructure.tools.plans` as the canonical
    owner of the side-effect-free `UpdatePlanTool` adapter. The former
    `neuro_code.tools.plans` path remains a compatibility facade and the registry
    consumes the canonical tool. Plan validation, redaction, metadata shape,
    SessionStore handling, permissions, and workspace behavior remain unchanged;
    executable tools are intentionally deferred to separate slices.
36. Stage 2U establishes `neuro_code.infrastructure.tools.skills` as the canonical
    owner of the read-only, bounded `SkillTool` adapter. The former
    `neuro_code.tools.skills` path remains a compatibility facade and the registry
    consumes the canonical tool. Symlink/reparse rejection, workspace-root checks,
    bounded reads, substitution, redaction, output limits, and cancellation remain
    unchanged; filesystem writes, shell execution, and discovery ownership remain
    separate boundaries.
37. Stage 2V establishes `neuro_code.infrastructure.tools.filesystem` as the
    canonical owner of the read-only filesystem tools (`ReadFileTool`,
    `ListDirTool`, and `GrepTool`) and their shared workspace-path helpers.
    The former `neuro_code.tools.filesystem` path remains a compatibility facade
    for those tools while continuing to own the `search_replace` write tool;
    the registry consumes the canonical read-only tools. Path resolution,
    workspace display, primary-workspace tracking, sandbox boundaries, output
    bounds, redaction, and cancellation remain unchanged; write tools, shell
    execution, and background-task managers are intentionally deferred.
38. Stage 2W establishes `neuro_code.infrastructure.tools.registry` as the
    canonical owner of `ToolRegistry` and the `default_tool_registry` factory.
    The former `neuro_code.tools.registry` path remains a compatibility facade
    and the public `neuro_code.tools` aggregate re-exports the same objects;
    bootstrap consumes the canonical factory. The registry is pure wiring:
    tool implementations are imported lazily when the factory is called, so
    importing the registry loads no bash, background-task, client terminal,
    filesystem, plan, or skill implementation. Registration order, tool
    identity, sandbox/workspace gating, and permission boundaries remain
    unchanged.
39. Stage 2X establishes `neuro_code.infrastructure.tools.background_tasks`
    as the canonical owner of the read-only background task tools
    (`TaskOutputTool` and `WaitTasksTool`) and their shared argument,
    snapshot, and rendering helpers. The former
    `neuro_code.tools.background_tasks` path remains a compatibility facade
    for those tools while continuing to own the `kill_task` write tool;
    the registry consumes the canonical read-only tools. Both read-only tools
    observe managed tasks only through the background-task port and consume
    completion bookkeeping via `mark_completions_reported`; task-id
    validation, output preview and truncation, error metadata, cancellation,
    redaction, and registry gating remain unchanged. The background task
    manager, bash, and process ownership are intentionally deferred.
40. Stage 2Y establishes `neuro_code.infrastructure.tools.client_terminal`
    as the canonical owner of the read-only ACP client terminal tools
    (`ClientTerminalOutputTool` and `ClientTerminalWaitTool`) and their shared
    task-id/wait/rendering and capability-gating helpers. The former
    `neuro_code.tools.client_terminal` path remains a compatibility facade for
    those tools while continuing to own `terminal_exec`, `terminal_start`, and
    `terminal_kill`; the registry consumes the canonical read-only tools. Both
    read-only tools observe managed tasks only through the `ClientTerminal`
    port's `get`/`wait` methods; task-id validation, wait mode/timeout bounds,
    output preview and truncation, error metadata, sandbox gating, redaction,
    and cancellation remain unchanged. Terminal session ownership, ACP
    capability negotiation, and the foreground execution path are
    intentionally deferred.
41. Stage 3A starts the Runtime Kernel split by establishing
    `neuro_code.application.runtime.tool_pipeline` as the canonical owner of
    the typed tool-observation collaborator `ToolObservationBuilder`. The
    builder owns metadata-fact allowlisting, workspace/background progress
    tokens, plan-from-tool-result parsing, and the fail-open observation
    construction previously embedded in `AgentRuntime`; `agent.py` keeps the
    `AgentRuntime`/`AgentRunResult` identity and retains the fail-open logging
    at its call site. Event ordering, tool result pairing, cancellation,
    supervision checkpoints, and SessionStore transactions remain unchanged;
    `run()` control flow is not rewired in this slice.
42. Stage 3B establishes `neuro_code.application.runtime.event_recorder` as
    the canonical owner of the per-turn `TurnEventRecorder` collaborator. The
    recorder owns the event sequence and `emit` persistence/delivery,
    session-task finishing, turn-failure recording, and terminal completion
    recording previously embedded in `AgentRuntime.run()` closures.
    `AgentRuntime` binds the recorder's methods as local names, so all call
    sites, event ordering, cancellation, pristine-rewind tracking, and
    SessionStore transaction boundaries remain unchanged; `run()` control
    flow is still not rewired.
43. Stage 3C establishes `neuro_code.application.runtime.context_builder` as
    the canonical owner of the `ContextBuilder` collaborator. The builder owns
    request-scoped reasoning/interaction/plan guidance, repository
    instruction refresh, skill listing injection, and the mutable
    `reasoning_effort`/`interaction_mode`/`plan`/`plan_comments` state.
    `AgentRuntime` keeps its public properties and setters as thin delegates
    and retains `_model_items_with_reasoning_guidance` as a private
    delegation seam used by existing tests. Guidance injection order, plan
    comment validation, permission-mode application, and per-step refresh
    semantics remain unchanged; `run()` control flow is still not rewired.
44. Stage 3D completes the tool pipeline slice: `ToolExecutor` joins
    `ToolObservationBuilder` in `neuro_code.application.runtime.tool_pipeline`
    as the canonical owner of tool execution. The executor owns permission
    decisions and interactive approval, tool dispatch and execution,
    workspace snapshot/report capture, plan handoff persistence, and
    unstarted-call recording previously embedded in `AgentRuntime`;
    `AgentRuntime` delegates `_execute_tool` call sites to the executor.
    Tool/ToolResult pairing, event ordering, cancellation, redaction,
    workspace-change timing, and SessionStore transactions remain unchanged;
    `run()` control flow is still not rewired.
45. Stage 3E establishes `neuro_code.application.runtime.model_step` as the
    canonical owner of per-step provider stream normalization. The
    `ModelStepProcessor` owns the seven model-event branches, thinking-
    completion timing, provider-origin adoption bookkeeping, and pristine
    cancel-eligibility updates; `ModelStepResult` carries normalized step
    text, reasoning, tool calls, and completion state. `AgentRuntime.run()`
    now consumes one processor per step via an `on_imperfect` callback so the
    pristine-rewind flag stays owned by the event recorder. Event ordering,
    provider events, cancellation, and step persistence remain unchanged;
    `run()` control flow is still not rewired.
46. Stage 3F completes the Runtime Kernel split: `agent_loop` becomes the
    canonical owner of the per-turn main loop (`AgentLoopRunner`) and the
    turn result value (`AgentRunResult`). The runner owns the step loop,
    supervision checkpoint sequence, batch decisions, finalization
    orchestration, and evidence collection previously embedded in
    `AgentRuntime.run()`; `AgentRuntime.run()` is now a thin delegate and
    re-exports `AgentRunResult`/`EventSink` for compatibility. Event ordering,
    tool result pairing, cancellation, transaction, and batch boundaries
    remain unchanged.

47. Stage 4A establishes `neuro_code.application.sessions` as the first
    application-use-case seam. `StartSessionRequest`, `SessionInspection`,
    and `SessionApplicationService` provide typed start/inspect operations
    over the `SessionStore` port. `ApplicationComposition` owns one service
    instance for inbound adapters to share. The service returns only safe
    session and execution-record projections; it does not expose messages,
    prompts, tool arguments, SQLite details, or runtime control. Session
    creation remains the storage adapter's atomic create operation, while the
    post-create summary read and execution-record projection are deliberately
    separate operations. `AgentConversation`, AgentRuntime, CLI, TUI, and ACP
    behavior are not rewired in this slice.

48. Stage 4B establishes the typed turn seam beside that session service:
    `RunTurnRequest` carries only prompt/content parts, cancellation policy,
    turn source, and an optional expected session identity;
    `ResumeSessionRequest` provides a validated read-only resume preflight.
    `SessionTurnService` binds an existing conversation runner without taking
    ownership of its lock, task scope, persisted context, event sink, or
    cancellation recovery. `SessionApplicationService.prepare_resume()` only
    returns the safe summary/execution-record projection; the existing
    `AgentConversation.open()` and composition binding remain the owners of
    workspace/sandbox validation and context reconstruction. No CLI/TUI/ACP
    entrypoint is rewired and no model turn is started by resume preflight.

49. Stage 4C wires only the CLI one-shot path through the application turn
    seam. A resumed CLI session first performs the read-only
    `ResumeSessionRequest` preflight, then the existing composition creates its
    binding and `SessionApplicationService.bind_runner()` executes a validated
    `RunTurnRequest`. The bound runner still owns its turn lock, task scope,
    persisted context, event sink delivery, cancellation recovery, and close
    order; the CLI keeps its plain/JSON/JSONL rendering and error behavior.
    TUI and ACP remain on their existing paths until their own bounded slices.

50. Stage 4D wires ACP prompts through the same application turn seam without
    changing the ACP wire protocol. `AcpApplicationService` reuses the
    composition-provided `SessionApplicationService`; `NeuroCodeAcpAgent.prompt()`
    creates a `RunTurnRequest` with converted content parts and the known
    internal session identity, then delegates to `SessionTurnService`. Existing
    ACP alias resolution, resume preparation, binding ownership, event mapping,
    cancellation, ProviderError mapping, and cleanup remain in their existing
    owners. CLI and TUI behavior are not changed by this slice.

51. Stage 4E wires the TUI user-turn and resume boundaries through the shared
    session application seam. The bootstrap performs a read-only
    `ResumeSessionRequest` preflight for command-line resume and for interactive
    session selection, then binds the existing `ProfileConversationController`
    through `SessionTurnService`. `NeuroCodeApp` uses that service only for user
    turns; background wake and plan operations remain on their existing
    controller contracts. The controller accepts typed content parts while
    preserving its turn lock and runner lifecycle. Layout, shortcuts, streaming,
    cancellation, persistence, and TUI rendering behavior remain unchanged.

52. Stage 4F adds `ForkSessionRequest` and
    `SessionApplicationService.fork_session()` as the shared typed application
    use case for durable session copies. The service validates only the opaque
    source-session intent and delegates the atomic `SessionStore.fork_session()`
    operation; it does not expose SQLite rows, context, messages, or provider
    state. The ACP adapter now calls this shared service after retaining its
    existing workspace, alias, binding, MCP, publication, and rollback gates.
    No CLI/TUI behavior, runtime turn, permission flow, or storage transaction
    semantics are changed by this slice.

53. Stage 4G establishes the typed `ApproveToolRequest` and
    `ToolApprovalService` seam for interactive tool approval. The service
    accepts only the already bounded `PermissionRequest` contract and
    delegates to the existing `PermissionApprover` port; it never owns policy,
    session approval caching, UI handlers, raw arguments, or tool execution.
    `ApplicationComposition` wraps each binding's approver with this service
    before constructing the runtime, so CLI/TUI/ACP bindings share the same
    application boundary without changing approval order, cancellation,
    fail-closed behavior, or persistence.

54. Stage 4H establishes `ProviderChangeService` as a non-owning
    application seam for the `ChangeProviderRequest` use case.  The existing
    `ProfileConversationController` remains the sole owner of provider
    availability checks, turn locking, fresh `ConversationBinding` creation,
    policy propagation, and old/new background-task scope shutdown.  The
    bootstrap binds that owner to the typed facade and the TUI calls
    `change_provider()` with a validated request.  No provider protocol,
    session persistence, model turn, cancellation, or resource ownership
    semantics are changed; other inbound adapters remain on their existing
    paths until separately audited.

55. Stage 4I establishes `PlanExecutionService` as the first typed application
    workflow seam for `ExecutePlanRequest`. The existing
    `ProfileConversationController` remains the owner of saved-plan validation,
    turn locking, SessionTask lifecycle, permissions, event delivery, and
    cancellation. `ApplicationComposition` binds that owner and the TUI's
    direct `/execute-plan` entry submits a typed request through the facade.
    Queued task scheduling and `run_session_task()` remain on their existing
    controller path until a separate lifecycle slice is audited; no workflow
    engine, AgentRuntime rewrite, or new persistence is introduced.

56. Stage 4J establishes `QueuedPlanExecutionService` as the typed application
    seam for the explicit `/run-task <task_id>` entry. `RunSessionTaskRequest`
    carries only a validated task identity; the existing conversation and
    runtime owners continue to perform the atomic queued-to-running claim,
    plan snapshot validation, task completion/failure/cancellation update,
    event delivery, and turn locking. Plan scheduling and its SessionStore
    create operation remain on the existing controller path until a separate
    slice is audited; this stage adds no new state machine or persistence.

57. Stage 4K establishes `PlanSchedulingService` as the typed application seam
    for the `/schedule-plan` entry. `SchedulePlanRequest` is an empty,
    immutable command because scheduling operates on the current saved plan;
    it carries no SQLite identity or plan copy. The existing conversation and
    profile controller remain the owners of saved-plan/session validation,
    turn locking, queued-task limits, `SessionTask` creation, and
    `SessionStore.create_session_task()`. The TUI uses the facade when it is
    bound and retains its controller fallback for legacy/test construction.
    This stage does not change queue state transitions, persistence
    transactions, cancellation, or task execution.

58. Stage 5A establishes `neuro_code.infrastructure.persistence.sqlite_session`
    as the canonical public `SqliteSessionStore` identity. Bootstrap composition
    now constructs the store from this infrastructure module, while
    `neuro_code.adapters.sqlite_session` remains a compatibility facade for
    existing imports and low-level SQLite test seams. The schema version,
    migrations, connection locking, transactions, and `SessionStore` port are
    deliberately unchanged in this first persistence slice; relocating the
    implementation body is a separate, audited step.

59. Stage 5B moves the SQLite SessionStore implementation body and all of its
    schema, migration, serialization, search, and row-conversion helpers into
    `neuro_code.infrastructure.persistence.sqlite_session`. The former
    adapter is now a one-way compatibility facade that re-exports the exact
    canonical class object. Production composition and entrypoints continue to
    use the infrastructure path, while low-level tests patch implementation
    seams at their canonical owner. Schema version, migration ordering,
    connection locking, transaction boundaries, port behavior, and public
    SessionStore semantics remain unchanged.

60. Stage 5C moves both session-owned MCP transport implementations into
    `neuro_code.infrastructure.mcp.stdio` and
    `neuro_code.infrastructure.mcp.http`. The old `mcp_stdio` and `mcp_http`
    adapter modules remain one-way compatibility facades, and bootstrap plus
    the HTTP transport import the canonical stdio owner directly. MCP limits,
    redaction, cancellation, process ownership, official SDK behavior, and
    ACP wiring remain unchanged; the split is an ownership move only.

61. Stage 5D moves the filesystem workspace path-boundary and bounded change
    observation implementations into
    `neuro_code.infrastructure.workspace.paths` and
    `neuro_code.infrastructure.workspace.changes`. The old top-level
    `workspace.py` and `workspace_changes.py` modules remain one-way
    compatibility facades, while bootstrap and read-only filesystem tools
    import the canonical owners. Workspace identity matching, path escape
    rejection, additional-root boundaries, snapshot limits, redaction,
    diff serialization, observer checkpoints, permission/sandbox ordering,
    and runtime workspace-report semantics remain unchanged.

62. Stage 5E moves the concrete model-provider adapters, failover chain,
    image-reference helpers, and provider factory into
    `neuro_code.infrastructure.providers`. The old `neuro_code.providers`
    package and provider submodules remain one-way compatibility facades, and
    bootstrap uses the canonical factory. Provider request payloads,
    `ModelToolPolicy`, streaming events, failover ordering, cancellation, and
    error semantics remain unchanged; this is an ownership move rather than a
    provider protocol or runtime behavior change.

63. Stage 5F moves the Bash shell-command tool implementation into
    `neuro_code.infrastructure.tools.bash`. The former
    `neuro_code.tools.bash` module remains a one-way compatibility facade, and
    the canonical registry imports Bash lazily from infrastructure. Permission
    and sandbox checks, foreground/background promotion, process-tree
    termination, bounded output, cancellation, timeout, and ToolResult
    semantics remain unchanged; write-tool and background-manager ownership is
    not migrated by this stage.

64. Stage 5G moves the atomic `SearchReplaceTool` implementation into the
    canonical `neuro_code.infrastructure.tools.filesystem` owner alongside
    the read-only filesystem tools. The former `neuro_code.tools.filesystem`
    module remains a one-way facade, and the canonical registry imports the
    write tool from infrastructure. Workspace-path resolution, instruction
    preflight, permission/sandbox boundaries, client-filesystem delegation,
    atomic replacement, redaction, cancellation, and ToolResult semantics are
    unchanged; no other write tool or background manager is moved here.

65. Stage 5H moves the direct `ClientTerminalTool` (`terminal_exec`) implementation
    into the canonical `neuro_code.infrastructure.tools.client_terminal` owner.
    The legacy `neuro_code.tools.client_terminal` module remains the owner of
    session-affecting `terminal_start` and `terminal_kill` tools and re-exports
    the moved execution tool. Client-terminal capability and sandbox checks,
    command/argument/timeout validation, bounded output, status validation,
    cancellation and ToolResult metadata remain unchanged; terminal session
    lifecycle ownership is intentionally not migrated by this stage.

66. Stage 5I completes the canonical client-terminal tool owner by moving
    `ClientTerminalStartTool` and `ClientTerminalKillTool` into
    `neuro_code.infrastructure.tools.client_terminal`. The legacy
    `neuro_code.tools.client_terminal` module becomes a one-way facade for all
    five client-terminal tools, and the canonical registry imports the complete
    family from infrastructure. Client-terminal port calls, capability and
    sandbox checks, task lifecycle, cancellation, bounded output, metadata,
    and ToolResult semantics remain unchanged; no background task manager is
    migrated by this stage.

67. Stage 5J completes the canonical background-task tool owner by moving
    `KillTaskTool` into `neuro_code.infrastructure.tools.background_tasks`
    alongside `TaskOutputTool` and `WaitTasksTool`. The former
    `neuro_code.tools.background_tasks` module is now a one-way compatibility
    facade, and the canonical registry imports the complete background-tool
    family from infrastructure. Background-task manager, process ownership,
    SQLite bookkeeping, cancellation, completion-reporting, permissions,
    and `ToolResult` semantics remain unchanged; this stage moves only the
    tool implementation owner.

68. Stage 5K moves the process-backed `LocalBackgroundTaskManager` implementation
    into `neuro_code.infrastructure.background_tasks`. The former
    `neuro_code.adapters.background_tasks` module becomes a one-way compatibility
    facade, and bootstrap composes the canonical owner directly. The
    `BackgroundTaskManager`/`BackgroundTaskSupervisor` ports, process-tree
    ownership, scope isolation, bounded output, timeout, cancellation, task
    retention, completion reporting, and public snapshot semantics remain
    unchanged; SQLite persistence and Runtime lifecycle ownership are not moved.

69. Stage 5L moves the bounded filesystem instruction-discovery implementation into
    `neuro_code.infrastructure.workspace.instructions`. The former
    `neuro_code.adapters.instruction_discovery` module remains a one-way compatibility
    facade for `FilesystemInstructionDiscovery`; bootstrap, skill discovery, and the
    canonical skill tool import the shared safety helpers from the canonical owner.
    Workspace-boundary checks, symlink/reparse rejection, TOCTOU-resistant bounded reads,
    encoding/control-character validation, fingerprints, limits, redaction, and
    instruction-tracker semantics remain unchanged. Skill discovery's own implementation
    owner is deliberately not migrated by this stage.

70. Stage 5M moves the bounded `FilesystemSkillDiscovery` implementation into
    `neuro_code.infrastructure.workspace.skills`. The former
    `neuro_code.adapters.skill_discovery` module remains a one-way compatibility
    facade, and bootstrap plus skill-discovery tests use the canonical owner.
    Skill scope ordering, git-root and home/workspace boundaries, symlink/reparse
    rejection, bounded traversal and reads, frontmatter parsing, substitutions,
    deduplication, fingerprints, redaction, cancellation, and tracker/tool
    semantics remain unchanged. The `SkillTool` implementation itself is not
    moved by this stage.

71. Stage 5N moves the atomic `JsonUiPreferencesStore` implementation into
    `neuro_code.infrastructure.persistence.ui_preferences`. The former
    `neuro_code.adapters.ui_preferences` module remains a one-way compatibility
    facade, while bootstrap and the direct persistence tests use the canonical
    owner. Preference schema validation, English/high/normal defaults, serialized
    writes, private file permissions, atomic replacement, write serialization,
    and the `UiPreferencesStore` port remain unchanged; the larger Rust session
    importer is intentionally deferred to a separate stage.

72. Stage 5O moves the read-only upstream Rust session importer into
    `neuro_code.infrastructure.persistence.rust_session`. The former
    `neuro_code.adapters.rust_session` module remains a one-way compatibility facade,
    while bootstrap, the Rust import tests, and the persistence aggregate expose the
    canonical owner. Summary and JSONL safety limits, timestamp and sandbox validation,
    bounded content conversion, preserved context handling, source immutability, and
    `SessionError` behavior remain unchanged; this stage changes implementation ownership
    only and does not alter SessionStore, resume, Runtime, Provider, or session protocol
    semantics.

73. Stage 5P moves the deterministic permission-policy implementation into
    `neuro_code.application.permissions.policy`. The former root
    `neuro_code.permissions` module remains a one-way compatibility facade,
    while Runtime, settings, composition, CLI, and terminal-session owners
    import the canonical policy directly. Permission modes, effects, rule
    matching, bash-command analysis, approval contracts, redaction, and all
    permission-denied behavior remain unchanged; the stage changes only the
    policy implementation owner and explicitly keeps approval contracts out
    of the policy module.

74. Stage 5Q establishes `neuro_code.domain.permissions.bash_commands` as the
    canonical owner of the pure, conservative Bash command decomposition used
    by permission decisions. The former root `neuro_code.bash_commands` module
    remains a one-way compatibility facade and application permission policy
    and approval contracts import the canonical domain module directly.
    Tokenization, wrapper handling, fail-closed classification, recursion
    limits, redaction boundaries, and permission behavior remain unchanged;
    this stage changes only the value/parser ownership boundary.

75. Stage 5R establishes `neuro_code.configuration.app` as the canonical owner
    of application configuration loading, provider-profile models, proxy policy,
    and configuration overrides. The former `neuro_code.config` module remains
    a one-way compatibility facade, including the historical `Path` patch seam.
    Configuration parsing, managed-provider settings integration, sandbox and
    provider override behavior, proxy resolution, redaction, and error semantics
    remain unchanged; this stage changes only implementation ownership and adds
    canonical import-isolation coverage.

76. Stage 5S establishes `neuro_code.domain.sessions.search` as the canonical
    owner of pure session-search projections, fallback title generation, and
    searchable-text construction. The former `neuro_code.domain.session_search`
    module remains a one-way compatibility facade, while domain aggregates,
    storage ports, SQLite persistence, runtime session search, CLI, bootstrap,
    and tests import the canonical owner. Search scoring/value validation,
    system-reminder filtering, provider-private context exclusion, and session
    persistence behavior remain unchanged; this stage changes only domain
    ownership and import boundaries.

77. Stage 5T establishes `neuro_code.domain.conversation.interaction_mode` as
    the canonical owner of the pure conversation operating-mode enum and its
    provider-neutral guidance. The former `neuro_code.domain.interaction_mode`
    module remains a one-way compatibility facade, while domain aggregates,
    application ports/runtime, infrastructure preference persistence, bootstrap,
    TUI, and tests import the canonical owner. Mode values, glyphs, cycling,
    guidance text, permission mapping, persistence, and user-visible behavior
    remain unchanged; this stage changes only domain ownership and import
    boundaries.

78. Stage 5U establishes `neuro_code.domain.conversation.reasoning` as the
    canonical owner of the provider-neutral `ReasoningEffort` value and review
    guidance. The former `neuro_code.domain.reasoning` module remains a one-way
    compatibility facade, while model context, application settings/ports/
    runtime, bootstrap, CLI, ACP, TUI, persistence, and tests import the
    canonical owner. Effort values, glyphs, effective `ultracode` fallback,
    guidance text, preference persistence, and user-visible behavior remain
    unchanged; no provider-private reasoning parameter or workflow orchestration
    is introduced by this stage.

79. Stage 5V establishes `neuro_code.application.memory.instruction_tracker` as
    the canonical owner of the binding-scoped AGENTS.md discovery tracker. The
    former `neuro_code.application.runtime.instruction_tracker` module remains
    a one-way compatibility facade, while Composition imports the application
    memory owner directly. Workspace boundaries, subtree isolation, fresh
    discovery, write pre-flight checks, instruction injection, and all tracker
    behavior remain unchanged; skill tracking and the Runtime loop are
    intentionally outside this stage.

80. Stage 5W establishes `neuro_code.application.memory.skill_tracker` as the
    canonical owner of the binding-scoped SKILL.md discovery tracker. The
    former `neuro_code.application.runtime.skill_tracker` module remains a
    one-way compatibility facade, while Composition imports the application
    memory owner directly. Workspace boundaries, subtree isolation, fresh
    discovery, and skill injection behavior remain unchanged; this stage
    changes only the application ownership and import boundary.

81. Stage 5X establishes `neuro_code.application.ports.provider_catalog` as
    the canonical owner of `ProviderConnectionSpec`, `ProviderCatalogResult`,
    `ProviderCatalogError`, and the `ProviderCatalog` port. The former
    `neuro_code.domain.provider_catalog` module remains a one-way compatibility
    facade and is classified as a legacy port path. Credential-bearing probe
    input, bounded catalog output, redacted error behavior, and HTTP provider
    discovery remain unchanged; this stage changes only contract ownership and
    import boundaries.

82. Stage 5Y establishes `neuro_code.application.ports.provider_settings` as
    the canonical owner of `ManagedProviderProfile`, `ManagedProviderSettings`,
    `ManagedProxyPolicy`, and the `ProviderSettingsStore` port. The former
    `neuro_code.domain.provider_settings` module remains a one-way compatibility
    facade and is classified as a legacy port path. Managed profile validation,
    proxy and background-wake override semantics, credential redaction, JSON
    persistence, and TUI behavior remain unchanged; this stage changes only
    contract ownership and import boundaries.

83. Stage 5Z establishes `neuro_code.shared.ui_language` as the canonical owner
    of the cross-layer `UiLanguage` primitive. The former
    `neuro_code.domain.ui_preferences` module remains a one-way compatibility
    facade. UI preference ports, persistence, TUI, and localized text import the
    shared owner directly; language values, persistence format, and UI behavior
    remain unchanged. This stage changes only the shared primitive ownership and
    import boundary.

84. Stage 5AA establishes `neuro_code.domain.workspace.instructions` as the
    canonical owner of the pure AGENTS.md instruction value objects and bounded
    instruction projection helpers. The former `neuro_code.domain.instructions`
    module remains a one-way compatibility facade, while the filesystem
    discovery implementation stays in `neuro_code.infrastructure.workspace.instructions`.
    Instruction validation, fingerprints, synthetic-message construction,
    discovery limits, redaction boundaries, and tracker behavior remain
    unchanged; this stage changes only the domain ownership and import boundary.
85. Stage 5AB establishes `neuro_code.domain.workspace.skills` as the canonical
    owner of the pure SKILL.md metadata value objects, bounded parsing and
    projection helpers. The former `neuro_code.domain.skills` module remains a
    one-way compatibility facade, while filesystem discovery stays in
    `neuro_code.infrastructure.workspace.skills` and skill-body loading stays
    in the read-only infrastructure tool. Skill validation, substitution,
    fingerprints, catalog rendering, synthetic-message construction and
    discovery behavior remain unchanged; this stage changes only the domain
    ownership and import boundary.
86. Stage 5AC establishes `neuro_code.application.sessions.profile_conversation`
    as the canonical owner of the application session/profile coordinator and
    its typed binding and selection projections. The former
    `neuro_code.application.runtime.profile_conversation` module remains a
    one-way compatibility facade. Provider selection, session selection,
    interaction-mode and reasoning-effort policies, turn serialization,
    binding replacement, background-task scope shutdown, and runner delegation
    remain unchanged; this stage changes only application ownership and import
    boundaries and does not move the Runtime Kernel, Provider, SessionStore,
    workflow execution, or inbound UI protocol.

87. Stage 5AD establishes `neuro_code.application.sessions.terminal_sessions`
    as the canonical owner of the bounded interactive terminal session
    implementation. The former
    `neuro_code.application.runtime.terminal_sessions` module remains a
    one-way compatibility facade. Permission, workspace, matching-sandbox,
    output-ring, process-lifecycle, cancellation, shutdown, and
    `InteractiveTerminalManager` port behavior remain unchanged; this stage
    changes only application ownership and import boundaries and does not move
    native platform adapters, the AgentRuntime kernel, ACP framing, or the
    terminal wire contract.

88. Stage 5AE establishes `neuro_code.application.sessions.conversation` as
    the canonical owner of the multi-turn `AgentConversation` controller. The
    former `neuro_code.application.runtime.conversation` module remains a
    one-way compatibility facade. Turn locking, session resume, provider-origin
    checks, plan/task coordination, background-wake delegation, execution-record
    reloads, cancellation recovery, and all existing runtime/provider/storage
    behavior remain unchanged; this stage changes only application ownership and
    import boundaries and does not split the Runtime Kernel or inbound protocols.

89. Stage 5AF establishes `neuro_code.application.permissions.broker` as the
    canonical owner of `ApprovalHandler` and `SessionApprovalBroker`. The former
    `neuro_code.application.runtime.approval` module remains a one-way
    compatibility facade. Interactive approval routing, session-scope caching,
    fail-closed behavior when no UI handler is available, approval identity,
    cancellation, and tool execution behavior remain unchanged; this stage
    changes only application ownership and import boundaries.

90. Stage 5AG completes the production import convergence for the canonical
    sandbox policy owner `neuro_code.domain.sandbox.models`. The former
    `neuro_code.domain.sandbox` package-level module remains a one-way
    compatibility facade, while application, infrastructure, bootstrap, and
    configuration consumers import `SandboxProfile` from the models owner
    directly. Sandbox profile parsing, fail-closed policy behavior, process
    isolation, persistence, cancellation, and user-visible behavior remain
    unchanged; this stage changes only import boundaries and adds a negative
    contract preventing production code from reintroducing the facade import.
91. Stage 5AH completes the production import convergence for the canonical
    terminal value-object owner `neuro_code.domain.terminal.models`. The
    application terminal port, application terminal-session owner, POSIX PTY,
    Windows PTY, and the domain aggregate import the terminal models directly;
    the former `neuro_code.domain.terminal` package remains a one-way
    compatibility facade. Terminal size validation, output-ring semantics,
    signal handling, PTY lifecycle, cancellation, and public import identity
    remain unchanged. A negative import contract prevents production code from
    reintroducing the facade dependency; this stage changes only the value
    object import boundary.
92. Stage 5AI completes the production import convergence for the canonical
    application configuration owner `neuro_code.configuration.app`. Bootstrap,
    CLI type contracts, TUI proxy-policy resolution, and the infrastructure
    provider factory now import configuration contracts directly from that
    owner. The former `neuro_code.config` module remains a one-way compatibility
    facade, including its historical `Path.home` patch seam. Configuration
    loading, provider/sandbox overrides, proxy validation, redaction, and
    runtime behavior remain unchanged; a negative import contract prevents
    production code from reintroducing the facade dependency.
93. Stage 5AJ completes the production import convergence for the canonical
    background-task domain value owner `neuro_code.domain.background_tasks.models`.
    Application ports/runtime/session modules, configuration, ACP/TUI, the
    background manager and infrastructure tools/persistence now import the
    models owner directly; the former `neuro_code.domain.background_tasks`
    module remains a one-way compatibility facade. Wake-ledger validation,
    task snapshot/result semantics, background execution, cancellation,
    persistence and UI behavior remain unchanged. A negative import contract
    prevents production code from reintroducing the facade dependency.
94. Stage 5AK completes the remaining production import convergence for the
    shared `UiLanguage` owner `neuro_code.shared.ui_language`. The domain
    aggregate now imports the shared primitive directly; the former
    `neuro_code.domain.ui_preferences` module remains a one-way compatibility
    facade for legacy callers. Language values, UI preference persistence,
    localization, TUI behavior, and public identity remain unchanged. A
    negative import contract prevents production code from reintroducing the
    domain facade dependency.
95. Stage 5AL audits the compatibility-facade quarantine after the latest
    consumer convergence. An explicit AST contract enumerates the migrated
    legacy paths and rejects imports of those paths from every other
    production module; each facade's own re-export file and public aggregate
    entry point are excluded because they are the compatibility boundary
    itself. The audit confirms that the remaining imports are facade-owned or
    compatibility-test imports, and it does not delete any facade or alter
    runtime behavior. A versioned removal decision and evidence from external
    callers remain required before compatibility paths can be removed.
96. Stage 5AM completes the remaining production import convergence in the
    public `neuro_code.tools` aggregate. Its exports now import directly from
    `neuro_code.infrastructure.tools.*` owners instead of importing the
    compatibility submodules. The legacy tool modules remain one-way facades
    for callers that still use the old paths; exported object identity, lazy
    registry behavior, tool permissions, sandboxing, cancellation, and output
    semantics remain unchanged. No compatibility facade is deleted, and the
    versioned removal decision still requires external-caller evidence.
97. Stage 5AN consolidates the conversation-domain compatibility quarantine.
    The legacy paths `neuro_code.domain.messages`, `events`,
    `model_events`, `model_context`, and `context_usage` are now included in
    the central explicit facade inventory. Production code already imports
    the canonical `neuro_code.domain.conversation.*` owners, while existing
    identity and legacy-import tests continue to cover callers of the old
    paths. This stage changes only the architecture guard; it does not delete
    facades or change message, event, context, provider, or runtime behavior.
98. Stage 5AO establishes a typed session-catalog application seam for the
    CLI. `SessionApplicationService` now owns bounded list/search/rename use
    cases and returns safe session inspections, including the durable execution
    projection without exposing messages, prompts, tool arguments, or snapshots.
    The CLI remains the inbound adapter and preserves its plain, JSON, and
    validation behavior; SQLite, SessionStore semantics, runtime execution,
    TUI, ACP, and provider behavior are unchanged. The service explicitly does
    not claim bulk-read or cross-row transaction atomicity; a later slice may
    optimize projections without moving storage ownership into the interface.
99. Stage 5AP routes the TUI workspace session catalog, search, and rename
    closures through the existing `SessionApplicationService`. Workspace
    matching remains a bootstrap composition policy, while storage access,
    safe projections, resume preflight, and title mutation stay behind the
    application seam. Existing session binding, provider selection, workspace
    filtering, error messages, and TUI behavior remain unchanged; this stage
    does not move the TUI package, add a new wire protocol, or optimize the
    intentionally bounded projection reads.
100. Stage 5AQ adds an injected `SessionWorkspaceMatcher` seam to
     `SessionApplicationService`. `ApplicationComposition` supplies the
     existing filesystem workspace policy, while the application service owns
     bounded workspace list/search projections. TUI consumers no longer repeat
     storage queries or filtering mechanics; ACP pagination and alias behavior
     remain in its dedicated service because they require cursor scanning and
     external-ID protocol semantics. No storage schema, bulk-read guarantee,
     provider, runtime, or user-visible protocol changes are introduced.
101. Stage 5AR adds an ordered bulk execution-record projection to the
     `SessionStore` port and its canonical SQLite implementation. CLI session
     list/search use one bounded read snapshot instead of loading one execution
     record per result; requested order, duplicate IDs, missing records, and
     invalid completion-event errors remain explicit and deterministic. The
     bulk operation is read-only and does not claim atomicity with session
     events or execution-record writes. TUI workspace catalog, ACP cursor/
     alias behavior, runtime execution, schema, and user-visible protocols
     remain unchanged.
102. Stage 5AS adds a typed keyset-page query to
     `SessionApplicationService` and routes ACP's safe summary page read
     through it. The application seam validates cursor fields and returns only
     `SessionSummary` values; ACP retains ownership of cursor tokens, scan
     limits, workspace filtering, alias allocation, and wire serialization.
     CLI offset/search projections and TUI workspace projections remain on
     their existing contracts. No execution projection, storage schema,
     provider, runtime, or ACP wire field changes are introduced.
103. Stage 5AT audits the compatibility boundary after the Stage 5AR bulk
     execution projection and Stage 5AS application page seam. The canonical
     `SqliteSessionStore` implements all 37 `SessionStore` protocol methods;
     the only repository alternatives are deliberately partial test doubles
     used through explicit casts, and `neuro_code.adapters.sqlite_session`
     remains a one-way identity-preserving facade. No production fallback or
     second storage protocol is introduced without evidence of an external
     implementor. Existing single-record/page methods remain available, and
     removal of compatibility paths requires external-caller evidence and a
     versioned deprecation window.
104. Stage 5AU adds the typed `DeleteSessionRequest` and
     `SessionApplicationService.delete_session()` use case. ACP retains
     workspace visibility checks plus alias, active-binding, and protocol
     cleanup, then delegates only the store-owned delete through the
     application seam. Delete semantics, errors, aliases, session lifecycle,
     schema, Runtime, Provider, and ACP wire fields remain unchanged.
105. Stage 5AV adds the typed `GetSessionSummaryRequest` and
     `SessionApplicationService.get_session_summary()` use case. ACP uses
     this seam for its workspace visibility preflight before fork/delete,
     while ACP alias operations and workspace matching remain adapter-owned.
     The summary read does not load execution records or messages, and no
     storage schema, Runtime, Provider, Finalizer, or ACP wire behavior changes.
106. Stage 5AW audits the three `SessionStore` alias operations. The only
     production consumer is ACP, and its `acp-v1` namespace, legacy raw-ID
     fallback, external-ID allocation, alias uniqueness, and protocol error
     mapping are ACP-specific. Because no second inbound consumer or stable
     cross-interface alias contract exists, alias ownership remains in
     `AcpApplicationService`; no generic application alias DTO or second
     protocol is introduced.
107. Stage 5AX routes `ApplicationComposition.config_for_session_resume()`
     through `SessionApplicationService.get_session_summary()`. Composition
     continues to own provider restoration, workspace matching, sandbox
     compatibility, and context-affinity selection; the application seam only
     supplies the persisted summary. Initial sandbox pinning, conversation
     context loading, CLI output, ACP behavior, schema, Runtime, and Provider
     behavior remain unchanged.
108. Stage 5AY adds the typed `ExportSessionRequest` and `SessionExport`
     projection to `SessionApplicationService`. The application seam now owns
     the persisted summary/item/event reads needed by an explicit export,
     while the CLI retains Markdown/JSON rendering, the `schema_version=4`
     payload, output-path handling, and the existing explicit export boundary
     for raw conversation/tool data. Markdown skips event reads; JSON opts in
     to them. No storage schema, Runtime, Provider, Finalizer, ACP, TUI, or
     session export field changes are introduced.
109. Stage 5AZ adds the typed `LoadSessionItemsRequest` use case to
     `SessionApplicationService` and routes `AgentConversation` resume and
     persisted-state reloads through it. The seam owns only the ordered durable
     `SessionItem` read; plan loading, execution-record loading, workspace and
     sandbox validation remain separate owners. Existing read order, context
     contents, cancellation recovery, SessionStore/SQLite behavior, Runtime,
     Provider, Finalizer, ACP, CLI, and TUI behavior remain unchanged.
110. Stage 5BA adds the typed `LoadExecutionRecordRequest` use case to
     `SessionApplicationService`. `inspect_session()` and `AgentConversation`
     resume/reload paths use this safe execution projection seam; the storage
     parser, missing-record behavior, invalid terminal-event errors, execution
     record persistence, Runtime, Provider, Finalizer, ACP, CLI, and TUI
     behavior remain unchanged.
111. Stage 5BB adds the typed `LoadSessionPlanRequest` use case to
     `SessionApplicationService`. `AgentConversation` resume and persisted-state
     reloads use this application-owned plan read, while plan comments, plan
     mutation, task scheduling, Provider-origin restoration, and workspace/
     sandbox policy remain with their existing owners. Plan contents, comment
     loading order, SessionStore/SQLite behavior, Runtime, Provider, Finalizer,
     ACP, CLI, and TUI behavior remain unchanged.
112. Stage 5BC adds the typed `ListPlanCommentsRequest` use case to
     `SessionApplicationService`. `AgentConversation` resume/reload and its
     read-only comment listing use this application read seam; comment writes,
     plan mutation, task scheduling, Provider-origin restoration, and
     workspace/sandbox policy remain with their existing owners. The plan
     fingerprint, comment ordering, SessionStore/SQLite behavior, Runtime,
     Provider, Finalizer, ACP, CLI, and TUI behavior remain unchanged.
113. Stage 5BD reuses the existing typed `GetSessionSummaryRequest` seam for
     `AgentConversation` resume and Provider-origin reloads. Conversation
     still owns workspace/sandbox validation and source-field assignment; the
     application service only supplies the persisted summary. No new DTO,
     Provider/configuration behavior, SessionStore/SQLite schema, Runtime,
     Finalizer, ACP, CLI, or TUI behavior is introduced.
114. Stage 5BE adds typed `ListSessionTasksRequest` and
     `GetSessionTaskRequest` reads to `SessionApplicationService`. The
     conversation's task listing, queued-count check, and queued-task lookup
     now cross the application seam; task creation, start, finish, permissions,
     and execution remain owned by the existing conversation/runtime
     lifecycle owners. Task ordering, bounds, status validation, SessionStore/
     SQLite behavior, Runtime, Provider, Finalizer, ACP, CLI, and TUI behavior
     remain unchanged.
115. Stage 5BF audits the remaining plan/task writes instead of moving them
     mechanically. Plan scheduling, direct plan execution, and queued task
     execution already have typed workflow facades, while the underlying
     conversation/runtime owners still correctly own turn locking, plan
     validation, task state transitions, permissions, event publication, and
     cancellation. Plan comments have only one production inbound consumer
     (the bound TUI conversation) and require the current plan plus the
     conversation lock, so no standalone storage-writing application DTO is
     introduced without a second consumer or a stable cross-interface
     contract. No production code or storage schema changes are introduced.
116. Stage 5BG adds the typed `ImportSessionRequest` and
     `SessionApplicationService.import_session()` use case. The CLI's Rust
     session parser and import-report rendering remain adapter-owned; the
     application seam validates the canonical `SessionSnapshot` and delegates
     the existing atomic `SessionStore.import_session()` write. Import
     statistics, JSON/text output, SQLite schema, Runtime, Provider, Finalizer,
     ACP, TUI, and session behavior remain unchanged.
117. Stage 5BH audits remaining inbound and persistence-facing `SessionStore`
     consumers. CLI and TUI have no direct storage business calls remaining;
     ACP alias operations remain ACP-protocol ownership, bootstrap sandbox
     preflight and store initialization remain composition-root ownership, and
     Runtime/Conversation writes remain lifecycle ownership. No second inbound
     consumer or stable cross-interface contract was found for background wake
     state, plan-comment writes, task creation, or session creation, so no
     mechanical DTO or storage facade is introduced.
118. Stage 5BI audits the 59 explicitly quarantined compatibility facades
     against repository consumers. Production imports have already converged
     on canonical owners; the remaining in-repository legacy imports are
     compatibility tests, live fixtures, or public identity checks. No
     external-package inventory or versioned deprecation window can be proven
     from this checkout, so facades remain identity-preserving and one-way.
     Deletion requires an explicit release decision, downstream-caller evidence,
     and a migration test window; this stage deletes no facade and changes no
     runtime behavior.
119. Stage 5BJ audits the public-contract evidence for the quarantined facades.
     The package is still published as pre-alpha (`0.1.0.dev0`) with the
     `neuro` and `neuro-code` entry points owned by bootstrap; the repository
     does not contain a versioned deprecation schedule, downstream package
     inventory, or a release note that authorizes removing old import paths.
     The compatibility matrix and this ADR describe the old paths as
     transitional/compatibility boundaries, while the import-contract tests
     already enforce one-way identity-preserving facades and canonical
     production imports. Therefore this stage freezes the current compatibility
     contract and defines deletion gates, but adds no warning, removal, alias
     rewrite, or runtime behavior change. A future removal stage must name the
     release boundary, prove downstream migration, update both documentation
     trees, and retain an import/identity migration test window.
120. Stage 5BM adds a session-scoped tool-output artifact application query.
     `SessionToolOutputArtifactApplicationService` verifies the session, derives
     only bounded artifact handles from that session's persisted tool terminal
     events, and delegates content reads through the existing reader port.
     Malformed metadata is ignored, unassociated handles are rejected without
     revealing cross-session or filesystem existence, and no raw output,
     arguments, absolute paths, SQLite table, Runtime, or event-kind change is
     introduced. CLI, TUI, and ACP exposure remain a later inbound slice.
121. Stage 5BN injects the session-scoped artifact query into the TUI without
     exposing filesystem infrastructure. A tool card retains only the bounded
     opaque artifact ID; expanding the card asynchronously reads the current
     session's associated artifact through the application service and existing
     reader limits. Missing or cross-session artifacts render a generic
     localized notice. No new event kind, SQLite schema, Runtime, Provider,
     permission, or session behavior is introduced.
122. Stage 5BO adds a read-only CLI `sessions artifacts SESSION_ID [ARTIFACT_ID]`
     query. Listing and reading go through the session-scoped artifact
     application service; the CLI never reads the state directory directly.
     Output omits filesystem paths and raw metadata, and bounded reads expose
     only redacted content. No SQLite schema, event, Runtime, Provider, TUI, or
     ACP behavior changes.
123. Stage 5BP adds an explicit `sessions artifacts --prune` operation for
     bounded artifact lifecycle maintenance. The application service scans all
     persisted session terminal-event metadata before calling a garbage-
     collector port; the file adapter deletes only canonical, unreferenced
     files older than a one-hour grace period and preserves malformed names,
     symlinks, non-regular files, referenced files, and recent files. The scan
     and filesystem unlink are deliberately best-effort rather than one
     SQLite/filesystem transaction. Session deletion, fork, import, export,
     startup, Runtime, Provider, Finalizer, TUI, ACP, schema, and event
     behavior remain unchanged.
124. Stage 5BQ adds the private namespaced ACP extension
     `_neuro-code/session/artifacts`. It resolves external ACP IDs through
     the existing alias namespace and delegates bounded list/read operations
     to the session-scoped artifact application service. Responses contain
     only opaque IDs, bounded redacted content, byte/event facts, and
     truncation flags; no paths, raw metadata, arguments, or secrets are
     exposed. The extension is not advertised as a standard ACP capability,
     and no schema, event, Runtime, Provider, Finalizer, permission, TUI, or
     SQLite behavior changes.
125. Stage 5BS reuses the existing typed `GetSessionTaskRequest` application
     seam for the Runtime's queued-plan lookup. `AgentLoopRunner` now asks
     `SessionApplicationService.get_session_task()` for the queued projection
     before the existing task-start transition; task creation, start, finish,
     event ordering, cancellation, and persistence ownership remain in the
     Runtime/session lifecycle owner. The service's runtime-only type imports
     are guarded for type checking to avoid introducing a module cycle. No
     storage schema, Provider, Finalizer, ACP, CLI, TUI, or task-state behavior
     changes.
126. Stage 5BT reuses the existing typed `StartSessionRequest` application
     seam when `AgentLoopRunner` creates a session for a turn without a
     caller-provided session ID. The service returns the canonical
     `SessionSummary`, whose ID seeds the unchanged event sequence and turn
     recorder path. Session creation remains an atomic `SessionStore` adapter
     operation; the service does not claim atomicity with a turn event,
     session items, or an execution record. Existing task lifecycle, event
     ordering, cancellation, Provider, Finalizer, ACP, CLI, TUI, and schema
     behavior remain unchanged.
127. Stage 5BU makes the session-scoped tool-output artifact application
     service reuse the existing typed session-summary and keyset-page seams.
     Artifact association still reads only the persisted terminal-event
     projection, and pruning still scans every session before invoking the
     filesystem garbage-collector port. This removes duplicate session
     existence/pagination forwarding without exposing raw events, paths, or
     metadata to inbound adapters; no artifact, transaction, Runtime, or
     schema behavior changes.
128. Stage 5BV adds typed session-alias requests to
     `SessionApplicationService` and makes `AcpApplicationService` delegate
     alias binding, lookup, and allocation through that seam. ACP continues to
     own external-ID validation and wire behavior, while storage retains alias
     uniqueness and durable conflict semantics. No alias schema, Runtime,
     Provider, Finalizer, event, or user-visible behavior changes.
129. Stage 5BW audits the remaining direct `SessionStore` consumers after the
     alias seam. Runtime event/item/finalization writes remain owned by the
     runtime recorder transaction; conversation plan-comment, task, and wake
     state writes remain owned by the locked session controller; and the
     artifact service keeps raw terminal-event reads because it needs an
     untrusted metadata projection. No second production consumer or stable
     cross-interface contract justifies another DTO, so no production seam is
     added in this audit.
130. Stage 5BX moves the CLI's pure output projections into the canonical
     `neuro_code.interfaces.cli.serialization` module. Execution outcomes,
     execution records, bounded tool-output artifact handles, session search
     pages, and Markdown session rendering keep their existing wire shapes and
     redaction boundaries; the CLI remains responsible for command dispatch
     and side-effect orchestration. The new interface module has no storage,
     provider, Runtime, or infrastructure dependency and does not change CLI
     behavior.
131. Stage 5BY moves ACP's typed execution-outcome projection into the
     canonical `neuro_code.interfaces.acp.serialization` module. Mapping to
     legal stop reasons and bounded execution metadata remains protocol-only;
     ACP session lifecycle, MCP conversion, tool execution, and error handling
     remain in the adapter/application owners. The projection preserves the
     typed-outcome-first behavior and does not expose snapshots, digests, tool
     arguments, or provider internals.
132. Stage 5BZ moves ACP's bounded text and payload-size primitives into the
     canonical `neuro_code.interfaces.acp.serialization` module. Control
     sanitization, UTF-8-safe truncation, explicit-value redaction, and
     canonical JSON byte sizing remain protocol safety operations; ACP session
     lifecycle, MCP transport, tool execution, and error handling remain in
     their existing owners. The extraction preserves all limits and wire
     behavior without introducing a second serializer or exposing raw output.
133. Stage 5CA moves TUI terminal-outcome metadata parsing into the canonical
     `neuro_code.interfaces.tui.execution` module. The projection accepts only
     the existing recoverable `STUCK` and `BUDGET_LIMITED` statuses and fails
     closed for unknown, non-recoverable, or non-terminal values. TUI layout,
     localization, event ordering, session behavior, and runtime decisions
     remain unchanged; the module does not access persistence or Textual.
134. Stage 5CB adds a typed `LoadSessionEventsRequest` read seam to
     `SessionApplicationService`. Session export and the session-scoped tool
     artifact application service now consume copied, immutable event-row
     projections through that seam. The event rows remain an untrusted storage
     projection rather than a second domain event model; event decoding,
     lifecycle writes, SQLite transactions, Runtime, Provider, Finalizer, CLI,
     TUI, and ACP wire behavior remain owned by their existing boundaries.
135. Stage 5CC establishes `neuro_code.application.sessions.catalog` as the
     canonical owner of read-only session catalog and inspection queries.
     `SessionCatalogApplicationService` owns bounded list/search/page/workspace
     projections and the safe execution-record inspection projection. The
     existing `SessionApplicationService` keeps a compatibility application
     facade that delegates these reads, while the CLI consumes the catalog
     service directly. Session lifecycle writes, aliases, conversation items,
     plans, tasks, event decoding, Runtime, Provider, Finalizer, ACP wire
     behavior, and storage transactions remain unchanged.
136. Stage 5CD establishes `neuro_code.application.sessions.turns` as the
     canonical owner of the typed single-turn application boundary.
     `RunTurnRequest`, `SessionTurnRunner`, and `SessionTurnService` move out
     of the broad session lifecycle module while preserving identity through
     one-way compatibility re-exports from `neuro_code.application.sessions`
     and `.service`. CLI, TUI, ACP, and application consumers import the
     canonical turns module directly. The runner still owns locking, persisted
     context, event delivery, cancellation, and Runtime behavior; no session
     schema, Provider, Finalizer, workflow, or wire behavior changes.
137. Stage 5CE establishes `neuro_code.application.providers.contracts` as the
     canonical owner of the shared `ProviderOption` and
     `ProviderSelectionResult` projections. The profile conversation controller
     remains responsible for binding replacement and session selection, while
     the Provider application service, bootstrap entrypoint, and TUI consume the
     provider projections from their own application seam. The profile module,
     package exports, and legacy runtime facade retain identity-preserving
     compatibility re-exports; provider lifecycle, selection behavior, session
     replay, and wire behavior remain unchanged.
138. Stage 5CF establishes `neuro_code.application.sessions.binding` as the
     canonical owner of the typed session-binding contract. `ConversationBinding`
     and `ConversationRunner` are shared by ACP, bootstrap, session application,
     and runtime-facing consumers, while `ProfileConversationController` retains
     profile-specific session selection and binding replacement. The historical
     profile and runtime paths keep identity-preserving compatibility re-exports;
     turn locking, event delivery, cancellation, persistence, Provider,
     Finalizer, and wire behavior remain unchanged.
139. Stage 5CG establishes `neuro_code.application.sessions.contracts` as the
     canonical owner of the immutable session-selection and interaction-policy
     projections: `SessionOption`, `SessionSelectionResult`,
     `ReasoningEffortSelectionResult`, and `InteractionModeSelectionResult`.
     The TUI consumes these projections directly, while
     `ProfileConversationController` retains selection, policy application,
     locking, and binding replacement. Profile and legacy runtime imports keep
     identity-preserving compatibility re-exports; session resume, provider
     selection, interaction modes, reasoning effort, and wire behavior remain
     unchanged.
140. Stage 5CH establishes `neuro_code.application.sessions.selection` as the
     canonical inbound seam for interactive session listing, selection, and
     rename. `SessionSelectionService` is a non-owning facade over the existing
     `ProfileConversationController`; locking, workspace checks, binding
     replacement, resume lifecycle, and execution-record projection remain with
     the controller. The TUI uses the facade for selection operations while
     retaining the legacy controller injection for compatibility with the
     current execution-record projection. No session schema, Runtime, Provider,
     Finalizer, ACP wire behavior, or TUI layout changes.

141. Stage 5CI establishes `neuro_code.application.sessions.lifecycle` as the
     canonical owner of the typed durable session lifecycle commands shared by
     the Runtime, CLI, and ACP application boundaries. `StartSessionRequest`,
     `ImportSessionRequest`, `RenameSessionRequest`, `ForkSessionRequest`, and
     `DeleteSessionRequest` plus `SessionLifecycleService` move out of the
     broad session service while that service keeps identity-preserving
     compatibility re-exports and delegates the commands. ACP retains
     workspace visibility and active-session cleanup around the service; CLI
     retains parsing, rendering, and file I/O. The lifecycle service performs
     no turn locking, binding replacement, Runtime execution, Provider or
     Finalizer work, and no session schema, transaction, or wire behavior
     changes.
142. Stage 5CJ establishes `neuro_code.application.sessions.task_queries` as
     the canonical owner of the typed, read-only session-task queries shared by
     the Runtime and the multi-turn conversation controller. `ListSessionTasksRequest`,
     `GetSessionTaskRequest`, `SessionTaskQueryController`, and
     `SessionTaskQueryService` move out of the broad session service while it
     retains identity-preserving compatibility exports and delegates the reads.
     Task creation, queueing, start/finish transitions, permissions, execution,
     locking, cancellation, SessionStore/SQLite writes, Runtime, Provider,
     Finalizer, and wire behavior remain with their existing owners.
143. Stage 5CK establishes `neuro_code.application.sessions.summary` as the
     canonical owner of the typed, read-only session-summary query shared by
     session resume, bootstrap configuration, ACP workspace validation, and
     session-scoped tool-output artifact reads. `GetSessionSummaryRequest`,
     `SessionSummaryQueryController`, and `SessionSummaryQueryService` move out
     of the broad session service, which retains identity-preserving
     compatibility exports and delegates the query. No session lifecycle write,
     event/item read, schema, transaction, Runtime, Provider, Finalizer, or wire
     behavior changes.
144. Stage 5CL establishes `neuro_code.application.sessions.execution_queries`
     as the canonical owner of typed, read-only execution-record projections.
     `LoadExecutionRecordRequest`, `LoadExecutionRecordsRequest`,
     `SessionExecutionQueryController`, and `SessionExecutionQueryService` are
     shared by the session catalog and conversation resume/reload paths; the
     broad session service retains identity-preserving compatibility exports.
     Single and bulk reads remain delegated to the existing `SessionStore` port.
     No execution-record write, schema, transaction, Runtime, Provider,
     Finalizer, TUI, ACP, or wire behavior changes.
145. Stage 5CM establishes `neuro_code.application.sessions.event_queries`
     as the canonical owner of the copied, read-only session-event projection.
     `LoadSessionEventsRequest`, `SessionEventQueryController`, and
     `SessionEventQueryService` are shared by session export and the
     session-scoped tool-output artifact application service; the broad session
     service retains identity-preserving compatibility exports. Event rows
     remain untrusted mappings and are not decoded into a second domain-event
     model. Event writes, lifecycle transactions, Runtime, Provider, Finalizer,
     TUI, ACP, and wire behavior remain unchanged.
146. Stage 5CN establishes `neuro_code.application.sessions.item_queries` as
     the canonical owner of the ordered, read-only session-item projection.
     `LoadSessionItemsRequest`, `SessionItemQueryController`, and
     `SessionItemQueryService` are shared by conversation resume/reload and
     explicit session export; the broad session service retains
     identity-preserving compatibility exports. Item writes, plans, comments,
     events, lifecycle transactions, Runtime, Provider, Finalizer, TUI, ACP,
     and wire behavior remain unchanged. Plan and comment reads remain with
     the conversation owner because no second production consumer currently
     exists.
147. Stage 5CO establishes explicit canonical-submodule imports for existing
     application consumers. Bootstrap composition and the TUI now import
     provider and workflow services from `application.providers.service` and
     the three `application.workflows.*` owners; CLI/bootstrap/ACP/session
     serializers import session lifecycle, service, and catalog projections
     from their concrete owners. Aggregate package exports remain
     identity-preserving compatibility paths for external and legacy callers.
     This is import-boundary convergence only: no request type, service
     ownership, locking, persistence, Runtime, Provider, Finalizer, TUI
     layout, ACP wire, or workflow behavior changes. Plan/comment/export
     reads were audited and were not split again because no second production
     owner or stable cross-interface contract exists.
148. Stage 5CP establishes direct imports of the bounded tool-output artifact
     application owner for every production consumer. CLI, TUI, ACP,
     bootstrap composition/entrypoints, ACP application orchestration, and CLI
     serialization now import `neuro_code.application.tools.service` directly;
     the package aggregate remains an identity-preserving compatibility export.
     Artifact handles, session visibility checks, redaction, byte limits,
     pruning, permissions, storage, Runtime, and protocol behavior remain
     unchanged. This stage does not expose filesystem paths or create a second
     artifact model.

## Consequences

- The intended dependency direction is executable before directory migration
  starts.
- Existing debt remains visible and can be reduced one direct import at a time.
- Compatibility modules preserve import identity during moves, at the cost of
  temporary extra modules and tests.
- Bootstrap may contain configuration loaders and factories, but cannot become
  the owner of shared configuration contracts.
- The compatibility re-export removal date is intentionally not decided here;
  removal requires a later ADR or equivalent versioned compatibility decision.

## Rejected alternatives

- Moving every package into the target tree at once: this obscures behavioral
  regressions and is difficult to roll back safely.
- Silently tolerating all imports between existing top-level packages: this
  would allow architecture debt to grow before migration begins.
- Placing all configuration types in bootstrap: this reverses the intended
  dependency direction for application and infrastructure consumers.
