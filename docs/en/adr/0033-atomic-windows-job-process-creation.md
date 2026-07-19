# ADR 0033: atomically create Windows processes inside their owned Job

[简体中文](../../zh-CN/adr/0033-atomic-windows-job-process-creation.md) · **English**

- Status: accepted
- Date: 2026-07-19
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

ADR 0031 introduced kill-on-close Job ownership for foreground and managed
background commands. Its first implementation used the standard asyncio
subprocess API and assigned the returned PID to the preconfigured Job. That left
a narrow interval in which an adversarial or simply very fast leader could
create a descendant before assignment.

Python's public `subprocess.STARTUPINFO.lpAttributeList` supports a restricted
inherited-handle list, but it does not expose the Job-list process attribute.
Using a private asyncio transport, launching suspended and repairing ownership
afterward, or requesting breakaway would either remain racy or weaken an
explicit containment boundary.

## Decision

A `windows_job_process` platform adapter uses only standard-library `ctypes` and
loads Win32 functions lazily. `ProcessTree` creates and configures its
kill-on-close Job first, then lends the still-owned handle to one
`STARTUPINFOEXW` creation call. The attribute list contains both:

- `PROC_THREAD_ATTRIBUTE_JOB_LIST`, pointing to the Job that must own the child
  from creation; and
- `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, containing only the inheritable null
  input and selected stdout/stderr pipe writers.

`CreateProcessW` receives a mutable command line, explicit Unicode environment
block and working directory, `STARTF_USESTDHANDLES`,
`EXTENDED_STARTUPINFO_PRESENT`, `CREATE_UNICODE_ENVIRONMENT`,
`CREATE_NEW_PROCESS_GROUP`, and `CREATE_NO_WINDOW`. `bInheritHandles` is true
only because the explicit handle list constrains inheritance. Parent pipe
readers are made non-inheritable before process creation; the Job handle is not
inherited.

The Win32 process and pipes are synchronous. Dedicated reader threads feed
public `asyncio.StreamReader` instances, and a dedicated waiter thread records
the direct child's exit status. This preserves the existing foreground Bash and
managed-background process contract without depending on asyncio's private
Windows subprocess transport. `ProcessTree.wait()` then continues observing the
Job's active-process count, while termination still targets the complete Job.

Pipe creation, null-input opening, attribute-list initialization and updates,
process creation, stream reads, waiting, termination, and every owned handle
have explicit cleanup. Failure before creation closes all pipes and the Job;
failure after creation terminates and reaps the child before releasing its
process handle. Aliased merged-output handles are closed exactly once.

Portable tests validate the fixed-width structures, exact Job-list and
handle-list attributes, restricted inheritance, creation flags, Unicode
environment ordering, shell command boundary, stream projection, non-zero
status, and every major failure path. Existing native Windows process-tree and
background-shutdown tests now execute through this atomic launcher.

## Consequences

- No leader instruction can run before Windows Job ownership applies, so the
  prior spawn-to-attach descendant escape window is removed.
- Unsupported extended attributes, incompatible nested-Job policy, or invalid
  standard-handle inheritance fail command creation visibly; there is no
  `taskkill`, breakaway, or post-launch fallback.
- The adapter adds no runtime dependency and remains safe to import on
  non-Windows platforms. Native behavior still requires Windows CI evidence.
- Non-PTY shell streams and ConPTY terminal sessions retain separate lifecycle
  owners. ADR 0034 now reuses the Job-list creation rule for production ConPTY
  and projects it through a shared terminal port; ACP protocol exposure remains
  a separate capability.

The Job-list behavior follows Microsoft's
[UpdateProcThreadAttribute documentation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute),
and restricted inheritance follows the
[CreateProcessW guidance](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw).
Historical ownership evidence comes from the Job Object `ProcessGroup` and
local-terminal paths in the read-only pinned Rust baseline.
