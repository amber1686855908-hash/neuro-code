# ADR 0145: ACP Prompt Content Boundary Extraction

- Status: Accepted
- Date: 2026-08-30
- Scope: first structural slice of V1 Interface Boundary Consolidation
- Depends on: ADR 0035, ADR 0054, and ADR 0055

## Context

`neuro_code.acp` is still the ACP/JSON-RPC adapter and legitimately owns
connection, session, update, client-capability, MCP, and transport lifecycle.
Its inbound prompt/content validation and conversion was nevertheless mixed
with those responsibilities. That made the first interface-package
consolidation step difficult to test and gave the content boundary no explicit
owner.

The existing `neuro_code.interfaces.acp` package already contains bounded ACP
serialization leaves. The first safe consolidation slice is therefore a pure
prompt/content boundary. It must preserve the frozen ACP behavior and must
not turn `neuro_code.acp` into a facade before the remaining responsibilities
are migrated in separately bounded slices.

## Decision

`neuro_code.interfaces.acp.content` is the canonical owner of the inbound ACP
prompt/content conversion boundary. It owns:

- `ConvertedPrompt` and `PromptBlock`;
- `convert_prompt_content` and its private validation/conversion helpers; and
- the content-specific count and byte limits for text, image, audio, embedded
  resources, resource links, and annotations.

`neuro_code.acp` imports these symbols directly from the canonical module. It
does not retain a second implementation or a forwarding wrapper. Existing
imports therefore continue to resolve to the same function, class, typing
alias, and constant objects.

`MAX_RESOURCE_FIELD_BYTES` is deliberately classified as
`SHARED_ACP_PROTOCOL`, not `CONTENT_BOUNDARY`: the existing history, event, and
session projections also use it. It has one dependency-neutral owner in the
existing `neuro_code.interfaces.acp.serialization` leaf, and the content
module imports that shared limit. It is not duplicated and is not presented as
a content-only limit.

## Accepted content and bounded behavior

The conversion accepts ACP `TextContentBlock`, inline
`ImageContentBlock`, inline `AudioContentBlock`, `ResourceContentBlock`, and
`EmbeddedResourceContentBlock` values in the supplied order. Embedded
resources may contain supplied `TextResourceContents` or
`BlobResourceContents`. Text, images, audio, embedded resources, resource
links, annotations, and the aggregate prompt all retain their existing count
and UTF-8/decoded-byte limits.

Images remain restricted to validated base64 and the fixed raster MIME
allowlist. Audio remains restricted to validated base64 with an `audio/*` MIME
type. Resource links and embedded-resource URIs are metadata only: the
adapter never resolves, reads, downloads, or dereferences them. `_meta` values
are omitted from the model-visible projection. Invalid input continues to
raise the same ACP `RequestError` categories and observed reason values.

The resulting ordered `ContentPart` tuple is unchanged. It continues to be
carried with the user message so provider adapters can apply their own role,
MIME, and request-size validation on current and resumed turns.

## Dependency direction

The allowed direction for this slice is:

```text
neuro_code.acp
        -> neuro_code.interfaces.acp.content
        -> neuro_code.interfaces.acp.serialization / domain conversation types
```

`interfaces.acp.content` does not import `neuro_code.acp`, bootstrap, or
infrastructure. It performs no session lookup, provider call, resource I/O,
global registration, or mutable lifecycle coordination.

## Explicit non-goals

This ADR does not move or redesign ACP session ownership, connection or
transport handling, `NeuroCodeAcpAgent`, `_AcpEventMapper`, history/update
projection, client filesystem or terminal capability handling, MCP bridging,
stdio, WebSocket, TUI, CLI, domain orchestration, persistence, or any later
interface boundary. It does not change ACP wire shapes, limits, accepted
types, error semantics, permissions, sandbox behavior, or provider behavior.

## Compatibility and staged strategy

The top-level ACP module remains a mixed adapter. Only the prompt/content
symbols in this ADR have a canonical owner in the interface package. The
compatibility imports are identity-preserving, and architecture tests assert
both that direction and the absence of a duplicate implementation.

Future ACP consolidation slices may separately establish canonical owners for
updates, client capabilities, agent/server handling, and transport only after
each boundary has its own audit, compatibility proof, and behavior-preserving
validation. This ADR does not pre-authorize those slices.

## Validation

Validation covers the existing ACP content matrix, ACP raw stdio and E2E
paths, dependency and import contracts, object identity, documentation parity,
the complete repository quality gates, and the final pull-request merge-ref
CI. The acceptance bar is structural consolidation with unchanged observable
ACP behavior.
