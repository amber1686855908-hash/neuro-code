# ADR 0128: Read-only LSP vertical slice

## Status

Accepted for the `codex/lsp-vertical-slice` stacked implementation. The slice
is intentionally read-only and does not claim worktree, checkpoint, rename,
format, code-action, or workspace-edit support.

## Decision

Neuro Code owns one provider-neutral `lsp` tool and an application-scoped
language-server manager. A configured server is routed by the canonical
workspace root and explicit server profile; there is no process-global LSP
singleton. Profiles are loaded from the existing user/project TOML merge under
`[lsp.servers.<name>]` and contain an argv array. Neuro Code never installs a
server or turns a language name into an executable implicitly.

The manager starts a server lazily through `LocalProcessSandbox` with
`LocalProcessPurpose.LSP_SERVER`, protocol stdio, a read-only workspace policy,
an explicit environment, and bounded process ownership. It implements
Content-Length JSON-RPC framing, `initialize`/`initialized`, correlated
requests, notifications, server-request responses, `$/cancelRequest`, and the
bounded `shutdown`/`exit` close handshake. Malformed, oversized, or truncated
frames fail the current session and never wait without a bound.

The supported model operations are definition, references, hover, document
symbols, workspace symbols, diagnostics, status, and explicit restart. The
manager synchronizes current UTF-8 documents with monotonic versions using
`didOpen`, full-text `didChange`, and `didClose`. It queries the current disk
content and does not treat a watcher or a stale cache as correctness evidence.
Diagnostics use pull when the server advertises it and otherwise consume a
bounded publish cache keyed by URI.

Model-facing positions are one-based line and column in Unicode code points.
The negotiated server encoding is converted at the protocol boundary, with
UTF-16 as the default and UTF-8/UTF-32 supported. Returned URIs are untrusted:
only local file URIs that canonicalize inside the configured workspace roots
and do not traverse link-like components are projected. Cross-file results are
passed through the existing canonical filesystem/permission policy; explicit
deny, outside-root, link-like, invalid, and unresolved ask results are omitted
without opening an approval prompt. Omission counts are bounded.

The server can request configuration, workspace folders, capability
registration, or a message response. `workspace/applyEdit` is answered as not
applied, and unknown requests receive a standard JSON-RPC method-not-found
error. No server result can expand Neuro Code filesystem authority, execute a
command, apply an edit, or expose raw HTML, script, or command URI content.

## Consequences

- The stable tool schema remains available at execution time even when no
  server is configured; the result reports a typed failure instead of silently
  disappearing from the model contract.
- Startup, initialization, request, diagnostics, stderr, pending requests,
  shutdown, result count, symbol depth, and text sizes are bounded.
- A crashed session is reported with a typed LSP error and can be lazily
  restarted a bounded number of times with cooldown. Application composition
  closes every manager before its global background supervisor.
- The real fake LSP server in the test suite exercises framing, Unicode,
  lifecycle, server requests, read-only apply-edit rejection, malformed and
  oversized frames, crash, duplicate/late responses, timeout, and stderr
  pressure paths. A live third-party server is not downloaded or required.

## Not in this ADR

Rename, formatting, code-action execution, `workspace/applyEdit`, arbitrary
server-side file writes, automatic server installation, worktree routing,
checkpoint/rollback, and cross-process detached-descendant ownership are
future work. The existing subagent capability invariant is preserved; LSP is a
parent read-only tool and is not silently added to the fixed child tool list.
