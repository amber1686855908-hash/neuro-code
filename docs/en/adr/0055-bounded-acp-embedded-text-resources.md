# ADR 0055 — Bounded ACP embedded text resources

[简体中文](../../zh-CN/adr/0055-bounded-acp-embedded-text-resources.md) · **English**

## Status

Accepted

## Context

An ACP client can include `EmbeddedResourceContentBlock` values whose resource
already contains text or a base64 binary blob. Treating the URI as a path or
network address would add unrequested filesystem or network authority to the
ACP interface. Treating a blob as text would create a second unbounded binary
input path. At the same time, already-provided text is useful context and the
existing ordered text `ContentPart` path persists it safely through model
providers and durable session history.

## Decision

ACP accepts only `TextResourceContents` from an embedded resource block. It
uses the supplied text directly, never opens, resolves, downloads, or
dereferences the associated URI. The URI and optional MIME type become a
bounded JSON origin label before the text. The block accepts at most eight
resources, 64 KiB of text per resource, and 128 KiB of embedded-resource text
per prompt. Empty URI or text values, and every malformed or oversized value,
fail closed.

The resulting value is an ordered text `ContentPart`; normal conversation
storage, provider adaptation, redaction, and visible-history projection apply.
It is input data rather than trusted instruction. Block, resource, and
annotation `_meta` values and annotations are not projected. No resource bytes
are fetched or decoded.

`BlobResourceContents`, audio, and other unsupported prompt blocks remain
rejected. Binary multimedia history replay remains outside this decision.

## Consequences

Clients can attach small, explicit text such as an editor buffer or generated
review note without granting the agent any new read or network capability.
The model and a later ACP history replay receive an honest origin label and
bounded text, while clients cannot use this path to make Neuro Code inspect an
arbitrary URI or recover binary attachments. Native binary attachments,
resource resolution, and richer embedded-resource semantics require separate
authority, size, persistence, and replay decisions.
