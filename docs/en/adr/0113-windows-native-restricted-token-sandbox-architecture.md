# ADR 0113: Windows native restricted-token sandbox architecture

## Status

Accepted as the W1 foundation and W2 setup-authority record. This ADR
establishes typed capability, restricted-token, installation setup, and
filesystem/firewall authority primitives; it does not connect them to runtime
child creation or claim a complete Windows sandbox.

## Context

ADR 0112 records why the classic stable unpackaged AppContainer route is not a
production adapter for the current stock Git for Windows workflow. Its
evidence remains historical and is not replaced by a silent fallback.

The next production direction must preserve the existing child-scoped process
boundary while making each security authority explicit. Filesystem and network
security authority are one contract; process lifecycle ownership is a separate,
orthogonal `LocalProcessLifecycleCapability` contract.

## Decision

W1 adds the platform-neutral `LocalProcessSecurityCapabilities` model in the
canonical local-process port. It has these dimensions:

- `READ_ISOLATION`
- `WRITE_ISOLATION`
- `NETWORK_ISOLATION`

Each dimension reports `STRONG`, `LIMITED`, or `UNSUPPORTED`.
`security_capability_satisfies()` compares the three axes explicitly: strong
satisfies strong or limited, limited satisfies limited only, and unsupported
satisfies only unsupported/no requirement. A caller that needs strong read
isolation must fail closed before creating an OS child when the provider is
limited or unsupported.

W1 establishes primitives and target architecture, but advertises no completed
Windows filesystem/network capability. Enabled Windows profiles remain fail
closed, so the actual W1 capability declaration is:

| Dimension | W1 actual provided |
| --- | --- |
| Read isolation | `UNSUPPORTED` |
| Write isolation | `UNSUPPORTED` |
| Network isolation | `UNSUPPORTED` |

The separate native-backend architecture target is:

| Dimension | Target | Reason |
| --- | --- | --- |
| Read isolation | `LIMITED` | Developer-tool compatibility remains the read boundary under review. |
| Write isolation | `STRONG` target | A later setup layer will grant only an explicit restricted SID. |
| Network isolation | `STRONG` target | Strong enforcement belongs to W2 firewall policy. |

Descendant ownership is not a security axis here. Existing Job Object and
ConPTY paths continue to provide `STRONG_DESCENDANT_OWNERSHIP` through the
independent lifecycle contract.

The production W1 token layer provides:

- validated in-memory synthetic `S-1-5-21-<u32>-<u32>-<u32>-<u32>` SIDs;
- a typed restricted-SID request with `WRITE_RESTRICTED`,
  `DISABLE_MAX_PRIVILEGE`, and `LUA_TOKEN` flags;
- lazy Win32 `OpenProcessToken`, `CreateRestrictedToken`,
  `GetTokenInformation`, and `CloseHandle` calls behind an injectable API;
- restricted-token attestation without exposing token contents; and
- fail-closed Win32 errors and deterministic source/created-handle cleanup.

W1 does not advertise completed filesystem/network authority. It does not
provision users, persist identities, mutate ACLs, use DPAPI,
configure a firewall, launch a command-runner binary, or add a Git/Python/MCP
broker. It does not rewrite Job Object or ConPTY code. Existing Linux
Bubblewrap, macOS Seatbelt, and Windows Job Object/ConPTY guarantees remain
unchanged. Windows enabled profiles remain unsupported and fail closed until a
later vertical slice wires a complete authority composition.

## W2 setup authority

W2 implements an installation-time setup boundary while leaving runtime child
creation for W3. The authority has these properties:

- Offline and Online are dedicated real local users (`NeuroSandboxOffline` and
  `NeuroSandboxOnline`). Their resolved account SIDs are stable setup facts and
  are distinct from the installation-scoped synthetic restricting SID.
- The synthetic SID is used only as the W1 `WRITE_RESTRICTED` token membership
  and write-only ACL principal. It is never a read principal and never a
  firewall identity; real account SIDs carry the read and primary-user write
  checks.
- The installation record uses schema version 3 and stores each actual account
  password inside a DPAPI machine-scoped encrypted payload. Because machine
  DPAPI alone is not a user boundary, the credential file also receives exact
  NTFS deny ACEs for both sandbox users; controller/setup access is preserved.
- Filesystem setup plans read allows for both real users, explicit primary-user
  write allows for both real users plus a write-only synthetic restricting SID
  on writable roots,
  explicit write denies on read-only roots, and sensitive-read denies for both
  real users. Native reconciliation uses `SetEntriesInAclW`, which canonicalizes
  explicit denies before allows while preserving unrelated controller ACEs and
  owner information. Re-running setup is idempotent, drift is `NEEDS_REPAIR`,
  and cleanup removes only exact managed tuples and installation-created users.
- Offline owns one persistent outbound block rule scoped to the real Offline account SID.
  Readiness verifies the complete managed tuple: outbound direction, block
  action, enabled state, expected profile, and the exact Offline SID. Any
  drift is `NEEDS_REPAIR`; the rule remains installed while either dedicated
  identity is used and is removed only by explicit cleanup. Online identity
  use therefore cannot change Offline network authority, and neither the
  Online account nor the controller user is matched by the Offline rule.
- The installation root is a private controller/setup root and must be
  disjoint from every sandbox read or writable root. Its inherited NTFS deny
  authority protects the DPAPI envelope and future state files from read,
  write, delete, and replace operations by both sandbox users. The persisted
  credential record remains the recovery source until ACL, firewall, and
  installation-created account rollback has completed.
- Local-account group validation uses well-known built-in group SIDs (with
  account-name lookup only as a locale-specific transport detail), so a
  translated `Users` or privileged-group display name cannot change the
  security decision.
- Setup, repair, and cleanup are an explicit administrative boundary. An
  ordinary session can inspect the state and run later runtime work without
  continuing to require administrator privileges.
- State is reported as `READY`, `NEEDS_SETUP`, `NEEDS_REPAIR`, or
  `UNSUPPORTED`. Setup success does not change
  `WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES`: all three runtime security
  axes remain `UNSUPPORTED` until W3 wires the child boundary.

W2 does not launch a command runner, create a runtime child, bridge MCP, alter
Git/Python integration, rewrite ConPTY or Job Object code, use AppContainer or
WSL2, or configure a firewall rule for the controller user. It reuses the W1
capability contract and existing Job/ConPTY lifecycle boundary and does not
reinterpret `LIMITED` read planning as a runtime capability.

## Consequences

The capability contract prevents a target declaration from being consumed as
an actual provider capability and keeps security authority orthogonal to
lifecycle ownership. The token foundation is useful independently of a future
filesystem setup layer, while the current Windows profile behavior continues
to fail closed. ADR 0112 remains the historical AppContainer feasibility
record.

## References

- [ADR 0112](0112-windows-appcontainer-sandbox-feasibility-decision.md)
- [Cross-platform lifecycle capability contract](0110-cross-platform-lifecycle-capability-contract.md)
- [Building Codex for Windows](https://openai.com/index/building-codex-windows-sandbox/)
- [Codex Windows sandbox setup reference](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/setup.rs)
