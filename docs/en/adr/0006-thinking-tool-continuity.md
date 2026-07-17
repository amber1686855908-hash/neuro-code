# ADR 0006 — Thinking-mode tool-call continuity

[简体中文](../../zh-CN/adr/0006-thinking-tool-continuity.md) · **English**

## Status

Accepted.

## Context

The normalized provider stream already exposed reasoning deltas as runtime
events, but the agent discarded them when constructing the corresponding
assistant message. That is insufficient for providers whose thinking-mode tool
protocol treats reasoning as part of the assistant turn. DeepSeek V4 Chat
Completions returns this state in `reasoning_content` and requires the complete
value to accompany an assistant tool-call message in the next request. Dropping
it breaks an otherwise valid multi-step tool loop.

Opaque reasoning imported from a Rust session has a different problem: its
provider, model, and wire-format affinity are not yet strong enough to send it
to an arbitrary active provider safely. It must remain distinct from reasoning
generated in the current normalized runtime.

## Decision

`Message` gains an optional `reasoning_content` field that is valid only for an
assistant role and may not be empty. `AgentRuntime` concatenates every
`ModelReasoningDelta` in a model step and stores the result on that step's
assistant message. SQLite persistence and JSON export use the existing message
serialization path, so a resumed tool loop retains the value without a database
schema migration.

The OpenAI-compatible adapter includes `reasoning_content` only when serializing
an assistant message that also contains tool calls. It does not echo completed,
no-tool reasoning in later requests. The configured `max_output_tokens` is sent
as Chat Completions `max_tokens`, placing the same explicit response bound on
this adapter that the native Anthropic and Gemini adapters already enforce.

`PreservedContextItem` values are not projected into this field by default.
[ADR 0007](0007-provider-affine-context-replay.md) permits visible imported
reasoning only under a strict xAI/source affinity contract; encrypted native
Responses state remains separate. Real-credential probes remain opt-in and
outside the repository and CI; the application reads only the named process
environment variable and never parses project `.env` files automatically.

## Consequences

New thinking-mode tool calls and locally resumed sessions can complete their
required reasoning round trip through the normalized agent runtime. The
reasoning remains opaque to application control flow, but it is now stored in
the local session and visible in JSON exports; session files and exports must
therefore be treated as potentially sensitive data.

The change does not claim that imported Rust reasoning is portable, nor does it
make reasoning replay universal across providers. A manual DeepSeek V4 Flash
probe verifies the current OpenAI-compatible streaming behavior and a read-only
`AgentRuntime`/SQLite round trip. Native local-stateless xAI replay is now the
separate [ADR 0008](0008-xai-responses-native-replay.md) path; durable opt-in
integration fixtures remain future work.
