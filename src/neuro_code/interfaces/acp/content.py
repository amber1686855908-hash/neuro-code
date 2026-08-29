"""Bounded inbound ACP prompt/content conversion.

ACP 入站提示词与内容块的有界转换.

This module owns only validation and conversion of ACP prompt content. It does
not inspect sessions, call providers, dereference resources, or perform I/O.
本模块只负责 ACP prompt content 的校验与转换,不读取 session、不调用 provider、不解引用资源,也不执行 I/O.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass

from acp.exceptions import RequestError
from acp.schema import (
    Annotations,
    AudioContentBlock,
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from neuro_code.domain.conversation.messages import ContentPart, ContentPartKind
from neuro_code.interfaces.acp.serialization import (
    MAX_RESOURCE_FIELD_BYTES,
    sanitize_controls,
    serialized_size_bytes,
)

MAX_PROMPT_BLOCKS = 96
MAX_TEXT_BLOCKS = 64
MAX_TEXT_BLOCK_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 256 * 1024
MAX_IMAGE_BLOCKS = 8
MAX_IMAGE_BLOCK_BYTES = 5 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BLOCKS = 8
MAX_AUDIO_BLOCK_BYTES = 5 * 1024 * 1024
MAX_AUDIO_TOTAL_BYTES = 10 * 1024 * 1024
MAX_EMBEDDED_TEXT_RESOURCES = 8
MAX_EMBEDDED_TEXT_RESOURCE_BYTES = 64 * 1024
MAX_EMBEDDED_TEXT_TOTAL_BYTES = 128 * 1024
MAX_EMBEDDED_BINARY_RESOURCE_BYTES = 5 * 1024 * 1024
MAX_EMBEDDED_BINARY_TOTAL_BYTES = 10 * 1024 * 1024
MAX_RESOURCE_LINKS = 32
MAX_RESOURCE_LINK_BYTES = 64 * 1024
MAX_RESOURCE_URI_BYTES = 4 * 1024
MAX_RESOURCE_NAME_BYTES = 512
MAX_ANNOTATIONS_BYTES = 4 * 1024
MAX_ANNOTATION_AUDIENCE = 16
MAX_ANNOTATION_AUDIENCE_BYTES = 128

_ACP_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
_ACP_IMAGE_MEDIA_TYPE_ALIASES = {"image/jpg": "image/jpeg"}

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


@dataclass(frozen=True, slots=True)
class ConvertedPrompt:
    """Bounded model input preserving ACP text, image, and link ordering.

    保持 ACP 文本,图像和链接顺序的有界模型输入."""

    content: str
    content_parts: tuple[ContentPart, ...]


def _invalid_params(reason: str, details: str | None = None) -> RequestError:
    data = {"reason": reason}
    if details is not None:
        data["details"] = details
    return RequestError.invalid_params(data)


def _bounded_input_text(value: str, *, limit: int, field_name: str) -> str:
    sanitized = sanitize_controls(value)
    if len(sanitized.encode("utf-8")) > limit:
        raise _invalid_params(f"{field_name}_too_large")
    return sanitized


def _annotations_payload(annotations: Annotations | None) -> dict[str, object] | None:
    if annotations is None:
        return None
    payload: dict[str, object] = {}
    if annotations.audience is not None:
        if len(annotations.audience) > MAX_ANNOTATION_AUDIENCE:
            raise _invalid_params("resource_annotations_too_large")
        payload["audience"] = [
            _bounded_input_text(
                audience,
                limit=MAX_ANNOTATION_AUDIENCE_BYTES,
                field_name="resource_annotation_audience",
            )
            for audience in annotations.audience
        ]
    if annotations.last_modified is not None:
        payload["lastModified"] = _bounded_input_text(
            annotations.last_modified,
            limit=MAX_RESOURCE_FIELD_BYTES,
            field_name="resource_annotation_last_modified",
        )
    if annotations.priority is not None:
        if not math.isfinite(annotations.priority):
            raise _invalid_params("resource_annotation_priority_invalid")
        payload["priority"] = annotations.priority
    if serialized_size_bytes(payload) > MAX_ANNOTATIONS_BYTES:
        raise _invalid_params("resource_annotations_too_large")
    return payload or None


def _resource_payload(resource: ResourceContentBlock) -> dict[str, object]:
    payload: dict[str, object] = {
        "uri": _bounded_input_text(
            resource.uri,
            limit=MAX_RESOURCE_URI_BYTES,
            field_name="resource_uri",
        ),
        "name": _bounded_input_text(
            resource.name,
            limit=MAX_RESOURCE_NAME_BYTES,
            field_name="resource_name",
        ),
    }
    for source_name, wire_name in (
        ("title", "title"),
        ("description", "description"),
        ("mime_type", "mimeType"),
    ):
        value = getattr(resource, source_name)
        if value is not None:
            payload[wire_name] = _bounded_input_text(
                value,
                limit=MAX_RESOURCE_FIELD_BYTES,
                field_name=f"resource_{source_name}",
            )
    if resource.size is not None:
        if resource.size < 0:
            raise _invalid_params("resource_size_invalid")
        payload["size"] = resource.size
    annotations = _annotations_payload(resource.annotations)
    if annotations is not None:
        payload["annotations"] = annotations
    return payload


def _image_content_part(block: ImageContentBlock) -> tuple[ContentPart, int]:
    """Validate one inline ACP image without reading or dereferencing its URI.

    验证一个 ACP 内联图像,但不读取或解引用其 URI."""

    media_type = _ACP_IMAGE_MEDIA_TYPE_ALIASES.get(
        block.mime_type.casefold(), block.mime_type.casefold()
    )
    if media_type not in _ACP_IMAGE_MEDIA_TYPES:
        raise _invalid_params("image_mime_type_unsupported")
    max_encoded_bytes = 4 * ((MAX_IMAGE_BLOCK_BYTES + 2) // 3)
    if not block.data or len(block.data) > max_encoded_bytes:
        raise _invalid_params("image_block_too_large")
    try:
        decoded = base64.b64decode(block.data, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_params("image_data_invalid") from None
    if not decoded or len(decoded) > MAX_IMAGE_BLOCK_BYTES:
        raise _invalid_params("image_block_too_large")
    return ContentPart.from_image(f"data:{media_type};base64,{block.data}"), len(decoded)


def _audio_content_part(block: AudioContentBlock) -> tuple[ContentPart, int]:
    """Validate and preserve one inline ACP audio block."""

    media_type = block.mime_type.casefold()
    if not media_type.startswith("audio/"):
        raise _invalid_params("audio_mime_type_unsupported")
    max_encoded_bytes = 4 * ((MAX_AUDIO_BLOCK_BYTES + 2) // 3)
    if not block.data or len(block.data) > max_encoded_bytes:
        raise _invalid_params("audio_block_too_large")
    try:
        decoded = base64.b64decode(block.data, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_params("audio_data_invalid") from None
    if not decoded or len(decoded) > MAX_AUDIO_BLOCK_BYTES:
        raise _invalid_params("audio_block_too_large")
    return ContentPart.from_audio(block.data, media_type), len(decoded)


def _embedded_text_resource_part(
    block: EmbeddedResourceContentBlock,
) -> tuple[ContentPart, int]:
    """Render an already-provided ACP text resource without resource I/O.

    渲染已提供的 ACP 文本资源,不进行资源 I/O."""

    resource = block.resource
    if isinstance(resource, BlobResourceContents):
        uri = _bounded_input_text(
            resource.uri,
            limit=MAX_RESOURCE_URI_BYTES,
            field_name="embedded_resource_uri",
        )
        if not uri.strip():
            raise _invalid_params("embedded_resource_uri_empty")
        media_type = resource.mime_type or "application/octet-stream"
        media_type = _bounded_input_text(
            media_type,
            limit=MAX_RESOURCE_FIELD_BYTES,
            field_name="embedded_resource_mime_type",
        )
        max_encoded_bytes = 4 * ((MAX_EMBEDDED_BINARY_RESOURCE_BYTES + 2) // 3)
        if not resource.blob or len(resource.blob) > max_encoded_bytes:
            raise _invalid_params("embedded_resource_blob_too_large")
        try:
            decoded = base64.b64decode(resource.blob, validate=True)
        except (binascii.Error, ValueError):
            raise _invalid_params("embedded_resource_blob_invalid") from None
        if not decoded or len(decoded) > MAX_EMBEDDED_BINARY_RESOURCE_BYTES:
            raise _invalid_params("embedded_resource_blob_too_large")
        return ContentPart.from_blob(uri, resource.blob, media_type), len(decoded)
    if not isinstance(resource, TextResourceContents):
        raise _invalid_params("embedded_resource_unsupported")

    uri = _bounded_input_text(
        resource.uri,
        limit=MAX_RESOURCE_URI_BYTES,
        field_name="embedded_resource_uri",
    )
    if not uri.strip():
        raise _invalid_params("embedded_resource_uri_empty")
    text = _bounded_input_text(
        resource.text,
        limit=MAX_EMBEDDED_TEXT_RESOURCE_BYTES,
        field_name="embedded_resource_text",
    )
    if not text.strip():
        raise _invalid_params("embedded_resource_text_empty")

    metadata: dict[str, str] = {"uri": uri}
    if resource.mime_type is not None:
        metadata["mimeType"] = _bounded_input_text(
            resource.mime_type,
            limit=MAX_RESOURCE_FIELD_BYTES,
            field_name="embedded_resource_mime_type",
        )
    rendered_metadata = json.dumps(
        metadata,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    rendered = f"<embedded_resource>{rendered_metadata}</embedded_resource>\n{text}"
    return ContentPart.from_text(rendered), len(text.encode("utf-8"))


def convert_prompt_content(prompt: list[PromptBlock]) -> ConvertedPrompt:
    """Convert supported ACP blocks to bounded, ordered structured model input.

    将支持的 ACP 块转换为有界,有序的结构化模型输入."""

    if not prompt:
        raise _invalid_params("prompt_empty")
    if len(prompt) > MAX_PROMPT_BLOCKS:
        raise _invalid_params("too_many_prompt_blocks")

    content_parts: list[ContentPart] = []
    text_count = 0
    image_count = 0
    image_bytes = 0
    audio_count = 0
    audio_bytes = 0
    embedded_text_resource_count = 0
    embedded_text_resource_bytes = 0
    embedded_binary_resource_count = 0
    embedded_binary_resource_bytes = 0
    resource_count = 0
    resource_bytes = 0
    for block in prompt:
        if isinstance(block, TextContentBlock):
            text_count += 1
            if text_count > MAX_TEXT_BLOCKS:
                raise _invalid_params("too_many_text_blocks")
            content_parts.append(
                ContentPart.from_text(
                    _bounded_input_text(
                        block.text,
                        limit=MAX_TEXT_BLOCK_BYTES,
                        field_name="text_block",
                    )
                )
            )
            continue
        if isinstance(block, ImageContentBlock):
            image_count += 1
            if image_count > MAX_IMAGE_BLOCKS:
                raise _invalid_params("too_many_image_blocks")
            image, decoded_bytes = _image_content_part(block)
            image_bytes += decoded_bytes
            if image_bytes > MAX_IMAGE_TOTAL_BYTES:
                raise _invalid_params("images_too_large")
            content_parts.append(image)
            continue
        if isinstance(block, AudioContentBlock):
            audio_count += 1
            if audio_count > MAX_AUDIO_BLOCKS:
                raise _invalid_params("too_many_audio_blocks")
            audio, decoded_bytes = _audio_content_part(block)
            audio_bytes += decoded_bytes
            if audio_bytes > MAX_AUDIO_TOTAL_BYTES:
                raise _invalid_params("audio_too_large")
            content_parts.append(audio)
            continue
        if isinstance(block, EmbeddedResourceContentBlock):
            if isinstance(block.resource, BlobResourceContents):
                embedded_binary_resource_count += 1
                if embedded_binary_resource_count > MAX_EMBEDDED_TEXT_RESOURCES:
                    raise _invalid_params("too_many_embedded_binary_resources")
            else:
                embedded_text_resource_count += 1
                if embedded_text_resource_count > MAX_EMBEDDED_TEXT_RESOURCES:
                    raise _invalid_params("too_many_embedded_text_resources")
            embedded_resource, resource_bytes = _embedded_text_resource_part(block)
            if embedded_resource.kind is ContentPartKind.BLOB:
                embedded_binary_resource_bytes += resource_bytes
                if embedded_binary_resource_bytes > MAX_EMBEDDED_BINARY_TOTAL_BYTES:
                    raise _invalid_params("embedded_binary_resources_too_large")
            else:
                embedded_text_resource_bytes += resource_bytes
                if embedded_text_resource_bytes > MAX_EMBEDDED_TEXT_TOTAL_BYTES:
                    raise _invalid_params("embedded_text_resources_too_large")
            content_parts.append(embedded_resource)
            continue
        if isinstance(block, ResourceContentBlock):
            resource_count += 1
            if resource_count > MAX_RESOURCE_LINKS:
                raise _invalid_params("too_many_resource_links")
            payload = _resource_payload(block)
            serialized = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            resource_bytes += len(serialized.encode("utf-8"))
            if resource_bytes > MAX_RESOURCE_LINK_BYTES:
                raise _invalid_params("resource_links_too_large")
            content_parts.append(
                ContentPart.from_text(f"<resource_link>{serialized}</resource_link>")
            )
            continue
        raise _invalid_params("unsupported_prompt_content")

    converted = "\n".join(part.text for part in content_parts if part.text is not None)
    if not converted.strip() and not (image_count or audio_count or embedded_binary_resource_bytes):
        raise _invalid_params("prompt_empty")
    if len(converted.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise _invalid_params("prompt_too_large")
    return ConvertedPrompt(converted, tuple(content_parts))


__all__ = [
    "MAX_ANNOTATIONS_BYTES",
    "MAX_ANNOTATION_AUDIENCE",
    "MAX_ANNOTATION_AUDIENCE_BYTES",
    "MAX_AUDIO_BLOCKS",
    "MAX_AUDIO_BLOCK_BYTES",
    "MAX_AUDIO_TOTAL_BYTES",
    "MAX_EMBEDDED_BINARY_RESOURCE_BYTES",
    "MAX_EMBEDDED_BINARY_TOTAL_BYTES",
    "MAX_EMBEDDED_TEXT_RESOURCES",
    "MAX_EMBEDDED_TEXT_RESOURCE_BYTES",
    "MAX_EMBEDDED_TEXT_TOTAL_BYTES",
    "MAX_IMAGE_BLOCKS",
    "MAX_IMAGE_BLOCK_BYTES",
    "MAX_IMAGE_TOTAL_BYTES",
    "MAX_PROMPT_BLOCKS",
    "MAX_PROMPT_BYTES",
    "MAX_RESOURCE_FIELD_BYTES",
    "MAX_RESOURCE_LINKS",
    "MAX_RESOURCE_LINK_BYTES",
    "MAX_RESOURCE_NAME_BYTES",
    "MAX_RESOURCE_URI_BYTES",
    "MAX_TEXT_BLOCKS",
    "MAX_TEXT_BLOCK_BYTES",
    "ConvertedPrompt",
    "PromptBlock",
    "convert_prompt_content",
]
