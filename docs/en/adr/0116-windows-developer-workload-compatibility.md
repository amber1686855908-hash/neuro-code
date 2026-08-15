# ADR 0116: Windows developer-workload compatibility baseline

- Status: W5 Gate 0 evidence collection; no production compatibility fix
- Date: 2026-08-16
- Scope: ordinary Windows developer workloads through the W3 and W4 routes

## Decision

W5 starts with a compatibility-only baseline.  The evidence branch measures
the current W4-merged tree at `716d56c2e769af5868e03d8e05d15eadec1cd8df`; it
does not change the Windows sandbox implementation, token model, setup
authority, ACLs, Firewall, private profile, Job ownership, or ConPTY.

Each workload is first run as a host control, then with the same resolved
executable and an equivalent argv through the production W3 non-PTY
`WindowsNativeLocalProcessSandbox.spawn()` route and the production W4 PTY
route (`spawn_terminal()` through `LocalInteractiveTerminalManager`).  The
primary profile is `WORKSPACE` with the Online identity.  Missing tools are
recorded as `NOT_INSTALLED`; observed workload failures are evidence rather
than automatic compatibility fixes.

The focused Windows job emits bounded JSON and JUnit artifacts.  The JSON
artifact, not a manually inferred summary, is the authoritative record of
tool provenance, every HOST/W3/W4 cell, classification, and correlation.
Credentials, environment secrets, handles, and full unbounded output are not
recorded.

## Frozen security contract

The matrix must preserve the already-certified W1-W4 contract:

| Contract | Current value |
| --- | --- |
| Read isolation | `LIMITED` |
| Write isolation | `STRONG` |
| Network isolation | `STRONG` |
| Descendant lifecycle | `STRONG_DESCENDANT_OWNERSHIP` |
| Primary profile | `WORKSPACE` supported |
| Read-only profile | supported |
| Strict profile | fail closed because strong read isolation is unavailable |

`TokenRestrictedSids` remains the exact singleton installation write SID;
`SeChangeNotifyPrivilege` and the existing privilege, ACL, Firewall, private
HOME/TEMP, identity, Job, named-pipe, and ConPTY boundaries are unchanged.
The matrix is not permitted to add a SID, privilege, fallback, or authority
just to make a workload pass.

## Matrix workload set

The data-driven acceptance module covers deterministic startup and local
filesystem operations only:

- `CMD_BASIC` and the distinct `CMD_NUL_REDIRECT` exit-oracle row;
- Windows PowerShell and `pwsh` when installed;
- Python version, `-I -S`, `-I`, and normal startup rows;
- Git version, repository discovery, and `status --porcelain=v1` in a
  disposable repository inside the authorized workspace;
- Node version/`-e` execution and the actually resolved npm launcher;
- curl `--version` only;
- a native acceptance-only `NUL_DIRECT_WIN32` probe using documented
  `CreateFileW(L"NUL")` and `WriteFile` calls.

No package download, public network dependency, global Git configuration,
execution-policy change, or compatibility workaround is part of Gate 0.  No
exact prior restricted-curl command exists in the current W3 evidence, so no
synthetic replacement is presented as a reproduction.

## Result interpretation

The result taxonomy distinguishes `PASS`, `NOT_INSTALLED`, process creation
and access errors, device access denial, runtime/dependency initialization,
repository discovery, timeout, non-zero exit, output mismatch, and
`INCONCLUSIVE`.  A HOST failure is fixture/tool evidence, not sandbox
compatibility evidence.  A HOST pass with failures in both W3 and W4 is a
shared restricted-runtime candidate; a W3-only or W4-only failure is retained
as transport-specific evidence.  These are hypotheses for the next W5 step,
not fixes or causal claims.

## Next decision boundary

After the focused CI artifact is reviewed, the highest-impact compatibility
candidate will be ranked by affected workloads, W3/W4 sharing, developer
workflow impact, and whether it can be addressed without weakening the frozen
security contract.  Gate 0 does not implement that candidate.  W5 Gate 1 has
not started.

