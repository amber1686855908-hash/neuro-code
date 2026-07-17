# ADR 0005 — Provider-native image replay

[简体中文](../../zh-CN/adr/0005-provider-native-image-replay.md) · **English**

## Status

Accepted.

## Context

Imported Grok Build sessions preserve ordered text and image references in
provider-independent `ContentPart` values. Flattening every image into text
keeps requests valid but prevents a resumed model from seeing screenshots and
tool-produced images. Sending every stored string as provider media is also
unsafe: malformed data URIs, unsupported media types, oversized payloads,
credential-bearing URLs, and role-specific API restrictions can reject the
entire next turn.

The three adapters use different wire contracts. Chat Completions accepts
`image_url` parts in user content. Anthropic Messages accepts base64 or URL
image blocks in user messages and tool results. Gemini `streamGenerateContent`
accepts inline bytes or a File API URI, but an arbitrary public URL requires a
separate download/upload workflow.

## Decision

Image-reference parsing lives in the provider adapter layer and performs no
network or filesystem I/O. It accepts strictly valid, non-empty base64 data
URIs within a provider-specific size bound, or HTTP(S) URLs with a hostname and
without URL user information. Media types are normalized and checked against a
provider-specific allowlist. Text and image part order is preserved.

Native projection is deliberately bounded:

| Adapter | Native input | Native roles | Explicit fallback |
|---|---|---|---|
| OpenAI-compatible/xAI Chat Completions | PNG/JPEG data URI or public HTTP(S) URL as `image_url` | user | all other roles or invalid input |
| Anthropic Messages | JPEG/PNG/GIF/WebP base64 or public HTTP(S) URL as `image` | user and `tool_result` | system/assistant or invalid input |
| Gemini `streamGenerateContent` | supported image data URI as `inlineData`, or an existing Gemini File API resource as `fileData` | user | arbitrary public URL, tool/system/model role, or invalid input |

Fallback uses the existing visible image placeholder and never copies the raw
image reference into model text or an error. The adapter does not download a
remote image, read a local `file:` URI, upload media, or mutate the imported
session. Provider-context reasoning and backend-tool records remain separate
future replay work.

## Consequences

Supported imported screenshots now reach multimodal models without changing
the domain or persistence contracts. Anthropic also retains the Rust
baseline's native tool-result image behavior. Invalid or non-portable image
references cannot break the request and remain visible to the model as a lost
media boundary rather than disappearing silently.

Gemini public-URL replay and multimodal function responses need an explicit
media-transfer capability and richer metadata before they can be added safely.
OpenAI-compatible tool-result images likewise wait for a provider contract that
is portable beyond a single gateway. Live model-specific limits can still be
stricter than these adapter bounds and require opt-in integration fixtures.
