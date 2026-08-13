# ADR 0113: Windows native restricted-token sandbox architecture

## Status

Accepted as the W1 foundation. This ADR establishes typed capability and
restricted-token primitives; it does not enable Windows filesystem/network
profiles or claim a complete Windows sandbox.

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

## W2 boundary

W2 may add the explicit setup authority needed for write ACL composition and
strong network enforcement, with its own production evidence and CI. It must
reuse the W1 capability contract and existing Job/ConPTY process boundary. It
must not reinterpret `LIMITED` read isolation as strong, and it must not use an
unsandboxed broker to bypass a missing child authority.

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
