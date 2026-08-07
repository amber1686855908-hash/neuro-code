# ADR 0073: Remove Obsolete Root Compatibility Facades

- Status: Accepted
- Date: 2026-08-07
- Supersedes: the root-facade retention decision for these five modules in ADR 0049

## Context

The canonical owners for configuration, permission policy, Bash command
analysis, workspace identity, and workspace-change observation have been stable
for the completed architecture migration. The following root modules contained
only identity-preserving re-exports and had no production consumers:

- `neuro_code.bash_commands`
- `neuro_code.config`
- `neuro_code.permissions`
- `neuro_code.workspace`
- `neuro_code.workspace_changes`

Keeping these modules after all internal consumers had migrated would preserve
compatibility debt without adding an active application boundary. This ADR is a
versioned breaking-cleanup decision for these five paths only.

## Decision

Remove the five root modules. Callers must use the canonical owners:

| Removed path | Canonical owner |
| --- | --- |
| `neuro_code.bash_commands` | `neuro_code.domain.permissions.bash_commands` |
| `neuro_code.config` | `neuro_code.configuration.app` |
| `neuro_code.permissions` | `neuro_code.application.permissions.policy` |
| `neuro_code.workspace` | `neuro_code.infrastructure.workspace.paths` |
| `neuro_code.workspace_changes` | `neuro_code.infrastructure.workspace.changes` |

All production and ordinary test consumers use canonical imports. Architecture
tests assert that the removed modules cannot be discovered and that canonical
imports do not load them. The historical `Path.home` patch seam is updated to
patch `neuro_code.configuration.app.Path.home` directly.

At the time this ADR was accepted, it did not remove:

- `neuro_code.adapters.*` facades;
- `neuro_code.tools.*` facades;
- flat conversation/domain facades such as `neuro_code.domain.messages`;
- canonical domain packages or aggregate package exports.

Those paths required separate compatibility decisions. The later consumer
audit and removal are recorded in
[ADR 0074](0074-remove-adapter-tool-domain-facades.md); this historical ADR
must not be read as retaining those paths today.

## Consequences

- The five removed import paths are intentionally breaking changes.
- Runtime, Provider, SessionStore, permissions, workspace, and tool behavior do
  not change; only import locations change.
- Package smoke and architecture contracts can fail closed if one of the
  removed modules is reintroduced.
- External callers using a removed path must migrate to the canonical owner.

## Validation

The removal is accepted only with passing architecture import contracts,
configuration/provider-settings tests, the affected application and TUI tests,
Ruff, formatting, mypy, documentation parity, and `git diff --check`.
