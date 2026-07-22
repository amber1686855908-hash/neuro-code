# ADR 0043: Dynamic session skill discovery

[简体中文](../../zh-CN/adr/0043-dynamic-session-skill-discovery.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

Root-only discovery misses skills defined for a nested project area. The Rust
baseline discovers skills near filesystem paths accessed during a session.

## Decision

Give each binding a `SkillTracker` with one moving target. `read_file`,
`list_dir`, and `grep` move it to the accessed directory; grep applies its
final update on the event-loop thread after the blocking walk. Before the next
model step, discovery walks from the target upward to the workspace root,
deepest-first. A deeper same-named skill therefore shadows a shallower one,
and moving to a sibling subtree removes the previous subtree's skills.

All LOCAL paths stay relative to one workspace root, including their nested
prefix (for example `src/api/.neuro/skills/review/SKILL.md`). This prevents
ambiguous paths and fingerprint collisions between sibling subtrees. The
ancestor walk is bounded; when it exceeds the cap, intermediate levels are
omitted with a rejection while workspace-root defaults are still scanned.

## Consequences

- Additions, removals, body changes, and target changes appear on the next
  model step without restarting the session.
- `search_replace` does not move the skill target, because skills are guidance
  rather than write authorization. Bash paths remain intentionally unparsed.
- The single-target policy keeps context focused but does not merge skills from
  multiple sibling paths accessed in one step.
