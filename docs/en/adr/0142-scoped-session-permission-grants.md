# ADR 0142 — Scoped session permission grants

[简体中文](../../zh-CN/adr/0142-scoped-session-permission-grants.md) · **English**

## Status

Accepted.

## Context

`ALLOW_SESSION` previously cached only the exact SHA-256 scope of one tool and
its complete arguments. That is safe, but an ordinary review or test workflow
asks again for every changed file and every slightly different command. A
broader grant must reduce prompts without becoming a blanket Bash, edit, or
workspace bypass.

The existing permission policy, canonical filesystem plan, sandbox, capability
manifest, and tool execution pipeline remain separate authorities. This ADR
adds only a process-local approval-memory layer above those authorities.

## Decision

- Keep exact-action `ALLOW_SESSION` unchanged. Its digest is memory-only and is
  still subordinate to a fresh `PermissionManager` decision on every call.
- Add typed runtime-generated candidates for `WORKSPACE_EDITS` and
  `COMMAND_FAMILY`. A candidate is not a model value and cannot be created by a
  provider, planner, worker, ACP payload, or tool argument. The broker accepts a
  scoped approval only when the exact candidate is present on the request.
- Add a typed decision source. Broad candidates are emitted only for the
  ordinary interactive default `ASK`. Explicit `DENY`, explicit `ASK`, mode
  decisions, headless decisions, and already-allowed calls produce no broad
  candidate. Explicit policy therefore remains stronger than approval memory.
- A workspace-edit candidate is generated only after the existing immutable
  `FilesystemAccessPlan` proves that every target is in the primary canonical
  root, has no link-like traversal, is an ordinary `CREATE` or `UPDATE`, and is
  not Neuro metadata, checkpoint/internal state, or an obvious credential/key
  target. Deletes, moves, additional roots, ambiguous paths, and failed plans
  remain exact-or-deny paths.
- A command-family candidate is generated only by the existing conservative
  Bash tokenizer plus a strict single-command classifier. The accepted forms
  are `pytest` (including the supported Python/`uv run` forms), read-only
  `ruff`/`mypy` checks, and bounded `git` read commands. Composition, wrappers,
  nested interpreters, substitutions, redirection, background execution,
  absolute/parent paths, unsafe options, and high-risk commands remain
  exact-or-deny paths.
- Every candidate is bound to a trusted logical session identity and the
  canonical primary workspace root. The broker stores grants only in process
  memory; no SQLite schema, export/import state, or persistent rule file is
  changed. A fresh broker/process has no previous grant.
- Equivalent requests queued behind an approval wait for that decision and
  then re-check the grant before opening a modal. An allow-once, denial, or
  cancellation wakes waiters without granting them. A malformed scoped response
  is downgraded to allow-once and never enters the cache.
- The TUI keeps deny as the initial focus and displays only the canonical root
  and typed family metadata. It never displays patch bodies, replacement text,
  credentials, or unrestricted command arguments. ACP continues to expose only
  its existing exact-action options, so it cannot invent a remote broad scope.
- Requested/resolved audit events remain in place and carry only bounded scope
  metadata and a `cache_hit` flag. Tool start ordering, headless denial, modes,
  sandbox, capability ceilings, Worktree, Checkpoint/Rollback, and Ultracode
  behavior are unchanged.

## Consequences

In the supported interactive path, repeated ordinary edits in one primary
workspace and repeated commands in one recognized family no longer require a
new modal after the user selects that typed scope. Exact approval remains
available when no safe broad candidate exists. Approval memory is intentionally
lost on process restart and cannot create durable policy.

The command classifier is deliberately narrower than a shell interpreter. New
command forms require a separate proof and tests; unknown or high-risk forms
continue to prompt for an exact action or fail closed. No automatic grant is
available to `bash`, arbitrary shell composition, destructive Git/filesystem
operations, network-side-effect commands, terminal creation, MCP, provider
tools, or writable-subagent capability construction.

## Validation

Focused and production-path tests cover canonical multi-file edits, protected
and non-primary targets, explicit-policy precedence, command-family rejection,
session/workspace isolation, process-memory restart behavior, queued approvals,
cancellation, TUI Pilot interaction, exact compatibility, and a representative
approval-fatigue count. Full repository validation remains the completion gate:
lock and docs parity, Ruff, formatting, mypy, coverage at least 85%, package
build, and diff whitespace checks.
