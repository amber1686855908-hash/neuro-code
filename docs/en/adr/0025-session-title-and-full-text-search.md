# ADR 0025: Session title and full-text search

[简体中文](../../zh-CN/adr/0025-session-title-and-full-text-search.md) · **English**

- Status: accepted
- Date: 2026-07-18
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

Recent-session listing is insufficient once one state database contains many
conversations. Users need to find a session by its title, prompts, replies, or
tool activity without loading every JSON conversation into the UI. Search must
remain workspace-safe in the TUI and must not turn provider-private reasoning,
encrypted native context, image URLs, or system instructions into visible
snippets.

## Decision

Extend the typed `SessionStore` port with `search_sessions` and return a
`SessionSearchPage` containing typed `SessionSearchHit` values. A hit combines
the canonical `SessionSummary` with a finite score, matched fields, and an
optional bounded FTS snippet.

SQLite schema v4 adds a stable optional title to session summaries plus an
external-content FTS5 table synchronized through insert, update, and delete
triggers. The v3-to-v4 migration backfills titles and search documents from the
existing ordered session items in the same database transaction. Missing
documents are repaired during initialization, and every message/item save or
read-only Rust import updates the index in the same transaction as canonical
session state.

Initialization acquires a SQLite immediate write transaction before inspecting
or changing the schema. Concurrent initializers therefore serialize across
store instances and processes, while a migration or FTS backfill failure rolls
back the schema, version marker, and derived documents together. Enabling WAL
retries only transient database-lock errors up to the connection timeout; other
SQLite failures remain fail-closed.

Native sessions receive a deterministic title from the first visible user
message: system-reminder blocks are removed and the first ten words are kept.
The title remains stable on later turns. A valid upstream `generated_title` is
preserved during read-only import; no auxiliary model request is required for
this slice.

`SessionStore.update_session_title` implements unconditional manual rename.
It rejects blank input, normalizes whitespace, applies the 200-character domain
bound, and updates the canonical title, update timestamp, and synchronized FTS
document in one SQLite transaction. A failed index write therefore rolls back
the summary change. Later message saves preserve every non-empty title, so a
manual rename cannot be replaced by deterministic fallback generation.

The searchable projection includes visible user/assistant message text and tool
names. It deliberately excludes system messages, raw tool-result content, tool
arguments/metadata, `PreservedContextItem` payloads, assistant
`reasoning_content`, and image URLs. Search uses sanitized Unicode-aware prefix tokens, requires all
tokens first, and retries with an OR query only when the intersection has no
matches. BM25 weights title matches ten times more strongly than content. Exact
cwd filtering, offsets, totals, and optional snippets are part of the adapter
contract.

Expose search as `neuro-code sessions search QUERY` for scripts and as
`/sessions QUERY` for the TUI. Expose manual rename as
`neuro-code sessions rename SESSION_ID TITLE` and current-session
`/rename TITLE`, with `/title` as an alias. The TUI applies filesystem-identity
workspace filtering to search and rename, and serializes rename with active
turns. It renders saved titles, user queries, and snippets as literal `Text`,
never as Textual markup. The controller retains the validated summary behind a
search result so an older hit need not also appear in the recent-session page;
selecting it recomputes current profile and sandbox availability before the
ordinary open-time workspace validation.

JSON session exports move to schema version 4 and include the optional title.

## Consequences

- Search is incremental and ranked instead of repeatedly scanning and parsing
  every `messages_json` value.
- A Python/SQLite build without FTS5 fails initialization explicitly rather
  than silently claiming that no sessions match.
- Concurrent schema initialization is bounded and atomic; this does not claim
  general cross-process coordination for all later session writes.
- Search snippets can reveal only material already present in the visible local
  conversation projection; provider-private and system-only context is not
  indexed.
- CLI search spans the selected state database. Interactive search additionally
  enforces the active workspace identity and the existing safe-resume checks.
- Model-generated titles, a live debounced picker search field, and stemming
  beyond the bounded prefix rules remain future slices. Workspace-scoped ACP
  session listing is implemented by ADR 0037; full-text search remains exposed
  through the CLI and TUI rather than ACP.

## Rejected alternatives

- Scanning serialized JSON with `LIKE` was rejected because it is unranked,
  escape-sensitive, and scales linearly with the full conversation store.
- Indexing complete provider-native payloads was rejected because encrypted or
  private continuity data must not become a display surface.
- Making title generation depend on a second paid model call was rejected for
  this slice because session persistence and search must succeed offline and
  during provider failure.
