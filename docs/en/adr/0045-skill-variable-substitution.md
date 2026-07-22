# ADR 0045: Bounded skill variable substitution

[简体中文](../../zh-CN/adr/0045-skill-variable-substitution.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

The pinned Rust baseline lets a loaded skill consume invocation arguments and
refer to its own directory. Neuro Code has no slash-command, session-token, or
plugin-token surface yet, but these core substitutions are useful to the
model-invoked skill tool.

## Decision

Add optional string `args` to the `skill` tool and apply substitutions only to
the selected body at load time:

- `$ARGUMENTS` becomes the trimmed full argument string.
- `$ARGUMENTS[N]` and `$N` use zero-based whitespace-split arguments.
- `${SKILL_DIR}` becomes the selected skill directory.
- When no supported argument token occurs and arguments are non-empty, append
  `**ARGUMENTS:** ...` for compatibility.

Arguments are capped at 8 KiB and one body may perform at most 32 supported
substitutions. Positional probing uses the Rust-compatible bounded window;
unsupported or price-like tokens such as `$100`, huge numeric indexes, and
unknown `${...}` names remain literal. Callable replacements preserve Windows
backslashes without regular-expression replacement semantics.

## Consequences

- Empty or whitespace-only arguments add no suffix.
- Path-only substitution does not consume arguments, so it still receives the
  compatibility suffix.
- `${SESSION_ID}`, Claude aliases, plugin roots/data, slash commands, and plugin
  execution remain out of scope.
