# ADR 0112: Windows local-process sandbox AppContainer feasibility decision

## Status

Accepted. The investigated classic stable unpackaged Windows AppContainer
architecture is deferred and is not a production sandbox adapter. Explicit
Windows filesystem/network profiles remain unsupported and fail closed. The
existing `off` path and its Windows Job Object/ConPTY lifecycle contract are
unchanged.

## Context

Neuro Code's enabled local-process profiles must remain child-scoped and
fail-closed while supporting ordinary developer workloads. The production
contract used for this decision requires all of the following:

- non-administrator operation;
- no ACL expansion on protected host ancestors such as `C:\`, `C:\Users`, or
  `C:\Program Files`;
- no broad `USERPROFILE` or host-directory authority;
- no unsandboxed Git broker; and
- no bundled or permanently patched Git fork.

The W0 evidence phase investigated the classic stable unpackaged AppContainer
architecture without changing production code. Evidence PRs #33--#39 remain
separate, unmerged evidence branches:

| Evidence | Result |
|---|---|
| #33 AppContainer primitives | AppContainer, Job Object, and ConPTY primitives passed; the controller `HANDLE_LIST` probe remained blocked. |
| #34 named-pipe/IPC architecture | Secured named-pipe bootstrap and MCP byte-stream semantics passed. |
| #35 filesystem and ACL authority | Read-only/read-write authority, identity, hardlink/reparse isolation, crash recovery, and one-shot SID behavior passed. |
| #36 runtime and non-admin | Python, Node, PowerShell, `cmd`, real MCP, and standard-user core workloads passed; Git was blocked. |
| #37 Git NUL compatibility | Stock Git was blocked; a minimal evidence patch provided only partial compatibility. |
| #38 Git path canonicalization | A documented cwd fallback was partial; full Git compatibility remained blocked. |
| #39 Git repository discovery | Some ceiling/discovery scenarios passed, but full `init`/`add`/`status`/`rev-parse` remained blocked and protected ancestors were not production-eligible. |

The evidence therefore distinguishes viable AppContainer primitives from the
complete developer-tool compatibility contract. It does not conclude that
AppContainer is inherently unsafe or that Windows cannot provide a sandbox.

## Decision

The classic stable unpackaged AppContainer architecture does not currently
enter production. The blocker is compatibility of the current stock Git for
Windows runtime with protected-ancestor path canonicalization and repository
discovery under the non-admin, no-ACL-expansion contract. Granting broad
ancestor authority would violate the filesystem boundary; granting narrowly
scoped rights to a disposable owned parent was diagnostic evidence only and
does not make `C:\`, `C:\Users`, or `C:\Program Files` production-eligible.

Windows enabled `workspace`, `read-only`, and `strict` filesystem/network
profiles therefore remain **unsupported / fail closed**. This is an explicit
capability decision, not a silent fallback.

The Windows `off` path remains supported. Its existing process lifecycle uses
Job Object and ConPTY ownership and reports
`STRONG_DESCENDANT_OWNERSHIP`; this decision does not weaken or relabel that
contract. Filesystem and network sandbox authority is independent from that
lifecycle capability.

## Rejected production alternatives

The following alternatives are not production decisions in this ADR:

- **Protected-ancestor ACL expansion:** rejected because it would require
  administrator or non-owned ACL changes and could expose parent metadata or
  sibling names/content.
- **Bundled or patched Git:** rejected because it would create a maintained
  runtime fork and would not establish compatibility for the user's stock
  developer-tool environment.
- **Unsandboxed Git broker:** rejected because it would move Git execution and
  authority outside the child-scoped sandbox contract.
- **Windows Sandbox/Hyper-V as the default adapter:** rejected for now because
  a VM/isolated desktop is not a transparent local child boundary with the
  required Job, ConPTY, MCP, and ordinary CLI semantics.

## Future re-evaluation candidates

Reconsideration requires evidence for one of these independent changes:

1. Git for Windows upstream resolves the NUL, path-canonicalization, and
   repository-discovery incompatibilities while preserving the non-admin and
   no-broad-ACL contract.
2. Microsoft ships a documented, stable, non-admin, child-scoped primitive
   that supports arbitrary CLI processes and composes with Job Object,
   ConPTY, and MCP. Possible future candidates include a stable Win32 App
   Isolation release or a stable successor to
   `Experimental_CreateProcessInSandbox`/BFS. Current preview or experimental
   behavior is not a production guarantee.

No Endpoint Security-style helper, privileged broker, production adapter, or
new evidence probe is introduced by this decision.

## Compatibility consequence

The compatibility matrix records Windows as follows:

| Area | Windows state |
|---|---|
| `off` | Supported; no OS filesystem/network sandbox is promised. |
| Lifecycle | `STRONG_DESCENDANT_OWNERSHIP` through the existing Job Object/ConPTY path. |
| `workspace` | Unsupported / fail closed. |
| `read-only` | Unsupported / fail closed. |
| `strict` | Unsupported / fail closed. |

This ADR records the W0 evidence decision only; it does not alter production
code or merge any evidence PR.
