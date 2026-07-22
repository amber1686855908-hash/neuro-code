# ADR 0040: Bounded read-only skill discovery

[简体中文](../../zh-CN/adr/0040-read-only-skill-discovery.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

After repository instructions, the next vertical capability is discovering
local `SKILL.md` reference documents without downloading or executing them.
Full bodies should not inflate every model prompt.

## Decision

Add a `SkillDiscovery` port, domain values, and a
`FilesystemSkillDiscovery` adapter. The adapter scans `skills/` beneath
`.neuro`, `.agents`, `.grok`, and `.claude`, in that priority order. It parses
bounded frontmatter for `name`, `description`, and `when-to-use`; absent or
malformed metadata falls back to the directory name and first prose body line.
Frontmatter delimiters must occupy complete lines.

The adapter rejects all links/reparse points and unsafe or non-portable paths.
It caps recursive skill depth at 5, ancestor traversal at 64, visited skill
directories at 200, entries per directory at 1,000, candidates at 200, loaded
skills at 50, each file at 64 KiB, and total accepted reads at 512 KiB. The
model catalog is separately capped at 64 KiB.

Skills are first-seen-wins by normalized name, with scope priority
`LOCAL > REPO > USER`. Discovery fingerprints include the full bounded file
content even though only metadata enters the catalog. The catalog is a
transient `User` item tagged `AVAILABLE_SKILLS` and tells the model to use the
read-only `skill` tool for a relevant body.

## Consequences

- Skill files are data, never executable plugins.
- The simple parser intentionally does not implement full YAML.
- `.cursor` vendor skills, conditional `paths:` activation, server/bundled
  skills, hooks, plugins, and remote synchronization remain out of scope.
- ADRs 0042–0044 extend the initial LOCAL scope without changing this safety
  boundary.
