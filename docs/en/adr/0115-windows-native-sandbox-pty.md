# ADR 0115: Windows native sandbox ConPTY vertical slice

- Status: W4 implementation in progress; Gate 1 restricted ConPTY vertical slice
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

The Gate 1 probe is a disposable native C executable. It verifies actual
console dimensions, input, merged PTY output, final output drain, exit code,
restricted-token attestation, and bounded malformed-resize cleanup for both W2
Online and Offline identities. Python, Git, Node, NUL, curl, application
terminal routing, and W5 developer-tool compatibility are outside this ADR.

## Consequences

- `PROTOCOL_VERSION` remains 1; `PTY_OUTPUT` is an event frame and `RESIZE` is
  a control frame.
- The runner keeps draining the PTY output channel before publishing `EXIT`.
- `ClosePseudoConsole` is performed only after the Job-owned scope is empty;
  no second lifecycle or Job authority is introduced.
- Gate 1 evidence is required before exposing a future production terminal
  route. This ADR does not certify that public route yet.

## References

- [Creating a pseudoconsole session](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [CreatePseudoConsole function](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole)
- [CreateProcessAsUserW function](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
