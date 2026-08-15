# ADR 0115: Windows native sandbox ConPTY vertical slice

- Status: W4 implementation in progress; Gate 1 hardened, Gate 2 PTY write/network isolation, and Gate 3 PTY lifecycle ownership accepted
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

The public enabled-profile `spawn_terminal()` route remains fail closed while
the Gate 1 evidence is collected. The private candidate path is used only by
the focused native acceptance. W3 non-PTY behavior and its capability contract
are unchanged: READ LIMITED, WRITE STRONG, NETWORK STRONG, and strong
descendant ownership. STRICT continues to fail closed because it requires
strong read isolation.

Gate 3 reuses the W3 native descendant probe under the private ConPTY
candidate. In 3A, the direct leader exits with code 23 while its stdio-free
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
the exact singleton synthetic restricting SID and no unexpected enabled
privileges. The private candidate therefore evidences READ LIMITED, WRITE
STRONG, and NETWORK STRONG; STRICT still fails closed because it requires
strong read isolation.

The public enabled-profile `spawn_terminal()` route remains fail closed; Gate 2
acceptance does not expose it. W5 has not started, and Python, Git, Node, NUL,
curl, application terminal routing, and developer-tool compatibility are not
certified by this ADR.

## Consequences

- `PROTOCOL_VERSION` remains 1; `PTY_OUTPUT` is an event frame and `RESIZE` is
  a control frame.
- The runner keeps draining the PTY output channel before publishing `EXIT`.
- `ClosePseudoConsole` is performed only after the Job-owned scope is empty;
  no second lifecycle or Job authority is introduced.
- Gate 1, Gate 2, and Gate 3 evidence are required before exposing a future
  production terminal route. This ADR does not certify that public route yet.

## References

- [Creating a pseudoconsole session](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [CreatePseudoConsole function](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole)
- [CreateProcessAsUserW function](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
