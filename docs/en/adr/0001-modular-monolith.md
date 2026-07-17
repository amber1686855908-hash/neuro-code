# ADR 0001: Python modular monolith instead of crate mirroring

[简体中文](../../zh-CN/adr/0001-modular-monolith.md) · **English**

- Status: accepted
- Date: 2026-07-17
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

The source workspace contains more than 70 crates and roughly 419,000 lines of
Rust. Its crate boundaries reflect Rust compilation, ownership, generated
monorepo structure, and historical migrations. Copying those boundaries would
create many Python packages without improving runtime isolation.

## Decision

Use one distribution and one import package with explicit domain, application,
port, adapter, interface, tool, and platform modules. Dependencies point from
interfaces toward domain contracts; infrastructure is selected only at the
composition root. Split a separately versioned package only when an external
consumer or process boundary proves the need.

## Consequences

- End-to-end slices can evolve without cross-package release coordination.
- Static typing and tests, rather than Cargo, enforce boundaries.
- Upstream crate-to-module mapping is many-to-many and belongs in compatibility
  evidence, not in directory names.
- Import-cycle and forbidden-dependency checks become required as the codebase
  grows.
