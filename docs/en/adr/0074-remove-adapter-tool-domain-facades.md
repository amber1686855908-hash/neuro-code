# ADR 0074: Remove Adapter, Tool, and Flat Domain Facades

- Status: Accepted
- Date: 2026-08-07
- Supersedes: the adapter/tool/domain-facade retention decisions in ADR 0049,
  ADR 0072, and ADR 0073

## Context

Architecture Freeze v1 established canonical owners under
`neuro_code.infrastructure`, `neuro_code.application`, and nested domain
packages. A repository-wide consumer audit confirmed that the remaining
`neuro_code.adapters.*`, `neuro_code.tools.*`, and flat modules directly under
`neuro_code.domain` were identity-preserving re-exports without state,
composition, or side effects. Production code had already migrated to the
canonical owners.

Keeping these paths would preserve a second import surface and allow new code
to drift back to retired boundaries. This decision is an intentional breaking
cleanup; it does not change runtime, provider, tool, permission, persistence,
or protocol behavior.

## Decision

Remove the complete `neuro_code.adapters` and `neuro_code.tools` facade
families, and remove flat facade modules directly under `neuro_code.domain`,
including the former conversation, model-event, provider-settings,
instruction, skill, sandbox, terminal, and background-task aggregates.

Canonical consumers use these owners instead:

- tools: `neuro_code.infrastructure.tools.*`;
- adapters: `neuro_code.infrastructure.*` or the corresponding
  `neuro_code.application.ports.*` contract;
- domain values: nested canonical packages such as
  `neuro_code.domain.conversation.*`, `neuro_code.domain.execution.*`,
  `neuro_code.domain.workspace.*`, `neuro_code.domain.sandbox.models`,
  `neuro_code.domain.terminal.models`, and
  `neuro_code.domain.background_tasks.models`.

The `neuro_code.domain` package initializer remains an intentional aggregate
for its canonical public values. Nested canonical package initializers also
remain; they are implementations, not compatibility facades.

## Compatibility boundary

Importing a removed path must fail with `ModuleNotFoundError` (or, for an
absent parent package, the equivalent missing-parent error). Architecture
import-contract tests assert the absence of facade files, the absence of
production legacy imports, and the continued availability of canonical nested
packages. External callers must migrate to canonical paths.

## Consequences

- There is one implementation owner for tools, infrastructure adapters, and
  flat-domain value objects.
- No runtime behavior, event ordering, persistence schema, security boundary,
  or provider request behavior changes.
- This is a deliberate import-compatibility break and is not a temporary
  allowlist.
- Future architecture work must add a new boundary only when a user capability
  requires it, not to recreate these facades.

## Validation

The cleanup is accepted with passing architecture import contracts, affected
provider/tool/domain/session tests, Ruff, formatting, mypy, documentation
parity, and `git diff --check`.
