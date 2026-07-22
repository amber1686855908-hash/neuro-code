# ADR 0039: Repository instruction discovery

[简体中文](../../zh-CN/adr/0039-repository-instruction-discovery.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

Neuro Code needs project conventions without granting repository text the trust
level of the application system prompt. The pinned Rust baseline represents
project instructions as a tagged synthetic user item, but its static and
runtime discovery paths are not one coherent production flow.

## Decision

Add an `InstructionDiscovery` port and a bounded
`FilesystemInstructionDiscovery` adapter. For the current binding target, the
adapter reads `AGENTS.md` from the workspace root down to the target directory
and returns files shallowest-first. Only the exact `AGENTS.md` name is in scope.

Discovery permits at most 20 directory levels, 10 loaded files, 64 KiB per
file, and 256 KiB total. Reads require stable regular-file identity, valid
UTF-8, and no forbidden C0/C1/DEL controls. All symlinks and Windows reparse
points are rejected and classified for audit; rejection paths escape controls.

Before every model step, bounded discovery runs off the event-loop thread. Its
content is injected as a transient `User` message tagged
`PROJECT_INSTRUCTIONS`, after the system message and before genuine user input.
The marker and message are never persisted.

`InstructionTracker` keeps a moving target and separately records the result
actually injected into the latest model step. `search_replace` compares the
current target instructions with that snapshot by path and content; new or
changed rules abort the write as an error. Arbitrary Bash paths cannot be
derived reliably, so Bash writes do not have this preflight guarantee.

## Consequences

- CLI, TUI, and ACP share the same discovery port and default adapter.
- Same-session changes appear on the next model step and session resume always
  performs fresh discovery.
- `inspect` exposes paths, depths, byte counts, rejection reasons, and a stable
  content fingerprint.
- Claude/rules compatibility names, gitignore filtering, and Bash path parsing
  remain outside this slice.
