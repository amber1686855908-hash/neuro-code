# ADR 0153: Architecture Completion Before v1

- Status: Accepted
- Date: 2026-09-02
- Scope: the final pre-v1 modular-monolith and ports-and-adapters consolidation
- Depends on: ADR 0049 and the interface/session boundary ADRs through ADR 0152

## Context

The repository had already established `application`, `application/ports`,
`domain`, `infrastructure`, `interfaces`, `bootstrap`, and `shared`, but several
large canonical implementations still lived at the package root. The root
modules made ownership ambiguous: the interface packages were partly wrappers,
configuration mixed value contracts with file loading, and bootstrap combined
process launch, concrete service selection, factory policy, and resource graph
assembly.

This was an internal architecture migration before the first release. The
objective was therefore to make ownership visible in the source tree while
preserving the existing CLI, TUI, ACP, Runtime, Provider, permission, sandbox,
session, persistence, and security behavior.

## Decision

The package root is closed to production implementation modules. Its source
tree contains only `__init__.py`, `__main__.py`, and the architectural packages:
`application`, `bootstrap`, `domain`, `infrastructure`, `interfaces`, and
`shared`.

The inbound adapters have these canonical owners:

- `interfaces.cli.app` owns CLI parsing, dispatch, presentation, and exit-code
  handling; `interfaces.cli.sessions` owns the parsed sessions command boundary.
- `interfaces.tui.app` owns only the Textual lifecycle, high-level wiring, and
  app-owned state. `interfaces.tui.contracts`, `interaction`, and `state` own
  TUI contracts and local models; `widgets` and `screens` own reusable visual
  surfaces; `controllers` owns cohesive turns, commands, preferences,
  provider/session selection, plans/tasks, background, transcript, runtime, and
  tool-activity orchestration. The existing `commands`, `text`, `theme`, and
  `tool_activity` modules remain their canonical owners.
- `interfaces.acp.agent` owns the public ACP protocol facade and high-level
  wiring. `interfaces.acp.negotiation`, `session_registry`,
  `session_lifecycle`, `mcp`, `extensions`, and `prompt` own connection
  negotiation, published session state, session lifecycle, live MCP handling,
  private extension dispatch, and prompt/permission execution respectively;
  the existing `content`, `updates`, `client_io`, `mcp_config`, `transport`,
  and `session` modules remain the canonical owners of their boundaries.

The former root implementations `neuro_code.cli`, `neuro_code.tui`,
`neuro_code.acp`, `neuro_code.tui_commands`, `neuro_code.tui_text`, and
`neuro_code.tui_theme` are removed. No root compatibility wrapper remains
authoritative.

Configuration has one explicit split:

- `application.ports.configuration` owns immutable `AppConfig` and
  `ProviderProfile` values, validation, and explicit-input configuration
  policy. It does not read the process environment, probe optional packages,
  or resolve filesystem paths.
- `bootstrap.configuration` owns TOML, environment, CC Switch, legacy-format,
  managed-overlay, path resolution, and stored-credential loading.
- `infrastructure.providers.binding` owns concrete environment-credential and
  optional HTTP-capability resolution immediately before provider construction.
- `infrastructure.providers.managed_provider_settings` owns the concrete
  managed JSON reader.
- `application.ports.provider_dialects` owns dialect inference used by the
  application-facing contract.

Bootstrap has one explicit composition split:

- `bootstrap.entrypoints` is a lazy, thin process launcher.
- `bootstrap.cli` selects concrete CLI/TUI services.
- `bootstrap.acp` contains ACP workspace and MCP composition adapters.
- `bootstrap.factories` owns default concrete factory selection.
- `bootstrap.composition` remains the single owner of the shared resource
  graph, lifecycle ordering, and failure cleanup.

Architecture tests enforce the source-tree boundary and scan production imports
for the forbidden directions `interfaces -> infrastructure/bootstrap`,
`application -> infrastructure/interfaces/bootstrap`,
`domain -> application/infrastructure/interfaces/bootstrap`, and
`infrastructure -> interfaces/bootstrap`. The existing narrow entrypoint edge
and any intentional compatibility exports are tested explicitly rather than
being hidden by a broad allowlist.

## Consequences

The directory structure now communicates the intended modular-monolith
architecture without requiring `architecture.md` to explain which root module
is authoritative. Interface imports do not assemble concrete infrastructure,
and application ports do not load bootstrap configuration or providers.

The refactor is behavior-preserving: command grammar, output and exit codes,
TUI presentation and shortcuts, ACP wire semantics, Runtime and Provider
behavior, permission and sandbox gates, and session persistence semantics are
unchanged.

The TUI decomposition deliberately keeps `interfaces.tui.app` as the lifecycle
and wiring owner; its controller mixins are split by reason to change and do
not import the app module. `infrastructure.persistence.sqlite_session` remains
the one SQLite session-store/schema/transaction owner. Neither boundary is
duplicated or weakened.
