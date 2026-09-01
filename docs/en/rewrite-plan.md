# Rewrite execution plan

[简体中文](../zh-CN/rewrite-plan.md) · **English**

## Working method

The unit of delivery is a testable user capability, not an upstream crate. Each
slice starts with source evidence and fixtures, adds domain contracts, then
application behavior, adapters, interfaces, and differential tests. Generated
Understand Anything summaries accelerate discovery but never replace reading
the source or tests.

## Milestones

### M0 — baseline and governance

- Pin source and analysis commits.
- Record business boundary, architecture, compatibility matrix, provenance,
  engineering rules, and ADR process.
- Establish golden-fixture and differential-test conventions.

Exit: every M1/M2 capability has an upstream evidence path and an owner status.

### M1 — installable engineering skeleton

- One project-owned namespace across product, distribution, command, Python
  package, environment variables, state directories, and errors.
- Cross-platform package, CLI, configuration, errors, domain events, and ports.
- Equivalent globally installed `neuro`/`neuro-code` entry points, the
  `neuro code` TUI alias, and a normal dependency set that includes the TUI.
- `version`, `inspect`, and machine-readable output.
- Ruff, mypy, pytest/unittest bootstrap and CI-ready commands.

Exit: the project installs and the same contract suite passes on all targets.

### M2 — headless coding-agent vertical slice

- Named provider profiles, explicit wire protocols, optional read-only CC
  Switch configuration/gateway compatibility, no implicit cloud default, and
  ordered lazy fallback profiles that switch only before the first model event.
  Per-profile HTTP transport policy supports inherited environment, direct, or
  secret-safe explicit proxy routing across every provider adapter.
- OpenAI Chat, generic Responses with an optional xAI dialect, Anthropic, and
  Gemini streaming providers behind one normalized multi-provider port, including validated
  provider-native image projection with explicit fallback for unsupported roles
  and references, bounded output tokens, persisted reasoning continuity for
  thinking-mode tool-call turns, and strict-affinity native xAI encrypted
  context replay.
- Agent/tool loop, structured tool calls, cancellation, step limit, bounded
  output, audited provider attempt/selection events, a per-run failover bypass,
  a double-gated DeepSeek live regression path, and owned process-tree cleanup.
- Opt-in xAI-hosted web, X, and code-interpreter tools with collision-safe wire
  projection, provider-owned lifecycle events, and durable native output replay.
- Read/list/grep/search-replace/bash tools, an owned background-command
  snapshot/event-driven single-or-multi-wait/kill lifecycle, and a headless
  permission policy that evaluates
  every safely decomposable shell-command segment.
- SQLite sessions, ordered message/provider-context items, prefix-safe resume,
  stable titles, ranked title/visible-content search, JSON/Markdown export,
  read-only Rust JSONL session import, and fail-closed provider-affine context
  projection. Terminal provider output is canonical for
  persisted assistant text and native items while streaming deltas remain the UI
  surface.

Exit: a fake-provider end-to-end test performs read, edit, command, and final
response; an opt-in live test can do the same against a configured provider.

### M3 — core TUI and platform safety

- Textual UI over the same event stream: prompt, stable in-place streamed
  conversation blocks, persistent English/Simplified-Chinese interface choice,
  first-run and Settings-based multi-provider management, approval, safe
  profile/session selection, workspace-scoped session search,
  session-scoped task metadata/notices, richer tool cards, model picker, and
  essential slash commands.
- Owned process trees, conversation-scoped managed background shell commands,
  bounded any/all multi-task waits, and completion metadata at explicit model
  boundaries; interactive PTY
  creation/input/resize, signals, clipboard, and filesystem notifications.
- `off`, `workspace`, `read-only`, and `strict` sandbox profiles.
- Session-fixed built-in sandbox persistence, startup resume conflict checks,
  and an in-process TUI restart boundary for different-profile sessions.

Exit: interactive smoke suites pass on Linux, macOS, and Windows without leaked
terminal state or child processes.

### M4 — protocols and extensibility

- Partial ACP v1 stdio/WebSocket core: official Python SDK framing/router and
  the bounded WebSocket newline-JSON bridge,
  initialize/new/list/load/delete/fork/resume/prompt/cancel/close, durable
  external-to-internal session aliases, transactional durable fork/delete,
  replaying load versus silent resume, workspace-scoped bounded cursor discovery,
  bounded/redacted visible-history replay, bounded Text/inline-Image/inline-Audio/
  ResourceLink/embedded-TextResource/BlobResource input,
  fail-closed permission requests, standard event updates, per-session
  concurrency, disconnect cleanup, bounded additional directories, and
  bounded session-owned stdio/Streamable HTTP/legacy SSE MCP tool servers,
  plus capability-gated client text-file reads and exact replacements through
  `fs/read_text_file` and `fs/write_text_file`, direct foreground
  `terminal_exec` plus bounded standard client background terminal lifecycle
  (`terminal_start`/`terminal_output`/`terminal_wait`/`terminal_kill`), durable
  native image context with safe ACP-history placeholders, and labeled bounded
  embedded text resources, are implemented.
- The bounded private MCP session extension also implements
  resource/resource-template discovery, resource reads, prompt
  discovery/retrieval, sampling/elicitation callbacks, and dynamic tool-list
  refresh; these are bounded projections rather than generic MCP parity.
- Complete ACP conformance remains open: ACP-transport MCP server declarations,
  MCP features outside the bounded private projections, interactive client
  terminal input/resize/PTY framing, binary multimedia-history replay,
  provider-native audio/video semantics, and any advertised `x.ai/*`
  extensions.
- Bounded exact-name `AGENTS.md` inheritance and read-only LOCAL/REPO/USER
  skill discovery/body loading are implemented, including dynamic session
  targets, content-change checks, and bounded variable substitution.
  Remote/server/bundled skills, agent profiles, hooks, and executable plugins
  remain open alongside MCP features outside the bounded private projections.
- A bounded provider-neutral plan mode is implemented: model-managed
  `update_plan` replacements persist per session, restore on resume, inherit on
  fork, and are shown through TUI `/plan DESCRIPTION` and `/view-plan`. A
  user-confirmed `/execute-plan`/`/run-plan` handoff persists an execution
  event, creates one bounded durable session-task record, and moves only to
  `accept-edits`. The task has opaque identity and explicit terminal states but
  neither schedules nor wakes work. The local `/comment-plan STEP COMMENT`
  command adds bounded feedback to the current plan revision and shows it with
  `/view-plan`. Each execution task also preserves the handed-off immutable
  plan revision: `/tasks` shows a compact audit summary, while explicit
  current-session `/view-task TASK_ID` renders the stored snapshot only as
  read-only reference. Task scheduling and subagents remain open.

Exit: standard ACP clients pass the complete conformance scenarios and
extension failures do not corrupt the primary session. The implementation
remains partial; the original stdio-only slice did not satisfy this exit
condition, and complete conformance remains open.

### M5 — advanced parity

- Memory/compaction, LSP, worktrees/checkpoints, web/media/Mermaid/voice,
  telemetry/update, leader/relay, and cloud-service adapters.
- Complete CLI/config/session/TUI/safety audit and release provenance review.

Exit: every public upstream capability is compatible or has an approved,
documented intentional difference.

## Change discipline

Every capability change includes tests, compatibility status, source evidence,
and documentation. Source updates are reviewed as behavioral deltas against the
pinned commit; files are never mechanically synchronized. Calendar estimates
are derived only after staffing is known, while milestone exit gates remain
fixed.
