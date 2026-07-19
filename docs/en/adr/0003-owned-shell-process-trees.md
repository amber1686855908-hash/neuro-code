# ADR 0003: Own shell process trees and fail closed during command classification

[简体中文](../../zh-CN/adr/0003-owned-shell-process-trees.md) · **English**

- Status: accepted
- Date: 2026-07-17
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

Killing only the immediate shell can orphan compilers, package managers, test
runners, or their grandchildren. Checking a permission rule only against the
original command string also permits a denied command to hide after a shell
operator, a wrapper such as `timeout`/`env`, or an inner `bash -c` script.
Unbounded `communicate()` calls can exhaust memory before output truncation is
applied.

The pinned Rust implementation creates an owned process group, uses a bounded
TERM-to-KILL sequence, and evaluates restrictive Bash rules against each
decomposed command form.

## Decision

`ProcessTree` is the platform adapter for shell lifecycle ownership. POSIX
children start in a new session and the adapter signals the whole process
group. Windows children start in a new process group and are atomically assigned
to an anonymous kill-on-close Job Object by the same `CreateProcessW` call;
waiting and termination use that stable handle rather than later PID-based tree
discovery. The Bash tool drains stdout and stderr concurrently while retaining
at most the configured byte limit per stream.
[ADR 0031](0031-fail-closed-windows-job-objects.md) and
[ADR 0033](0033-atomic-windows-job-process-creation.md) refine the Windows
ownership, creation, and failure semantics.

The permission layer uses a conservative shell lexer for simple sequences. It
checks all `&&`, `||`, `;`, and pipe segments, both wrapped and unwrapped
command forms, and recursively analyzes common shell `-c` invocations to a
depth limit. Sensitive environment overrides and constructs not safely
classified by this lexer are marked incomplete. If a Bash deny/ask rule could
apply, incomplete classification becomes `ask` interactively and denial in
headless mode.

## Consequences

- Timeout and cancellation do not intentionally leave same-group descendants.
- A later or nested denied command cannot hide behind an allowed prefix.
- Output retention is bounded independently of command output volume.
- Some valid complex Bash scripts are conservatively denied under restrictive
  policies until a full parser and shell file-access model are added.
- Windows Job Object ownership starts atomically at process creation and covers
  all descendants. Native ConPTY lifecycle evidence is covered by ADR 0032;
  interactive ACP PTY ownership remains a separate M4 capability.
