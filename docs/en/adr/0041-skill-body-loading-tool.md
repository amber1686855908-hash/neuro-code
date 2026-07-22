# ADR 0041: Read-only skill body loading tool

[简体中文](../../zh-CN/adr/0041-skill-body-loading-tool.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

ADR 0040 advertises compact skill metadata, but the model needs an explicit way
to load a selected workflow only when it is relevant.

## Decision

Register a non-side-effecting `skill` tool. It resolves a discovered skill by
its deduplicated name through the binding's `SkillContextTracker`, validates
the normalized relative path against the skill's LOCAL, REPO, or USER root,
and performs the shared bounded, symlink-resistant read.

The loaded bytes must still match the discovery content fingerprint. A change
between discovery and loading produces a retryable error instead of mixing
stale metadata with a new body. The tool validates UTF-8 and controls, strips
BOM and frontmatter, and returns a bounded `<skill_content>` block.

The output includes the base directory and up to 10 direct regular-file names.
Bundled links, directories, control-character names, and directories exceeding
the 256-entry listing bound are omitted. No bundled file is executed or read by
this listing.

## Consequences

- Tool output obeys `ToolContext.output_byte_limit`; over-limit content fails
  instead of being silently truncated.
- `ToolContext` depends on a tracker port, preserving the ports/runtime
  dependency direction.
- Loading guidance may lead the model to request other tools, but those tools
  retain their normal permission and sandbox checks.
- ADR 0045 adds bounded argument substitution to this loading path.
