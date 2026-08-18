# ADR 0114: Windows native non-PTY sandbox runtime

- Status: Accepted; W3 merged after focused native acceptance and full CI
- Date: 2026-08-14
- Scope: Windows enabled profiles for BASH, background Bash, and MCP stdio

## Decision

The Windows runtime keeps the controller outside the sandbox boundary.  For
each non-PTY child it starts a trusted, workspace-independent runner with
`CreateProcessWithLogonW` under the selected W2 real account.  The runner opens
its own process token, applies the persisted synthetic write SID through the
W1 `CreateRestrictedToken(WRITE_RESTRICTED)` primitive, and creates the final
child with `CreateProcessAsUserW` inside a kill-on-close Job Object.

The final token's restricting SID set contains exactly the installation
synthetic write SID. Everyone, logon, sandbox-user, and controller SIDs remain
object-ACL principals only. `DISABLE_MAX_PRIVILEGE` must preserve
`SeChangeNotifyPrivilege`; W3 inspects that fact and never re-grants it with
`AdjustTokenPrivileges`.

Controller and runner communicate through two random controller-owned,
directional synchronous named pipes: a controller-writer/runner-reader
control pipe and a runner-writer/controller-reader event pipe. Each pipe has
an exact controller/selected-user DACL, specific client rights that exclude
`FILE_CREATE_PIPE_INSTANCE`, and versioned length-prefixed binary frames.
Stdout and stderr remain separate; protocol payloads are not decoded as text
and runner diagnostics never enter MCP stdout. The runner is launched with
Python `-I` and an explicit environment, but those measures are not a
provenance proof by themselves: before `CreateProcessWithLogonW`, the
resolved interpreter, runner module, Neuro Code package root, and dependency
root must be disjoint from every model-writable root.

`ISOLATED` selects the persistent Offline identity and `INHERIT` selects the
Online identity.  Runtime never changes Firewall state and never performs
setup, repair, or UAC elevation.  A setup inspection that is not `READY`
fails before child creation.

The fully wired W3 runtime exposes the concrete provider contract of read
`LIMITED`, write `STRONG`, and network `STRONG`.  Focused native acceptance has
certified that declaration; this is not a CI-dependent runtime bypass.  The
W1/W2 foundation actual-capability declaration remains `UNSUPPORTED`, and the
architecture target is not used for runtime admission.  `STRICT` requires
strong read isolation and therefore fails closed.  Interactive PTY/ConPTY
remains a W4 scope.

Gate 1 does not depend on Python startup.  The basic Win32 child is attested
from the actual process handle returned by `CreateProcessAsUserW`, before
`SpawnReady`; the controller checks `TokenUser`, `IsTokenRestricted`, the
production restricting-SID set, `SeChangeNotifyPrivilege`, and the absence of
unexpected enabled privileges.  The W5 compatibility matrix then validated
the current venv and base Python, child Python, PowerShell, Git, Node/npm, curl,
NUL, and dynamic BCrypt startup through both W3 and W4 without changing those
security boundaries.

## Consequences

- W2 remains the sole authority for accounts, DPAPI state, ACLs, and the
  persistent Offline Firewall rule.
- Job ownership is not duplicated: the runner-owned Job is the descendant
  lifecycle authority for the final child and its descendants.
- The final child receives an explicit environment, including private profile
  and temporary paths derived from the selected sandbox account; controller
  credentials and DPAPI plaintext are never forwarded.
- Native acceptance must execute the final restricted child and prove identity,
  exact restricted SIDs, preserved traversal privilege, authorized and
  broad-primary-user-only write behavior, ACL, network, lifecycle, binary
  stdio, and protocol behavior.
- A failed native acceptance blocks W3 production admission rather than
  changing capability semantics at runtime.

## Focused native acceptance evidence

The current W3 provider completed the focused Windows runtime acceptance:
seven tests executed, zero skipped. The evidence covers:

- Gate 1: Online and Offline final-child identity/token attestation before
  `SpawnReady`.
- Gate 2: workspace allow, read-only and sensitive-read denial, installation
  state denial, and the adversarial broad-primary-user/Everyone-write fixture
  without the synthetic SID, which remains denied.
- Gate 3: repeated and concurrent Online Winsock connections, Offline
  `WSAEACCES` denial, controller pre/postflight, and an unchanged exact
  persistent Firewall rule.
- Gate 4: binary stdout/stderr capture, merged ordering, protocol framing,
  EOF, non-zero exit preservation, and no output after `Exit`.
- Gate 5A: a stdio-free descendant keeps normal wait pending and finishes
  naturally; Gate 5B: public `terminate()` ends the whole Job scope; Gate 5C:
  controller loss closes the scope fail-closed; Gate 5D: runner death proves
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.

The accepted W5 workload artifact (run `32192058214`) records 20 HOST/W3/W4
rows with PASS results, including Python child processes, Git repository
operations, NUL read/write modes, curl startup, and dynamic `BCryptGenRandom`.
This is bounded local workload evidence; future tools and network scenarios
still require their own fixtures. Full CI and focused native acceptance continue
to be the merge-readiness evidence for the production runtime.
