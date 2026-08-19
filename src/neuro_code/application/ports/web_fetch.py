"""Canonical local Web Fetch application contracts.

定义规范的本地 Web Fetch 应用契约.

The contract deliberately contains one URL and bounded text only.  DNS, HTTP,
redirect, decompression, and HTML extraction remain infrastructure concerns;
hosted provider tools do not implement this port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from neuro_code.shared.errors import NeuroCodeError

MAX_FETCH_URL_CHARS = 2_048
DEFAULT_FETCH_MAX_CHARS = 20_000
MAX_FETCH_MAX_CHARS = 100_000
MAX_FETCH_TITLE_CHARS = 512
MAX_FETCH_MEDIA_TYPE_CHARS = 128
MAX_FETCH_RESULT_CHARS = 100_000


class WebFetchMode(StrEnum):
    """User-selected main-facing Web Fetch execution mode."""

    DISABLED = "disabled"
    AUTO = "auto"
    LOCAL = "local"
    INLINE = "inline"


class WebFetchExecutionPath(StrEnum):
    """Resolved path after MAIN capability inspection."""

    DISABLED = "disabled"
    LOCAL = "local"
    INLINE_HOSTED = "inline_hosted"
    UNAVAILABLE = "unavailable"


def resolve_web_fetch_path(
    mode: WebFetchMode,
    *,
    inline_supported: bool,
) -> WebFetchExecutionPath:
    """Resolve explicit user intent without implicit enablement."""

    if mode is WebFetchMode.DISABLED:
        return WebFetchExecutionPath.DISABLED
    if mode is WebFetchMode.LOCAL:
        return WebFetchExecutionPath.LOCAL
    if mode is WebFetchMode.INLINE:
        return (
            WebFetchExecutionPath.INLINE_HOSTED
            if inline_supported
            else WebFetchExecutionPath.UNAVAILABLE
        )
    return WebFetchExecutionPath.INLINE_HOSTED if inline_supported else WebFetchExecutionPath.LOCAL


class WebFetchProvenance(StrEnum):
    """How the URL entered the fetch boundary."""

    USER_PROVIDED = "user_provided"
    SEARCH_RESULT = "search_result"
    MODEL_GENERATED = "model_generated"
    FETCH_REDIRECT = "fetch_redirect"


class WebFetchErrorCode(StrEnum):
    """Stable, credential-free local fetch error vocabulary."""

    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
    UNSAFE_DESTINATION = "UNSAFE_DESTINATION"
    DNS_FAILURE = "DNS_FAILURE"
    DNS_UNSAFE_RESULT = "DNS_UNSAFE_RESULT"
    REDIRECT_UNSAFE = "REDIRECT_UNSAFE"
    TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
    SECRET_IN_URL = "SECRET_IN_URL"
    TIMEOUT = "TIMEOUT"
    TLS_ERROR = "TLS_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    BODY_TOO_LARGE = "BODY_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    CANCELLED = "CANCELLED"


class WebFetchError(NeuroCodeError):
    """Expected local fetch failure with no URL or response-body echo."""

    def __init__(
        self,
        code: WebFetchErrorCode,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        if not isinstance(code, WebFetchErrorCode):
            raise TypeError("web fetch error code must be canonical")
        bounded = " ".join(str(message).split())[:512]
        super().__init__(bounded or code.value)
        self.code = code
        self.status_code = status_code


def _error(code: WebFetchErrorCode, message: str) -> WebFetchError:
    return WebFetchError(code, message)


def _normalize_url_parts(value: str) -> tuple[str, SplitResult, str, int]:
    if not isinstance(value, str):
        raise TypeError("web fetch URL must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_FETCH_URL_CHARS:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL is empty or too long")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in candidate
    ):
        raise _error(WebFetchErrorCode.INVALID_URL, "URL contains unsupported whitespace")
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL could not be parsed") from error
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise _error(WebFetchErrorCode.UNSUPPORTED_SCHEME, "URL scheme is not HTTP or HTTPS")
    if not parsed.netloc or parsed.hostname is None:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL must be absolute")
    if parsed.username is not None or parsed.password is not None:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL must not contain user information")
    try:
        explicit_port = parsed.port
    except ValueError as error:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL port is invalid") from error
    effective_port = explicit_port or (443 if scheme == "https" else 80)
    if effective_port not in {80, 443}:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL port is not allowed")
    if explicit_port is not None and explicit_port != (443 if scheme == "https" else 80):
        raise _error(WebFetchErrorCode.INVALID_URL, "URL port does not match its scheme")
    hostname = parsed.hostname.casefold()
    if "%" in hostname:
        raise _error(WebFetchErrorCode.INVALID_URL, "IPv6 zone identifiers are not allowed")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError as error:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL hostname is invalid") from error
    if not ascii_hostname or len(ascii_hostname) > 253:
        raise _error(WebFetchErrorCode.INVALID_URL, "URL hostname is invalid")
    normalized_netloc = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    if explicit_port is not None:
        normalized_netloc = f"{normalized_netloc}:{explicit_port}"
    normalized = urlunsplit((scheme, normalized_netloc, parsed.path, parsed.query, ""))
    return normalized, parsed, ascii_hostname, effective_port


def normalize_web_fetch_url(value: str) -> str:
    """Validate syntax and normalize one HTTP(S) URL without resolving it."""

    return _normalize_url_parts(value)[0]


@dataclass(frozen=True, slots=True)
class WebFetchRequest:
    """Small request accepted by the local fetch service."""

    url: str
    max_chars: int = DEFAULT_FETCH_MAX_CHARS
    provenance: WebFetchProvenance = WebFetchProvenance.MODEL_GENERATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", normalize_web_fetch_url(self.url))
        if (
            isinstance(self.max_chars, bool)
            or not isinstance(self.max_chars, int)
            or not 1 <= self.max_chars <= MAX_FETCH_MAX_CHARS
        ):
            raise ValueError(f"max_chars must be between 1 and {MAX_FETCH_MAX_CHARS}")
        if not isinstance(self.provenance, WebFetchProvenance):
            raise TypeError("web fetch provenance must be canonical")


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    """Bounded, text-only result returned to the model-facing tool."""

    requested_url: str
    final_url: str
    title: str
    media_type: str
    content: str
    status_code: int
    truncated: bool = False
    provenance: WebFetchProvenance = WebFetchProvenance.MODEL_GENERATED
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_url", normalize_web_fetch_url(self.requested_url))
        object.__setattr__(self, "final_url", normalize_web_fetch_url(self.final_url))
        for name, value, maximum in (
            ("title", self.title, MAX_FETCH_TITLE_CHARS),
            ("media_type", self.media_type, MAX_FETCH_MEDIA_TYPE_CHARS),
        ):
            if not isinstance(value, str):
                raise TypeError(f"web fetch {name} must be a string")
            if len(value) > maximum:
                raise ValueError(f"web fetch {name} is too long")
        if not isinstance(self.content, str):
            raise TypeError("web fetch content must be a string")
        content = self.content
        truncated = self.truncated
        if len(content) > MAX_FETCH_RESULT_CHARS:
            content = content[:MAX_FETCH_RESULT_CHARS]
            truncated = True
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("web fetch status code must be an integer")
        if not 100 <= self.status_code <= 599:
            raise ValueError("web fetch status code is invalid")
        if not isinstance(truncated, bool):
            raise TypeError("web fetch truncated must be a boolean")
        if not isinstance(self.provenance, WebFetchProvenance):
            raise TypeError("web fetch provenance must be canonical")
        if self.metadata is not None:
            if not isinstance(self.metadata, Mapping):
                raise TypeError("web fetch metadata must be a mapping")
            if len(self.metadata) > 16:
                raise ValueError("web fetch metadata is too large")
            if any(
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or not isinstance(value, (str, int, float, bool, type(None)))
                for key, value in self.metadata.items()
            ):
                raise ValueError("web fetch metadata must contain bounded scalar values")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "truncated", truncated)


class WebFetchBackend(Protocol):
    """Execution port for one local or future trusted fetch backend."""

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult: ...


class WebFetchQueryPort(Protocol):
    """Application-facing query boundary for the model-visible tool."""

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult: ...


__all__ = [
    "DEFAULT_FETCH_MAX_CHARS",
    "MAX_FETCH_MAX_CHARS",
    "MAX_FETCH_MEDIA_TYPE_CHARS",
    "MAX_FETCH_RESULT_CHARS",
    "MAX_FETCH_TITLE_CHARS",
    "MAX_FETCH_URL_CHARS",
    "WebFetchBackend",
    "WebFetchError",
    "WebFetchErrorCode",
    "WebFetchExecutionPath",
    "WebFetchMode",
    "WebFetchProvenance",
    "WebFetchQueryPort",
    "WebFetchRequest",
    "WebFetchResult",
    "normalize_web_fetch_url",
    "resolve_web_fetch_path",
]
