"""Compatibility facade for canonical provider image-reference helpers.

提供 Provider 图像引用辅助函数的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.image_references import (
    ANTHROPIC_IMAGE_MEDIA_TYPES,
    ANTHROPIC_MAX_INLINE_IMAGE_BYTES,
    GEMINI_IMAGE_MEDIA_TYPES,
    GEMINI_MAX_INLINE_IMAGE_BYTES,
    OPENAI_IMAGE_MEDIA_TYPES,
    OPENAI_MAX_INLINE_IMAGE_BYTES,
    ImageReference,
    InlineImageReference,
    RemoteImageReference,
    is_gemini_file_uri,
    parse_image_reference,
)

__all__ = [
    "ANTHROPIC_IMAGE_MEDIA_TYPES",
    "ANTHROPIC_MAX_INLINE_IMAGE_BYTES",
    "GEMINI_IMAGE_MEDIA_TYPES",
    "GEMINI_MAX_INLINE_IMAGE_BYTES",
    "OPENAI_IMAGE_MEDIA_TYPES",
    "OPENAI_MAX_INLINE_IMAGE_BYTES",
    "ImageReference",
    "InlineImageReference",
    "RemoteImageReference",
    "is_gemini_file_uri",
    "parse_image_reference",
]
