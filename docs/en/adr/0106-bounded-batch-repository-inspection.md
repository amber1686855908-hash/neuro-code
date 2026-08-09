# ADR 0106: Bounded batch repository inspection

## Status

Accepted.

## Context

The compatible `read_file`, `list_dir`, and `grep` tools are safe but make a
large repository analysis alternate one model request with each small evidence
request. Parallelizing arbitrary tool calls would disturb approval ordering,
tool-result pairing, supervision reservations, workspace snapshots,
cancellation, and event ordering.

## Decision

Add three read-only infrastructure tools without changing the existing tools:

- `read_files` reads up to 16 explicit files in request order, applies bounded
  line ranges, isolates per-file errors, supports the session-scoped ACP text
  reader, and bounds the combined redacted output;
- `list_tree` walks a deterministic bounded depth and entry count, skips links
  plus common metadata, dependency, cache, and build directories, and never
  escapes the existing workspace resolver;
- `grep_many` scans a deterministic bounded Python-filesystem traversal for up
  to 16 regular expressions with optional include/exclude globs and separate
  per-query, total-result, scanned-file, and output-byte limits.

The canonical registry exposes all three in ordinary read-only bindings and in
the explicit isolated read-only subagent capability set. Instruction and skill
trackers keep their existing single-moving-target semantics: successful batch
items update them in deterministic order, so the last successful target is
used by the next model request.

Tool execution in `AgentLoopRunner` remains sequential. The optimization is a
coarser tool contract, not runtime concurrency.

## Consequences

Large repository mapping, search, and evidence reads need fewer model turns
without weakening permission, sandbox, cancellation, or message-pairing
boundaries. ACP can batch explicit reads because its filesystem port supports
text reads; it cannot delegate tree traversal or repository search because the
protocol exposes no corresponding capability.
