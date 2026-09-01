# ADR 0133: Bounded parent context relay

- Status: implemented as an explicit internal vertical slice; final rating waits for merge-ref CI
- Date: 2026-08-24
- Scope: one serialized writable worker and one immutable parent-to-child context snapshot

## Context

ADRs 0129-0132 established the managed worktree, READY baseline checkpoint,
writable child lease, fresh child session, and child-scoped read-only LSP
runtime. The child deliberately did not reuse parent context. The next narrow
capability is to hand useful parent conversation evidence to that existing
worker without cloning a transcript, sharing live state, or transferring any
authority.

## Decision

Add a provider-neutral `ParentContextRelay` value and an insert-only persistence
boundary. Production projection is derived only from the durable session bound
to the actual parent `ConversationBinding`; callers cannot nominate a source
session or provide raw relay text.

The relay is bound to the exact parent session/task, child session, writable
lease, `WorktreeId`, baseline `CheckpointId`, base commit, capability and grant
fingerprints, and a digest of the explicit child task. Source and rendered
content have separate deterministic fingerprints, and the complete record has
an integrity fingerprint verified on every load.

### Safe deterministic projection

The first slice scans durable parent `SessionItem` values newest to oldest,
selects at most ten eligible items, and restores chronological order. Only
genuine plain-text USER messages and visible plain-text ASSISTANT messages are
eligible. Assistant visible text may be used when `reasoning_content` is a
separate field; reasoning itself is never projected. A message containing tool
calls or any media part is omitted in full.

System and tool-role messages, synthetic application context, tool arguments
and metadata, tool results, preserved reasoning/backend calls, media data and
URLs, and project/runtime notices are excluded structurally. Eligible text is
processed through the composition-owned configured redaction boundary before
persistence. This is a bounded configured redaction contract, not a claim that
every possible secret shape can be detected.

The byte budgets are:

- 10 selected items;
- 4 KiB UTF-8 text per item;
- 24 KiB total projected text; and
- 32 KiB maximum complete rendered relay.

Truncation preserves valid UTF-8. Selection, rendering, and fingerprints are
deterministic; no model call or second summarization system is used. Existing
durable compaction summaries are not reused in this first slice because their
current-validity proof is separate application work.

### Durable ordering and failure semantics

Schema 17 adds a one-to-one `parent_context_relays` record linked with RESTRICT
foreign keys to the writable lease, parent task, and parent/child sessions. The
READY row is immutable. An exact duplicate may be accepted only after equality
and integrity verification; a mismatch is rejected and no blind UPSERT or
payload update exists.

The writable lifecycle is:

```text
managed worktree READY
  -> baseline checkpoint READY
  -> child session
  -> SubagentLink
  -> safe parent projection
  -> relay inserted READY and reloaded with integrity verification
  -> child runtime creation
  -> lease ACTIVE
  -> first model request
```

Relay publication failure prevents child runtime/model execution and preserves
the existing worktree, checkpoint, lease, child session, and link identities.
Provider failure, tool failure, cancellation, timeout, and process death after
publication preserve the immutable relay as audit evidence. Reconciliation
does not rerun the child or regenerate the relay.

### Model context, not authority

`ContextBuilder` injects exactly one plain-text synthetic USER message tagged
`SyntheticReason.PARENT_RELAY` on every child model request. Its stable order is
system, project instructions, available skills, parent relay, then genuine
child task/history. The synthetic relay is never persisted as a genuine child
`SessionItem`, and it remains byte-stable across child model/tool/LSP steps.

Relay text cannot alter tool names, workspace roots, sandbox profile, network,
LSP, worktree, or checkpoint authority. Paths and commands mentioned by the
parent remain unparsed text. The existing authority intersection and child-root
instruction/skill discovery remain unchanged.

## Not implemented

Raw transcript reuse, hidden reasoning transfer, tool-output transfer,
compaction-summary reuse, live parent-context streaming, long-term memory,
shared conversation state, unbounded or shared parallel workers, task DAGs beyond
the bounded worker slice, Leader/Swarm/Ultracode
orchestration, automatic delegation, Bash/process workers, richer child result
relay, merge/integration, and automatic cleanup remain outside this slice.

## Validation boundary

Acceptance requires safe-structure exclusion and configured-secret tests,
byte-bound and multibyte truncation tests, deterministic snapshot/fingerprint
tests, insert-only and tamper rejection, populated schema-16 migration,
READY-before-model ordering, no transcript persistence, stable multi-step model
requests, unchanged worker LSP authority, failure/cancel/timeout preservation,
and a real process-death case after Relay publication but before the first model
request. Final proof requires full local gates and the stacked PR merge-ref
Linux/macOS/Windows/package matrix.
