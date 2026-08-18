# ADR 0115: Windows native sandbox ConPTY vertical slice

- Status: Accepted; W4 production Windows ConPTY routing certified by focused native acceptance and full CI
- Date: 2026-08-15

## Context

The Windows W3 runtime already owns the final restricted child through the W2
identities, exact synthetic write SID, private HOME/TMP, private desktop, and
runner-owned Job Object. The ordinary `WindowsConPtyPlatform` is intentionally
not a sandbox authority: it uses `CreateProcessW` and is therefore unsuitable
for an enabled Windows profile.

## Decision

Gate 1 composes the existing trusted W3 runner with the Windows ConPTY
primitives. The runner creates the input/output channels and an HPCON, then
uses one `STARTUPINFOEXW` attribute list containing both
`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` and `PROC_THREAD_ATTRIBUTE_JOB_LIST` for
the final `CreateProcessAsUserW` call. PTY output is one raw byte stream and
resize is a directional control frame. The child does not inherit controller,
runner, or protocol handles; ConPTY supplies its console streams.

The enabled-profile `spawn_terminal()` route is now the canonical production
boundary and is the same route exercised by focused native acceptance. W3
non-PTY behavior and its capability contract
are unchanged: READ LIMITED, WRITE STRONG, NETWORK STRONG, and strong
descendant ownership. STRICT continues to fail closed because it requires
strong read isolation.

Gate 3 reuses the W3 native descendant probe through the production ConPTY
route. In 3A, the direct leader exits with code 23 while its stdio-free
grandchild remains active; `poll_exit()` stays pending until the grandchild
naturally writes its finished marker and the runner-owned Job becomes empty.
In 3B, `session.close()` sends the canonical TERMINATE frame and the runner's
Job terminates both live descendants without controller-side runner fallback;
the bounded termination observation records the final child as active at the
request point. In 3C, abrupt controller-helper loss causes the runner to
fail closed and terminate the complete Job scope. In 3D, killing the trusted
runner proves KILL_ON_JOB_CLOSE: both descendants exit without a natural
completion marker, and the controller receives one bounded error rather than
a fabricated clean EOF. All four scenarios preserve bounded HPCON/relay
teardown and report no orphan processes.

Controller-loss classification is state-based. The runner treats control EOF
as harmless only after the final `EXIT` was sent or after
`owned_scope_quiesced` was established by direct-child exit plus
`Job ActiveProcesses == 0`. EOF while the owned Job is active calls
`fail_closed()` immediately; no time-based EXIT grace is used for this
security decision.

The Gate 1 probe is a disposable native C executable. It verifies actual
console dimensions, input, merged PTY output, final output drain, exit code,
restricted-token attestation, and bounded malformed-resize cleanup for both W2
Online and Offline identities. Its hardened evidence also records natural
runner exit (runner exit code 0, no forced termination) and the documented
ConPTY standard-handle contract: `bInheritHandles=false`, no handle-list
attribute, and valid console input/output handles without claiming unsupported
handle enumeration.

Gate 2 re-certifies the W3 capability boundary through the restricted PTY
child. Workspace create/append/rename/delete succeeds; an outside directory
with ordinary-user and Everyone write ACEs but no synthetic write SID remains
denied; read-only and installation/controller state mutations remain denied.
Online Winsock connects, Offline Winsock is denied with `WSAEACCES` (10013),
and the managed Offline firewall rule is inspected as READY before and after
each run without runtime mutation. Every PTY SpawnReady attestation contains
the exact ordered production restricting-SID set (synthetic capability SID,
sandbox-user SID, runner logon SID, and World) and no unexpected enabled
privileges. The certified production route therefore evidences READ LIMITED, WRITE
STRONG, and NETWORK STRONG; STRICT still fails closed because it requires
strong read isolation.

Gate 4 proves the real application route: the terminal manager builds the
normal `SandboxedProcessRequest` and calls the public
`LocalProcessSandbox.spawn_terminal()` port. For an enabled Windows profile,
the production chain is:

`LocalInteractiveTerminalManager` → `LocalProcessSandbox.spawn_terminal()` →
`WindowsNativeLocalProcessSandbox` → W2 identity → trusted runner → restricted
token → `CreatePseudoConsole` → `PSEUDOCONSOLE` + `JOB_LIST` →
`CreateProcessAsUserW` → restricted final child.

WORKSPACE and READ_ONLY are admitted only when W2 reports `READY`; STRICT
continues to fail closed because READ LIMITED cannot satisfy its STRONG read
requirement. Runtime never performs setup, repair, UAC, ACL, or Firewall
mutation. SandboxProfile.OFF remains on the ordinary Windows ConPTY route.

The W5 workload matrix is now accepted as bounded compatibility evidence. Run
`32193614626` records 20 HOST/W3/W4 rows passing, including Python and child
Python, Git repository operations, Node/npm, curl, NUL access modes, and a
dynamic BCrypt probe. This evidence does not add a second PTY authority or
weaken the W4 token, ConPTY, or Job contracts; future tools still require their
own fixtures.

## Consequences

- `PROTOCOL_VERSION` remains 1; `PTY_OUTPUT` is an event frame and `RESIZE` is
  a control frame.
- The runner keeps draining the PTY output channel before publishing `EXIT`.
- `ClosePseudoConsole` is performed only after the Job-owned scope is empty;
  no second lifecycle or Job authority is introduced.
- Gate 1, Gate 2, Gate 3, shared-runner hardening, Gate 4 application routing,
  and the accepted W5 workload matrix are the acceptance evidence for this
  production route. The route is certified on the tested Windows CI matrix;
  future developer tools remain explicitly bounded by their own evidence rows.

## References

- [Creating a pseudoconsole session](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [CreatePseudoConsole function](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole)
- [CreateProcessAsUserW function](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
