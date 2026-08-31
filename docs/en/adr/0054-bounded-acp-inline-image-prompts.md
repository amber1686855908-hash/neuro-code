# ADR 0054 — Bounded ACP inline-image prompts

[简体中文](../../zh-CN/adr/0054-bounded-acp-inline-image-prompts.md) · **English**

## Status

Accepted

## Context

The ACP adapter already persisted ordered provider-independent `ContentPart`
values and provider adapters already had native, provider-specific image
projection. At this ADR's acceptance, ACP prompt conversion accepted only Text
and ResourceLink blocks, so an ACP client could not send a screenshot into that
existing safe path. Replaying raw media through `session/load` would also turn
a visible-history API into an unbounded binary and URL disclosure channel.

## Decision

ACP accepts inline `ImageContentBlock` values alongside Text and ResourceLink
blocks, preserving their supplied order as canonical `ContentPart` values. The
adapter accepts raw, validated base64 only; it normalizes `image/jpg` to
`image/jpeg`, allows a fixed raster MIME set, and forms a data URI without
reading the optional ACP URI, a local file, or a remote URL. It caps one prompt
at eight images, 5 MiB decoded per image, and 10 MiB decoded in aggregate.

The ordered parts enter the ordinary `AgentConversation` and durable session
history. Provider adapters remain responsible for their own role, media-type,
and request-size validation; unsupported input follows their existing safe
text-placeholder behavior. Runtime events and ACP load history use the domain
safe model projection, so they show an image placeholder but never emit a data
URI, media bytes, or image URL.

Subsequent prompt-content consolidation now accepts bounded audio and embedded
binary-resource ACP prompt blocks through the canonical content boundary;
bounded embedded text resources are defined separately in ADR 0055. Full binary
multimedia history replay and remote media transfer remain unsupported.

## Consequences

An ACP screenshot can reach compatible models on its first turn and after a
durable resume without adding provider dependencies to the ACP interface or
media I/O to the application. The client receives an honest history marker
instead of a silent omission, while another client cannot recover stored media
from replay. Full binary multimedia history replay and remote media transfer,
including provider-specific replay semantics, remain separate capabilities with
their own authority, size, and lifecycle decisions.
