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

For an enabled W3/W4 cell, `token_attestation=PASS` is emitted only when the
diagnostic facts match the selected Online W2 identity, `IsTokenRestricted=true`,
the exact singleton synthetic write SID, enabled `SeChangeNotifyPrivilege`,
and zero unexpected enabled privileges.

## Matrix workload set

The data-driven acceptance module covers deterministic startup and local
filesystem operations only:

- `CMD_BASIC` and the distinct `CMD_NUL_REDIRECT` exit-oracle row;
- Windows PowerShell and `pwsh` when installed;
- venv Python version, `-I -S`, `-I`, and normal startup rows, plus the
  verified base-interpreter version and `-I -S` rows;
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

## Gate 0 evidence record

The PR body is the canonical pointer to the latest exact-head CI run and its
artifact.  This ADR intentionally does not hard-code a volatile current-run
ID.  Historical run IDs may appear only with the exact commit they tested.
The `windows-native-sandbox-compatibility` job must execute one matrix test with
zero skips and upload bounded JSON and JUnit artifacts.  The matrix uses
Windows Server 2025 hosted `windows-latest`, `WORKSPACE`, and the Online
identity.

Tool provenance recorded by the controller:

| Tool | Resolved path and version |
| --- | --- |
| `cmd.exe` | `C:\Windows\System32\cmd.exe`; Windows 10.0.26100.33158 |
| `powershell.exe` | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`; 5.1.26100.33158 |
| `pwsh.exe` | `C:\Program Files\PowerShell\7\pwsh.exe`; 7.6.4 |
| `python.exe` | `D:\a\neuro-code\neuro-code\.venv\Scripts\python.exe`; 3.12.10 |
| base `python.exe` | discovered from the venv's verified `sys._base_executable`; the artifact records `C:\\hostedtoolcache\\windows\\Python\\3.12.10\\x64\\python.exe` / Python 3.12.10 |
| `git.exe` | `C:\Program Files\Git\bin\git.exe`; 2.55.0.windows.3 |
| `node.exe` | `C:\Program Files\nodejs\node.exe`; v22.23.2 |
| `npm.cmd` | `C:\Program Files\nodejs\npm.cmd`; 10.9.8 |
| `curl.exe` | `C:\Windows\System32\curl.exe`; 8.16.0 Schannel |

The table records `classification` and exit code for `HOST / W3 / W4`; `T`
means the bounded timeout was reached.  Every installed W3/W4 cell reached
`SpawnReady` and retained a passing token attestation before the workload
result was observed.

| Workload / variant | HOST | W3 non-PTY | W4 PTY |
| --- | --- | --- | --- |
| `CMD_BASIC` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `CMD_NUL_REDIRECT` / default | PASS / 0 | DEVICE_ACCESS_DENIED / 1 | DEVICE_ACCESS_DENIED / 1 |
| `POWERSHELL_BASIC` / Windows PowerShell | PASS / 0 | RUNTIME_INITIALIZATION_FAILURE / 4294901760 | RUNTIME_INITIALIZATION_FAILURE / 4294901760 |
| `PWSH_BASIC` / pwsh | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_VERSION` / default | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_MINIMAL_NO_SITE` / `-I -S` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_ISOLATED` / `-I` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_NORMAL` / normal | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_BASE_VERSION` / verified base interpreter | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_BASE_MINIMAL_NO_SITE` / base `-I -S` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `GIT_VERSION` / default | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `GIT_REPO_DISCOVERY` / disposable repo | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `GIT_STATUS` / porcelain v1 | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `NODE_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_EXEC` / `-e` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NPM_VERSION` / resolved `npm.cmd` | PASS / 0 | PASS / 0 | PASS / 0 |
| `CURL_VERSION` / `--version` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `NUL_DIRECT_WIN32` / `CreateFileW` + `WriteFile` | PASS / 0 | DEVICE_ACCESS_DENIED / 2 (Win32 5) | DEVICE_ACCESS_DENIED / 2 (Win32 5) |

The venv and verified base interpreter both report Python 3.12.10 on the host,
but they are distinct executables; all six Python rows time out in both W3 and
W4. The base rows therefore do not establish a venv-only startup cause.

The access-mode-specific NUL evidence is retained separately in the artifact:

| NUL access mode | HOST | W3 non-PTY | W4 PTY |
| --- | --- | --- | --- |
| `NUL_READ` (`GENERIC_READ`) | Create PASS / error 0; write not attempted | Create PASS / error 0; write not attempted | Create PASS / error 0; write not attempted |
| `NUL_WRITE` (`GENERIC_WRITE`) | Create PASS / error 0; `WriteFile` PASS | Create FAIL / error 5; `WriteFile` not attempted | Create FAIL / error 5; `WriteFile` not attempted |
| `NUL_READ_WRITE` (both) | Create PASS / error 0; `WriteFile` PASS | Create FAIL / error 5; `WriteFile` not attempted | Create FAIL / error 5; `WriteFile` not attempted |

Important bounded error facts are retained in the artifact: Windows PowerShell
reported `HRESULT 80070005` (Win32 `2147942405`) while starting the CLR;
Git reported `fatal: could not open '/dev/null' for reading and writing:
Permission denied`; the direct NUL probe reported `CreateFileW` error 5;
`pwsh` PTY output included a bounded `BCrypt.dll` initialization failure
(`0x8007045A`).  Python produced no user marker in any startup variant.  The
curl row is startup-only; no exact prior restricted-curl command was found in
the current W3 evidence and no replacement network command was run. For every
timeout row, W3 used canonical process termination followed by a bounded
two-second pipe drain, and W4 used canonical terminal-session close followed by
its bounded close/drain; the runner reported exit 0 and the artifact recorded
no forced-runner fallback. This is cleanup evidence only; it does not measure
or claim an `orphan_count` value.

The interpretation separates at least two evidence clusters.  Cluster A is
device/NUL evidence: `CMD_NUL_REDIRECT`, the access-mode-specific
`NUL_DIRECT_WIN32` probe, and Git's `/dev/null` startup failure.  Cluster B is
runtime initialization: Windows PowerShell's `HRESULT 0x80070005`, pwsh's
`BCrypt.dll`/`0x8007045A` output, Python startup timeouts, and curl startup
timeouts.  Node and npm pass through both transports.  The shared W3/W4
pattern is factual, but the relationship between these clusters remains
unproven; Python is not equated with BCrypt, curl is not equated with BCrypt,
and PowerShell is not equated with NUL without new evidence.

## Next decision boundary

The first candidate for a later W5 fix investigation is the shared restricted
startup/device-compatibility seam affecting several W3/W4 workloads.  Its
sub-causes must be separated before any change, and no CNG/Bcrypt, token, SID,
privilege, ACL, Firewall, or fallback change is authorized by Gate 0.  Gate 0
does not implement a fix, and W5 Gate 1 has not started.  Compatibility timeout
rows record canonical termination, runner state, and bounded drain facts; the
matrix does not claim an `orphan_count=0` observation when that field is not
measured.
