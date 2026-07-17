# ADR 0002: Normalize provider streams and preserve opaque round-trip state

[简体中文](../../zh-CN/adr/0002-normalized-provider-streams.md) · **English**

- Status: accepted
- Date: 2026-07-17
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

OpenAI-compatible Chat Completions, Anthropic Messages, and Gemini
`streamGenerateContent` expose different message shapes, stream event names,
tool-call identifiers, completion reasons, and token-usage fields. Letting
these payloads leak into the agent loop would couple every application feature
to each provider. Some APIs also return opaque values, such as Gemini thought
signatures, that must be sent back with the associated function call.

## Decision

Each provider has a native adapter that converts normalized `Message` and
`ToolDefinition` values into its wire format and emits the shared `ModelEvent`
union. The runtime only handles text, reasoning, complete tool calls, and
completion events. Provider-only values required by a later request are stored
in `ToolCall.metadata` under provider-namespaced keys, persisted by the
canonical session store, and otherwise treated as opaque.

HTTP failures, malformed streams, provider error events, incomplete tool
calls, and blocked prompts become credential-safe `ProviderError` instances.
Unknown stream events are ignored so additive protocol changes do not break a
turn.

## Consequences

- The agent loop and UI remain provider-independent.
- Native API features can be added inside adapters without changing the port.
- Session persistence must retain tool metadata exactly.
- Cross-provider session resume may ignore foreign metadata but must not alter
  or execute it.
- Provider contract fixtures and opt-in live tests are required before an
  adapter can move from `partial` to `compatible`.
