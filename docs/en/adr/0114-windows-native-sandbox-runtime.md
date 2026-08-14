# ADR 0114: Windows native non-PTY sandbox runtime

- Status: W3 implementation under focused native acceptance
- Date: 2026-08-14
- Scope: Windows enabled profiles for BASH, background Bash, and MCP stdio

## Decision

The Windows runtime keeps the controller outside the sandbox boundary.  For
each non-PTY child it starts a trusted, workspace-independent runner with
`CreateProcessWithLogonW` under the selected W2 real account.  The runner opens
its own process token, applies the persisted synthetic write SID through the
W1 `CreateRestrictedToken(WRITE_RESTRICTED)` primitive, and creates the final
child with `CreateProcessAsUserW` inside a kill-on-close Job Object.

Controller and runner communicate through a random controller-owned named
pipe with an exact controller/selected-user DACL and versioned length-prefixed
binary frames.  Stdout and stderr remain separate; protocol payloads are not
decoded as text and runner diagnostics never enter MCP stdout.

`ISOLATED` selects the persistent Offline identity and `INHERIT` selects the
Online identity.  Runtime never changes Firewall state and never performs
setup, repair, or UAC elevation.  A setup inspection that is not `READY`
fails before child creation.

The fully wired W3 runtime exposes a candidate provider contract of read
`LIMITED`, write `STRONG`, and network `STRONG` so privileged native acceptance
can exercise the real boundary.  Native acceptance and the required PR gate
certify that declaration; this is not a CI-dependent runtime bypass.  The W1/W2
foundation actual-capability declaration remains `UNSUPPORTED`.  `STRICT`
requires strong read isolation and therefore fails closed.  Interactive
PTY/ConPTY remains a W4 scope.

## Consequences

- W2 remains the sole authority for accounts, DPAPI state, ACLs, and the
  persistent Offline Firewall rule.
- Job ownership is not duplicated: the runner-owned Job is the descendant
  lifecycle authority for the final child and its descendants.
- The final child receives an explicit environment, including private profile
  and temporary paths derived from the selected sandbox account; controller
  credentials and DPAPI plaintext are never forwarded.
- Native acceptance must execute the final restricted child and prove identity,
  ACL, network, lifecycle, binary stdio, and protocol behavior.
- A failed native acceptance blocks W3 production admission rather than
  changing capability semantics at runtime.
