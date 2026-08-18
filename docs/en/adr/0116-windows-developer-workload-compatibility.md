# ADR 0116: Windows developer-workload compatibility baseline

- Status: Accepted; W5 workload compatibility validated on the production W3/W4 routes
- Date: 2026-08-16
- Scope: ordinary Windows developer workloads through the W3 and W4 routes

## Decision

The W5 compatibility baseline is an evidence-only measurement of the current
production W3/W4 routes. Run `32193614626` (head
`a31097d47fcd40b01209770438f5b87a0a1c50a9`) validated the Windows Server 2025
matrix; the workload probes do not change the Windows sandbox implementation,
token model, setup authority, ACLs, Firewall, private profile, Job ownership,
or ConPTY.

Each workload is first run as a host control, then with the same resolved
executable and an equivalent argv through the production W3 non-PTY
`WindowsNativeLocalProcessSandbox.spawn()` route and the production W4 PTY
route (`spawn_terminal()` through `LocalInteractiveTerminalManager`). The
primary profile is `WORKSPACE` with the Online identity. Missing tools are
recorded as `NOT_INSTALLED`; observed results are evidence rather than a
reason to weaken a security boundary.

The focused Windows job emits bounded JSON and JUnit artifacts. The JSON
artifact, not a manually inferred summary, is authoritative for tool
provenance, every HOST/W3/W4 cell, classification, and correlation. Credentials,
environment secrets, handles, and full unbounded output are not recorded.

## Frozen security contract

The matrix preserves the already-certified W1-W4 contract:

| Contract | Current value |
| --- | --- |
| Read isolation | `LIMITED` |
| Write isolation | `STRONG` |
| Network isolation | `STRONG` |
| Descendant lifecycle | `STRONG_DESCENDANT_OWNERSHIP` |
| Primary profile | `WORKSPACE` supported |
| Read-only profile | supported |
| Strict profile | fail closed because strong read isolation is unavailable |

`TokenRestrictedSids` remains the exact ordered production set; the existing
privilege, ACL, Firewall, private HOME/TEMP, identity, Job, named-pipe, and
ConPTY boundaries are unchanged. The matrix is not permitted to add a SID,
privilege, fallback, or authority just to make a workload pass.

For an enabled W3/W4 cell, `token_attestation=PASS` is emitted only when the
diagnostic facts match the selected Online W2 identity, `IsTokenRestricted=true`,
the production restricting-SID set, enabled `SeChangeNotifyPrivilege`, and
zero unexpected enabled privileges.

## Matrix workload set

The data-driven acceptance module covers deterministic startup and local
filesystem operations only:

- `CMD_BASIC` and the distinct `CMD_NUL_REDIRECT` exit-oracle row;
- Windows PowerShell and `pwsh` when installed;
- venv Python version, `-I -S`, `-I`, normal startup, and a child-Python
  subprocess, plus verified base-interpreter version and `-I -S` rows;
- Git version, repository discovery, and `status --porcelain=v1` in a
  disposable repository inside the authorized workspace;
- Node version/`-e` execution and the actually resolved npm launcher;
- curl `--version` only;
- a native `NUL_DIRECT_WIN32` probe using documented `CreateFileW(L"NUL")`
  and `WriteFile` calls;
- a dynamic `bcrypt.dll` load followed by `BCryptGenRandom` using the final
  restricted child token.

No package download, public network dependency, global Git configuration,
execution-policy change, or compatibility workaround is part of this matrix.

## Result interpretation

The result taxonomy distinguishes `PASS`, `NOT_INSTALLED`, process creation and
access errors, device access denial, runtime/dependency initialization,
repository discovery, timeout, non-zero exit, output mismatch, and
`INCONCLUSIVE`. Every installed W3/W4 cell in the accepted artifact reached
`SpawnReady` and retained a passing token attestation before the workload result
was observed.

## W5 compatibility evidence record

Run `32193614626` completed the 20-row matrix on Windows Server 2025 hosted
`windows-latest`, `WORKSPACE`, and the Online identity. All HOST, W3 non-PTY,
and W4 PTY cells are `PASS / 0`:

| Workload / variant | HOST | W3 non-PTY | W4 PTY |
| --- | --- | --- | --- |
| `CMD_BASIC` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `CMD_NUL_REDIRECT` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `POWERSHELL_BASIC` / Windows PowerShell | PASS / 0 | PASS / 0 | PASS / 0 |
| `PWSH_BASIC` / pwsh | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_MINIMAL_NO_SITE` / `-I -S` | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_ISOLATED` / `-I` | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_NORMAL` / normal | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_CHILD_PROCESS` / subprocess | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_BASE_VERSION` / verified base interpreter | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_BASE_MINIMAL_NO_SITE` / base `-I -S` | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_REPO_DISCOVERY` / disposable repo | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_STATUS` / porcelain v1 | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_EXEC` / `-e` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NPM_VERSION` / resolved `npm.cmd` | PASS / 0 | PASS / 0 | PASS / 0 |
| `CURL_VERSION` / `--version` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NUL_DIRECT_WIN32` / `CreateFileW` + `WriteFile` | PASS / 0 | PASS / 0 | PASS / 0 |
| `BCRYPT_CNG_RUNTIME` / dynamic `bcrypt.dll` + `BCryptGenRandom` | PASS / 0 | PASS / 0 | PASS / 0 |

The access-mode-specific NUL evidence is also passing:

| NUL access mode | HOST | W3 non-PTY | W4 PTY |
| --- | --- | --- | --- |
| `NUL_READ` (`GENERIC_READ`) | Create PASS / error 0; write not attempted | Create PASS / error 0; write not attempted | Create PASS / error 0; write not attempted |
| `NUL_WRITE` (`GENERIC_WRITE`) | Create PASS / error 0; `WriteFile` PASS | Create PASS / error 0; `WriteFile` PASS | Create PASS / error 0; `WriteFile` PASS |
| `NUL_READ_WRITE` (both) | Create PASS / error 0; `WriteFile` PASS | Create PASS / error 0; `WriteFile` PASS | Create PASS / error 0; `WriteFile` PASS |

The artifact records bounded PASS evidence for Windows PowerShell, PowerShell
7, Python and its child process, Git repository operations, Node/npm, curl,
NUL read/write modes, and dynamic BCrypt CNG startup. Natural W3/W4 completions
record `job_active_processes_after_quiesce=0` and
`relay_threads_after_join=0`. The matrix is workload evidence, not permission
to weaken token, SID, ACL, environment, or lifecycle contracts.

## Next decision boundary

No workload-specific compatibility blocker remains in the measured matrix.
Future developer tools or network workloads require their own bounded fixture
and artifact row; they must not be inferred from these local startup probes or
used to weaken the current security contract.
