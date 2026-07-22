# ADR 0044: Repository-level skill discovery

[简体中文](../../zh-CN/adr/0044-repository-level-skill-discovery.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

When a workspace is a subdirectory of a repository, repository and monorepo
package skills can live above the workspace and would otherwise be invisible.

## Decision

Detect the nearest git root with a bounded, filesystem-only upward search for
a regular `.git` directory or file. Links/reparse points are not accepted as
repository markers. An explicit `git_root` remains available for tests.

For `REPO` scope, scan every ancestor above the workspace through the git root,
closest-first. This includes intermediate monorepo locations, not only the git
root. All REPO relative paths use the git root as their common boundary, so
paths stay unique and the skill tool can reopen them safely. A nearer
same-named REPO skill shadows a git-root default. Overall priority remains
`LOCAL > REPO > USER`.

## Consequences

- No `git` subprocess is spawned in ACP/TUI event loops.
- Worktree `.git` files are recognized as markers, but their referenced target
  is not parsed; bare repositories and non-standard layouts are not inferred.
- Repository ancestor traversal shares the global discovery budgets and the
  64-level cap.
- Server, bundled, and plugin scopes remain unimplemented.
