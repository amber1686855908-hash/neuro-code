# ADR 0004 — Preserve ordered session items

[简体中文](../../zh-CN/adr/0004-ordered-session-items.md) · **English**

## Status

Accepted.

## Context

Grok Build v1 JSONL is not a list of chat messages. It interleaves ordinary
messages with Responses-API reasoning and backend-executed tool-call records.
User and tool messages can also contain ordered image parts. Flattening these
values into strings loses provider context, record order, and image identity,
while exposing provider payloads directly to the agent loop would couple the
runtime to one API.

## Decision

The persisted conversation is an ordered union named `SessionItem`:

- `Message` remains the provider-independent runtime contract and may contain
  ordered text/image `ContentPart` values;
- `PreservedContextItem` stores a deeply immutable, validated JSON payload for
  reasoning or backend tool calls without interpreting provider-only fields.

`SessionSnapshot` owns the complete item sequence and exposes a filtered
`messages` property. `SessionStore.load_session_items` serves export and
migration paths, while `load_messages` serves the agent runtime. Existing
SQLite message-only JSON remains readable. If preserved context exists,
`save_messages` accepts only the existing message prefix plus appended messages
so a resumed run cannot silently reorder or erase imported items.

JSON session export advances to schema version 2 and includes both `messages`
and `conversation_items`. Provider-native image replay is a separate adapter
decision documented in [ADR 0005](0005-provider-native-image-replay.md).
Preserved provider-context records remain outside the normalized agent message
projection.

At the Rust import boundary, legacy context embedded in assistant `raw_output`,
singular `reasoning`, or v0 `reasoning_content` is lifted before the assistant.
Standalone backend-tool IDs seen earlier in the stream suppress matching
embedded copies, while reasoning entries are never collapsed. Invalid embedded
and unsupported entries are reported independently from invalid JSONL rows.

## Consequences

Rust reasoning, backend-tool payloads, image URLs, and their relative order can
survive import, SQLite round trips, resume, and export. The headless runtime and
existing provider adapters keep their normalized `Message` interface. Opaque
context must remain validated JSON, and features that need to edit or replay it
must add an explicit typed adapter rather than inspect arbitrary fields in the
application layer.
