# ADR 0152: CLI Session Command Boundary

- Status: Accepted
- Date: 2026-08-31
- Scope: the bounded CLI session-command execution slice stacked on PR #80
- Depends on: PR #80 and the existing CLI/bootstrap and session application boundaries

## Context

The exact frozen PR #80 head is
`11a6c610fe7f9e949d5a5c2f3aab2adb2358385f`, based on
`codex/acp-transport-boundary` at
`d5c95fc5d0a621b58827c5aa9b1e9f43dff70e06`. At that head,
`neuro_code.cli` still combined parser construction, top-level dispatch, the
full CLI service protocol, and the complete execution body for the `sessions`
command. The next consolidation slice must be small enough to audit without
changing the CLI grammar, application session services, or bootstrap
composition.

This ADR extracts only the execution boundary for an already parsed
`sessions` command. It does not make `neuro_code.cli` a complete compatibility
facade and does not change the public command surface.

## Pre-change audit

The audit was completed against the exact PR #80 head before moving code.

### Boundary symbols

The session-specific execution symbols in `neuro_code.cli` were:

- `_sessions_command`, the single async implementation for the parsed command;
- `SessionCatalogApplicationService`, `ListSessionsRequest`, and
  `SearchSessionsRequest` for list/search;
- `SessionLifecycleService` and `RenameSessionRequest` for rename;
- `TurnRecoveryService` for inspect/abandon/retry;
- `SessionToolOutputArtifactApplicationService` and its list/read requests for
  artifact operations; and
- the session execution/artifact/search serializers imported from
  `neuro_code.interfaces.cli.serialization`.

`MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES` is used by the session execution body and
also by parser construction, so it is not moved out of `neuro_code.cli`.
Parser-only symbols, `_export_session`, `_import_session`, and all other CLI
command bodies remain in their existing module.

### State and lifecycle

The sessions command owns no long-lived mutable state. Each invocation loads a
configuration and opens one session store. List/search/rename and artifact
operations use the existing application services over that store. Compact and
recovery retry open an application for the selected session, configure the
resume, create one binding, invoke the existing runner operation, and always
close the application under `asyncio.shield` in the existing `finally` path.
There is no new task registry, retry state, cancellation owner, provider
authority, workspace authority, or persistence implementation in this slice.

### Call sites and service contract

The only production call site was the `run()` sessions dispatch in
`neuro_code.cli`. The canonical bootstrap launcher continues to construct
`BootstrapCliServices`, while tests and callers may inject compatible services.
The audited execution body needs only:

- `load_config`;
- `create_session_store`;
- `create_tool_output_artifact_service`; and
- `open_application`.

The full `CliServices` protocol contains unrelated agent, TUI, provider, ACP,
import, and export capabilities, so it is not used as the canonical command
boundary contract.

### Dependency direction and frozen behavior

The intended direction is:

```text
neuro_code.cli parser/top-level dispatch
        -> neuro_code.interfaces.cli.sessions
        -> application session/artifact services and ports
        -> injected application/binding/runner seams
```

The canonical module may reuse `interfaces.cli.serialization`, but it must not
import `neuro_code.cli` or acquire bootstrap, provider, workspace, sandbox, or
permission authority. The CLI parser still owns the artifact bound because the
same bound is part of parser defaults.

Existing `tests/test_cli.py` coverage freezes public sessions list/search/rename
and artifact list/read/prune behavior, including JSON/plain projections and
validation. The extracted slice adds direct canonical execution, public
dispatch equivalence, error mapping, JSON/plain equivalence, compact/recovery
retry cleanup, identity alias, and import-direction coverage.

## Decision

`neuro_code.interfaces.cli.sessions` is the canonical owner of
`run_sessions_command(args, services)`. It owns validation, application-service
selection, execution, and presentation for the already parsed:

- list;
- search;
- rename;
- compact;
- artifacts list/read/prune; and
- recovery inspect/abandon/retry operations.

It declares the narrow `SessionCliServices`, `SessionCliApplication`,
`SessionCliBinding`, and `SessionCliRunner` protocols for the exact capabilities
used by this boundary. These protocols describe existing application seams;
they do not introduce a second service implementation or alter ownership.

`neuro_code.cli` retains `build_parser`, all sessions parser grammar, top-level
`run` dispatch, and the other command implementations. Its private
`_sessions_command` name is an identity-preserving import alias to
`run_sessions_command`, so private compatibility imports continue to resolve
to the one implementation.

The canonical command continues to use
`neuro_code.interfaces.cli.serialization` for bounded projections. No parser
or public CLI API redesign is part of this decision.

## Behavior and compatibility

The extraction preserves existing command arguments, default values, bounds,
validation messages, exception types, exit-code mapping, JSON and plain output,
artifact byte limits, redaction, session visibility, storage delegation,
recovery semantics, and application cleanup. In particular, compact and retry
retain the existing `config_for_session_resume`, binding creation, runner
invocation, and shielded close order. A canonical direct call and the existing
top-level dispatch are required to produce the same observable result.

No new capability gate is introduced. No session, provider, workspace,
sandbox, permission, background-task, or persistence authority moves into the
interface module.

## Explicit non-goals

This ADR does not extract the parser, `build_parser`, top-level `run`, agent,
provider, ACP, TUI, export/import, subagent, provider-selection, or bootstrap
facades. It does not redesign session APIs, serializers, session storage,
recovery/compaction services, cancellation, retries, background tasks,
permissions, workspace/sandbox policy, orchestration, or UI behavior.

## Status and validation

This ADR is Accepted for this bounded execution-boundary slice. Acceptance is
limited to the canonical module, its identity-preserving compatibility alias,
the existing session behavior, and the accompanying architecture/docs tests;
it does not claim that the remaining `neuro_code.cli` responsibilities have
been consolidated.

Final local evidence for the slice records 163 focused CLI/architecture tests
passed (with 2 subtests), and 2602 full tests passed with 50 skipped and 17
deselected. Full coverage is 85.29%. `uv lock --check`, documentation parity
(163 English/Chinese pairs), Ruff lint, Ruff format, mypy, `uv build`, and
`git diff --check` all pass. The skipped cases are platform- or privilege-
specific tests already gated by the repository; this evidence does not claim
native Windows/macOS acceptance on Linux.
