# ADR 0042: User-level skill discovery

[简体中文](../../zh-CN/adr/0042-user-level-skill-discovery.md) · **English**

- Status: accepted
- Date: 2026-07-22

## Context

Users need personal skills that apply across workspaces, while project-local
skills must remain able to override them.

## Decision

Extend `FilesystemSkillDiscovery` with an optional `user_home`. Production
resolves `Path.home()` at discovery time and scans the same fixed config
directories under that root as `USER` scope. A resolution failure safely skips
the USER pass, and a home equal to the workspace is not scanned twice.

`SkillInfo.root` records the resolved boundary used to interpret its normalized
relative path. The skill tool therefore validates USER files against the user
root rather than incorrectly forcing them inside the workspace. LOCAL
candidates are processed first, so a same-named project skill shadows a user
skill regardless of config-directory priority.

## Consequences

- Personal skill discovery is explicit and read-only; it does not broaden
  arbitrary workspace tool access to the home directory.
- Tests supply or isolate the home root so developer-machine skills cannot
  contaminate results.
- Vendor-default deny lists and remotely managed user skills remain out of
  scope.
