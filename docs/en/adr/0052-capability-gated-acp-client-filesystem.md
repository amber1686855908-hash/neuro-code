# ADR 0052: Capability-gated ACP client filesystem

[简体中文](../../zh-CN/adr/0052-capability-gated-acp-client-filesystem.md) · **English**

- Status: accepted
- Date: 2026-07-29

## Context

ACP defines client-owned `fs/read_text_file` and `fs/write_text_file` requests.
An ACP agent must not assume that the connected client has a filesystem, nor
should an advertised remote filesystem silently fall back to the agent process's
local files. Neuro Code already has workspace confinement, explicit sandbox
profiles, permission decisions, write-time `AGENTS.md` discovery, and exact
replace semantics in its normal file tools.

The protocol provides text reads and whole-file writes only. It has no atomic
compare-and-swap, directory enumeration, recursive search, or delete operation.

## Decision

- Add the canonical `ClientFileSystem` application port. It is session-scoped,
  capability-aware, and has only text read/write operations.
- The ACP inbound adapter creates the port only after the client advertises at
  least one filesystem capability, and binds it while creating, loading,
  resuming, or forking that session. Bootstrap passes the port through the
  composition root; application code never imports ACP SDK types.
- `read_file` delegates to `fs/read_text_file` when that port is bound. Its
  path receives only lexical session-root validation; the host must not resolve,
  inspect, or substitute a local path for a client-owned target. Its line range
  retains existing bounds. A read capability is required; no local fallback is
  used for that delegated operation.
- `search_replace` is registered for a delegated client only when both read and
  write are advertised. It retains session-root, sandbox, additional-directory,
  instruction preflight, exact-match, ambiguity, and ordinary permission gates,
  then reads and writes through the port. The client owns the final write's
  atomicity, link semantics, and filesystem identity, which the protocol cannot
  represent on the host.
- Bound each client text response and write request to 1 MiB. Convert client
  exceptions and malformed text to stable fail-closed `ToolError` messages;
  do not echo client errors or credentials.
- Keep terminal operations separate from this filesystem slice. ADR 0053 later
  adds only a capability-gated foreground executable; the existing local
  directory/search/command tools are not redefined by the ACP text filesystem
  methods.

## Consequences

- Clients that advertise text filesystem access can receive tool reads and
  exact replacements without Neuro Code directly using those delegated file
  contents for the operation.
- Read-only clients cannot receive the mutation tool, and absent client
  capability leaves ordinary local tool behavior unchanged.
- The capability remains deliberately narrow. Client terminal framing,
  filesystem enumeration/search/delete, multimedia, WebSocket, and extensions
  remain separate protocol slices.
