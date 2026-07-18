# ADR 0019: fail-closed Linux sandbox profiles

[简体中文](../../zh-CN/adr/0019-fail-closed-linux-sandbox-profiles.md) · **English**

## Context

The fixed historical Rust baseline exposes `off`, `workspace`, `read-only`, and
`strict` profiles. Its observable contract separates model-provider network
access from local child-process network access and applies operating-system
filesystem restrictions to both in-process tools and spawned commands.

Neuro Code already contained workspace path validation and permission prompts,
but neither is an operating-system sandbox. Bash can address absolute paths,
and an approved command can spawn descendants. Calling a policy “workspace”
without a kernel boundary would therefore be a security misrepresentation.
Conversely, silently continuing when a user explicitly requests an unavailable
profile would weaken their request.

## Decision

`SandboxProfile` is a domain value with four canonical names. Resolution order
is CLI `--sandbox`, `NEURO_CODE_SANDBOX`, user configuration, project
configuration, then `off`. A project profile is considered only when user
configuration does not pin one, so an untrusted workspace cannot replace a
user-selected profile with `off`.

Linux is the first enforcing platform adapter:

- `off` preserves the existing unsandboxed behavior.
- `workspace` re-executes Neuro Code under bubblewrap with a read-only host
  root and writable workspace, state, and temporary paths. Local child network
  remains available.
- `read-only` keeps the host root and workspace read-only while retaining
  writable state and temporary paths. The edit tool is omitted from the model
  schema and rejects direct invocation as a second guard.
- `strict` starts from an empty allowlist root, read-binds required system and
  Python runtime paths, write-binds the workspace, state, and temporary paths,
  then remounts the root read-only.
- Bash under `read-only` and `strict` is launched through a nested Linux network
  namespace. The parent agent retains network access for model APIs; the shell
  and all descendants inherit the isolated namespace.

The platform adapter resolves `bwrap` and, when needed, `unshare` only from a
non-workspace, non-caller-writable executable path. It probes required namespace
support before replacement. The child receives an internal profile marker, but
the marker is not trusted alone: startup and every sandboxed Bash launch attest
root/workspace/state mount flags, and `strict` additionally verifies its root is
an allowlist `tmpfs`. Any mismatch, missing helper, unusable namespace, exec
failure, or unsupported platform is terminal.

All current local subprocess creation continues through `ShellSandbox` and the
owned `ProcessTree`. Permission approval remains an independent, earlier gate;
`--always-approve` cannot disable or weaken the selected kernel boundary.
The composition root also passes only protected environment-variable names to
the tool context; Bash removes configured provider credentials and standard or
explicit proxy variables before spawning, so their values cannot become tool
output.

## Consequences

This is intentionally partial M3 support. Linux has enforceable built-in
profiles, while macOS and Windows reject every explicit non-`off` profile until
their adapters exist. Custom profiles, `devbox`, deny globs, Seatbelt,
Landlock-specific optimization, and a Windows sandbox adapter remain pending.
Built-in profile persistence and resume pinning are defined separately by
[ADR 0020](0020-session-fixed-sandbox-profiles.md).

Writable state and temporary paths are deliberate exceptions, matching the
profile contract. `strict` also exposes the active Python runtime read-only so
an installation outside system directories can start. Future tools that spawn
processes must use the same shell/platform port; bypassing it is prohibited.
Commands that intentionally require a provider credential need a future
explicit secret-injection capability rather than ambient inheritance.

The source evidence for the four profile meanings, default `off`, child-only
network restriction, and startup application order is the sandbox profile and
shell startup implementation at historical commit
`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`.
