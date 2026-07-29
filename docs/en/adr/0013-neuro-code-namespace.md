# ADR 0013 — Neuro Code namespace

[简体中文](../../zh-CN/adr/0013-neuro-code-namespace.md) · **English**

## Status

Accepted.

## Context

The project is becoming an independent coding agent. Keeping its historical
bootstrap name in package imports, commands, environment variables, state
directories, error types, tests, and documentation would make that origin a
permanent public contract and create naming conflicts for future features.

## Decision

Use one namespace consistently:

- product name: `Neuro Code`;
- distribution and executable: `neuro-code`;
- Python package: `neuro_code`;
- environment variables: `NEURO_CODE_*`;
- user and project state directory: `.neuro-code`;
- expected application error root: `NeuroCodeError`.

Historical command, import, environment-variable, and state-directory aliases
are not retained. Provider models are always explicit; no vendor-branded model
identifier is supplied as an application default. CC Switch input is opt-in
through `NEURO_CODE_CC_SWITCH_CONFIG` instead of coupling discovery to another
tool's provider-specific directory layout.

The product, runtime code, and ordinary documentation use the project-owned
namespace and describe Neuro Code as independently developed.

## Consequences

The rename is intentionally breaking for local commands, imports, environment
variables, and state paths. Existing data can be retained by moving it into the
new state directory before launch and updating configuration automation.
Installed metadata and editable environments must be refreshed after pulling
the change. Future features now have a single project-owned namespace.
