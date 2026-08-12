# ADR 0111: macOS Seatbelt local-process sandbox

## Status

Accepted. Enabled built-in sandbox profiles on macOS use a production
`MacOSSeatbeltLocalProcessSandbox`. This decision does not change Linux
Bubblewrap or Windows Job Object guarantees.

## Context

The macOS evidence workload proved that deny-by-default Seatbelt profiles can
enforce child filesystem, network, and environment authority on both GitHub
macOS runner architectures. The same evidence proved that a POSIX process
group does not own a descendant that successfully calls `setsid()`. Filesystem
and network enforcement therefore must not be presented as strong descendant
lifecycle ownership.

## Decision

Composition selects the Seatbelt adapter for `workspace`, `read-only`, and
`strict` on Darwin. It requires the fixed root-owned
`/usr/bin/sandbox-exec`, a canonical workspace and controller state directory,
and non-overlapping authorized roots. Unsupported platforms and invalid
authority fail closed.

Each request is converted to a deny-by-default SBPL profile using escaped
string literals. Runtime reads are limited to the evidence-backed macOS system
roots, the controller interpreter runtime, explicit workspace roots, and one
child-private HOME/TMP pair. Controller state, other host-home data, and
outside writes remain denied. `workspace` permits outbound networking;
`read-only` and `strict` omit that authority. `read-only` grants no workspace
writes, while `strict` preserves the requested per-root modes and the main
workspace remains writable.

Authorized runtime, workspace, and private roots receive recursive metadata
authority for their own subtrees. Their ancestors receive exact `literal`
metadata authority only for pathname traversal, including exact `/`; no
ancestor, including `/`, receives recursive `subpath` metadata authority.

Before either pipe or PTY creation, `PosixWorkspaceInodeAudit` checks every
authorized root and rejects external hardlink aliases or mixed read-only and
read-write aliases. Child environments are reconstructed only from
`LocalProcessEnvironmentPolicy`: there is no implicit `os.environ` merge.
HOME, TMPDIR, TMP, and TEMP point to independent private directories owned by
the returned process/session wrapper and removed after wait, termination,
spawn failure, or PTY close.

Bash, background Bash, MCP stdio, and interactive PTY requests all launch
`/usr/bin/sandbox-exec` as the outer executable. Shell requests use the trusted
`/bin/sh -c`; MCP remains argv-safe. POSIX pipe creation explicitly uses
`close_fds=True`, with `pass_fds` reserved for explicit infrastructure
descriptors. The existing PTY path retains the same close-FD invariant.

## Lifecycle contract

The adapter and every process or terminal it returns always report
`PROCESS_GROUP_BEST_EFFORT`. A request requiring
`STRONG_DESCENDANT_OWNERSHIP` fails before OS child creation. `setsid()` is not
described as owned, even though Seatbelt filesystem and network policy remains
enforced for a detached descendant.

Endpoint Security, a System Extension, and a privileged helper remain outside
this implementation. They are candidates only for a future hardened macOS
architecture and are not claimed as current lifecycle solutions.

## Verification boundary

Dedicated CI executes the real adapter, without skips, on `macos-15` ARM64 and
`macos-15-intel`, using Python 3.12 and 3.14. It records the macOS version/build,
architecture, fixed sandbox executable, signature details, and SIP status.
The production implementation is complete, and GitHub ARM64/Intel integration
is validated. GitHub-hosted evidence currently reports SIP disabled. Physical
SIP-enabled macOS acceptance is `DEFERRED_NO_MAC_HARDWARE` because no Mac
hardware is currently available; this is not a code or merge blocker, and it
does not claim that physical SIP-enabled acceptance has passed.
