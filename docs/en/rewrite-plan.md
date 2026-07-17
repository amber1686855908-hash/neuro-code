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

- Cross-platform package, CLI, configuration, errors, domain events, and ports.
- `version`, `inspect`, and machine-readable output.
- Ruff, mypy, pytest/unittest bootstrap and CI-ready commands.

Exit: the project installs and the same contract suite passes on all targets.

### M2 — headless coding-agent vertical slice

- OpenAI-compatible, Anthropic, and Gemini streaming providers behind one
  normalized multi-provider port.
- Agent/tool loop, structured tool calls, cancellation, step limit, bounded
  output, and owned process-tree cleanup.
- Read/list/grep/search-replace/bash tools and a headless permission policy
  that evaluates every safely decomposable shell-command segment.
- SQLite sessions, resume, and JSON/Markdown export foundation.

Exit: a fake-provider end-to-end test performs read, edit, command, and final
response; an opt-in live test can do the same against a configured provider.

### M3 — core TUI and platform safety

- Textual UI over the same event stream: prompt, scrollback, tool cards,
  approval, model picker, and essential slash commands.
- PTY/process tree, resize, signals, clipboard, filesystem notifications.
- `off`, `workspace`, `read-only`, and `strict` sandbox profiles.

Exit: interactive smoke suites pass on Linux, macOS, and Windows without leaked
terminal state or child processes.

### M4 — protocols and extensibility

- ACP stdio/WebSocket plus advertised `x.ai/*` extensions.
- MCP lifecycle, skills, AGENTS.md, agent profiles, hooks, and plugins.
- Subagents, plan mode, session fork, and background tasks.

Exit: standard ACP clients pass conformance scenarios and extension failures do
not corrupt the primary session.

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
