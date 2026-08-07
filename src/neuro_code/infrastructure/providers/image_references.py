"""Canonical provider image-reference validation helpers.

定义规范的 Provider 图像引用验证辅助函数."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import urlsplit

OPENAI_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
ANTHROPIC_IMAGE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
GEMINI_IMAGE_MEDIA_TYPES = frozenset(
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

OPENAI_MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024
ANTHROPIC_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
GEMINI_MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024

_MAX_REMOTE_URL_CHARS = 16_384
_MEDIA_TYPE_ALIASES = {"image/jpg": "image/jpeg"}


@dataclass(frozen=True, slots=True)
class InlineImageReference:
    media_type: str
    data: str

    @property
    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"


@dataclass(frozen=True, slots=True)
class RemoteImageReference:
    url: str


ImageReference = InlineImageReference | RemoteImageReference


def parse_image_reference(
    value: str,
    *,
    allowed_media_types: frozenset[str],
    max_inline_bytes: int,
) -> ImageReference | None:
    """Validate an image reference without performing network or filesystem I/O.

    验证图像引用,不执行网络或文件系统 I/O."""

    if value[:5].lower() == "data:":
        header, separator, data = value.partition(",")
        if not separator or not data:
            return None
        metadata = header[5:].split(";")
        if len(metadata) != 2 or metadata[1].lower() != "base64":
            return None
        media_type = _MEDIA_TYPE_ALIASES.get(metadata[0].lower(), metadata[0].lower())
        if media_type not in allowed_media_types:
            return None

        max_encoded_chars = 4 * ((max_inline_bytes + 2) // 3)
        if len(data) > max_encoded_chars:
            return None
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not decoded or len(decoded) > max_inline_bytes:
            return None
        return InlineImageReference(media_type, data)

    if len(value) > _MAX_REMOTE_URL_CHARS:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return RemoteImageReference(value)


def is_gemini_file_uri(reference: RemoteImageReference) -> bool:
    """Return whether a URL is a Gemini Developer API File resource URI.

    返回 URL 是否为 Gemini Developer API File 资源 URI."""

    parsed = urlsplit(reference.url)
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "generativelanguage.googleapis.com"
        and parsed.path.startswith(("/v1/files/", "/v1beta/files/"))
    )
