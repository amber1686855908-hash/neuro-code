"""Canonical hosted web-search application contracts.

定义规范的 Hosted Web Search 应用契约.

The contract intentionally contains only bounded, provider-neutral evidence.  A
Responses ``web_search_call`` item, an xAI native payload, and a local client
tool are all implementation details outside this module.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

from neuro_code.application.ports.model import ModelCapabilitySet
from neuro_code.application.ports.routing import ModelRoute
from neuro_code.shared.errors import NeuroCodeError

MAX_QUERY_CHARS = 4_096
MAX_DOMAIN_CHARS = 253
MAX_DOMAIN_COUNT = 16
MAX_SOURCE_COUNT = 32
MAX_SOURCE_URL_CHARS = 2_048
MAX_SOURCE_TITLE_CHARS = 512
MAX_SOURCE_PROVIDER_CHARS = 128
MAX_SOURCE_SNIPPET_CHARS = 2_000
MAX_CITED_TEXT_CHARS = 4_000
MAX_EVIDENCE_CHARS = 24_000
MAX_PROVIDER_PROFILE_CHARS = 128
MAX_MODEL_CHARS = 512
MAX_METADATA_ITEMS = 16
MAX_METADATA_KEY_CHARS = 64
MAX_TOTAL_RESULT_BYTES = 96_000
MAX_MAX_SOURCES = 32

_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_DOMAIN_NAME = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class WebSearchMode(StrEnum):
    """User-selected hosted-search execution mode."""

    DISABLED = "disabled"
    AUTO = "auto"
    INLINE = "inline"
    SIDECAR = "sidecar"


class WebSearchExecutionPath(StrEnum):
    """Resolved runtime path after capability and route inspection."""

    DISABLED = "disabled"
    INLINE_HOSTED = "inline_hosted"
    SIDECAR_HOSTED = "sidecar_hosted"
    UNAVAILABLE = "unavailable"


def resolve_web_search_path(
    mode: WebSearchMode,
    *,
    inline_supported: bool,
    sidecar_available: bool,
) -> WebSearchExecutionPath:
    """Resolve user intent without treating UNKNOWN capability as support."""

    if mode is WebSearchMode.DISABLED:
        return WebSearchExecutionPath.DISABLED
    if mode is WebSearchMode.INLINE:
        return (
            WebSearchExecutionPath.INLINE_HOSTED
            if inline_supported
            else WebSearchExecutionPath.UNAVAILABLE
        )
    if mode is WebSearchMode.SIDECAR:
        return (
            WebSearchExecutionPath.SIDECAR_HOSTED
            if sidecar_available
            else WebSearchExecutionPath.UNAVAILABLE
        )
    if inline_supported:
        return WebSearchExecutionPath.INLINE_HOSTED
    if sidecar_available:
        return WebSearchExecutionPath.SIDECAR_HOSTED
    return WebSearchExecutionPath.UNAVAILABLE


class WebSearchErrorCode(StrEnum):
    """Small, stable error vocabulary exposed to the model-facing tool."""

    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    SEARCH_UNSUPPORTED = "SEARCH_UNSUPPORTED"
    SEARCH_AUTHENTICATION = "SEARCH_AUTHENTICATION"
    SEARCH_RATE_LIMIT = "SEARCH_RATE_LIMIT"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
    SEARCH_PROVIDER_ERROR = "SEARCH_PROVIDER_ERROR"
    SEARCH_PROVIDER_DID_NOT_SEARCH = "SEARCH_PROVIDER_DID_NOT_SEARCH"
    SEARCH_INVALID_REQUEST = "SEARCH_INVALID_REQUEST"


class WebSearchError(NeuroCodeError):
    """A normalized, credential-free hosted-search failure."""

    def __init__(self, code: WebSearchErrorCode, message: str) -> None:
        if not isinstance(code, WebSearchErrorCode):
            raise TypeError("web search error code must be canonical")
        bounded = " ".join(str(message).split())[:1_000]
        super().__init__(bounded or code.value)
        self.code = code


def _bounded_text(
    value: str,
    *,
    name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _domain(value: str) -> str:
    normalized = _bounded_text(value, name="domain", maximum=MAX_DOMAIN_CHARS).strip()
    if not normalized.isascii() or normalized.endswith("."):
        raise ValueError("domain must be an ASCII hostname")
    if normalized.casefold() == "localhost":
        return "localhost"
    if not _DOMAIN_NAME.fullmatch(normalized):
        raise ValueError("domain must be an ASCII hostname")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in normalized.split(".")):
        raise ValueError("domain must contain valid hostname labels")
    return normalized.casefold()


def _http_url(value: str, *, name: str = "url") -> str:
    normalized = _bounded_text(value, name=name, maximum=MAX_SOURCE_URL_CHARS).strip()
    try:
        parsed = urlsplit(normalized)
    except ValueError as error:
        raise ValueError(f"{name} must be a valid HTTP(S) URL") from error
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"{name} must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain user information")
    return normalized


def _safe_metadata(value: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("web search metadata must be a mapping")
    if len(value) > MAX_METADATA_ITEMS:
        raise ValueError(f"web search metadata must contain at most {MAX_METADATA_ITEMS} items")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > MAX_METADATA_KEY_CHARS:
            raise ValueError("web search metadata keys must be short strings")
        if not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError("web search metadata values must be scalar")
        if isinstance(item, str) and len(item) > 512:
            raise ValueError("web search metadata strings are too long")
        normalized[key] = item
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    """The deliberately small request accepted by a hosted search backend."""

    query: str
    max_sources: int = 8
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.query, name="web search query", maximum=MAX_QUERY_CHARS)
        if (
            isinstance(self.max_sources, bool)
            or not isinstance(self.max_sources, int)
            or not 1 <= self.max_sources <= MAX_MAX_SOURCES
        ):
            raise ValueError(f"max_sources must be between 1 and {MAX_MAX_SOURCES}")
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("allowed_domains and blocked_domains are mutually exclusive")
        if len(self.allowed_domains) > MAX_DOMAIN_COUNT:
            raise ValueError(f"allowed_domains must contain at most {MAX_DOMAIN_COUNT} items")
        if len(self.blocked_domains) > MAX_DOMAIN_COUNT:
            raise ValueError(f"blocked_domains must contain at most {MAX_DOMAIN_COUNT} items")
        allowed = tuple(_domain(value) for value in self.allowed_domains)
        blocked = tuple(_domain(value) for value in self.blocked_domains)
        if len(set(allowed)) != len(allowed) or len(set(blocked)) != len(blocked):
            raise ValueError("domain filters must not contain duplicates")
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "allowed_domains", allowed)
        object.__setattr__(self, "blocked_domains", blocked)


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    """One bounded, provider-neutral source reference."""

    url: str
    title: str
    provider: str
    snippet: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _http_url(self.url))
        object.__setattr__(
            self,
            "title",
            _bounded_text(self.title, name="source title", maximum=MAX_SOURCE_TITLE_CHARS),
        )
        object.__setattr__(
            self,
            "provider",
            _bounded_text(
                self.provider,
                name="source provider",
                maximum=MAX_SOURCE_PROVIDER_CHARS,
            ),
        )
        if self.snippet is not None:
            object.__setattr__(
                self,
                "snippet",
                _bounded_text(
                    self.snippet,
                    name="source snippet",
                    maximum=MAX_SOURCE_SNIPPET_CHARS,
                    allow_empty=True,
                ),
            )


@dataclass(frozen=True, slots=True)
class WebSearchCitation:
    """A structured citation attached to one source URL."""

    url: str
    title: str
    cited_text: str | None = None
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _http_url(self.url))
        object.__setattr__(
            self,
            "title",
            _bounded_text(self.title, name="citation title", maximum=MAX_SOURCE_TITLE_CHARS),
        )
        if self.cited_text is not None:
            object.__setattr__(
                self,
                "cited_text",
                _bounded_text(
                    self.cited_text,
                    name="cited text",
                    maximum=MAX_CITED_TEXT_CHARS,
                    allow_empty=True,
                ),
            )
        if (self.start is None) != (self.end is None):
            raise ValueError("citation start and end must be provided together")
        if self.start is not None and (
            isinstance(self.start, bool)
            or not isinstance(self.start, int)
            or isinstance(self.end, bool)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end < self.start
        ):
            raise ValueError("citation range must be a non-negative ordered span")


def _deduplicate_sources(sources: Sequence[WebSearchSource]) -> tuple[WebSearchSource, ...]:
    result: list[WebSearchSource] = []
    seen: set[str] = set()
    for source in sources:
        if source.url.casefold() in seen:
            continue
        seen.add(source.url.casefold())
        if len(result) >= MAX_SOURCE_COUNT:
            break
        result.append(source)
    return tuple(result)


def _deduplicate_citations(citations: Sequence[WebSearchCitation]) -> tuple[WebSearchCitation, ...]:
    result: list[WebSearchCitation] = []
    seen: set[tuple[object, ...]] = set()
    for citation in citations:
        key = (citation.url.casefold(), citation.start, citation.end, citation.cited_text)
        if key in seen:
            continue
        seen.add(key)
        if len(result) >= MAX_SOURCE_COUNT:
            break
        result.append(citation)
    return tuple(result)


def _source_bytes(source: WebSearchSource) -> int:
    return (
        len(source.url.encode())
        + len(source.title.encode())
        + len(source.provider.encode())
        + len((source.snippet or "").encode())
    )


def _citation_bytes(citation: WebSearchCitation) -> int:
    return (
        len(citation.url.encode())
        + len(citation.title.encode())
        + len((citation.cited_text or "").encode())
    )


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """Bounded external evidence returned to a caller or client tool."""

    query: str
    evidence_text: str
    sources: tuple[WebSearchSource, ...] = ()
    citations: tuple[WebSearchCitation, ...] = ()
    provider_profile: str = ""
    model: str = ""
    truncated: bool = False
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.query, name="web search result query", maximum=MAX_QUERY_CHARS)
        if not isinstance(self.evidence_text, str):
            raise TypeError("web search evidence must be a string")
        evidence = self.evidence_text
        truncated = self.truncated
        if len(evidence) > MAX_EVIDENCE_CHARS:
            evidence = evidence[:MAX_EVIDENCE_CHARS]
            truncated = True
        if not isinstance(self.truncated, bool):
            raise TypeError("web search result truncated must be a boolean")
        if not all(isinstance(source, WebSearchSource) for source in self.sources):
            raise TypeError("web search sources must be canonical")
        if not all(isinstance(citation, WebSearchCitation) for citation in self.citations):
            raise TypeError("web search citations must be canonical")
        sources = list(_deduplicate_sources(self.sources))
        citations = _deduplicate_citations(self.citations)
        source_urls = {source.url.casefold() for source in sources}
        for citation in citations:
            if citation.url.casefold() not in source_urls and len(sources) < MAX_SOURCE_COUNT:
                sources.append(
                    WebSearchSource(
                        citation.url,
                        citation.title,
                        self.provider_profile or "unknown",
                    )
                )
                source_urls.add(citation.url.casefold())
        object.__setattr__(self, "query", self.query.strip())
        provider_profile = _bounded_text(
            self.provider_profile,
            name="provider profile",
            maximum=MAX_PROVIDER_PROFILE_CHARS,
            allow_empty=True,
        )
        model = _bounded_text(
            self.model,
            name="search model",
            maximum=MAX_MODEL_CHARS,
            allow_empty=True,
        )
        metadata = _safe_metadata(self.metadata)
        used_bytes = (
            len(self.query.encode())
            + len(provider_profile.encode())
            + len(model.encode())
            + len(str(dict(metadata or {})).encode())
        )

        bounded_sources: list[WebSearchSource] = []
        for source in sources:
            if used_bytes + _source_bytes(source) > MAX_TOTAL_RESULT_BYTES:
                truncated = True
                continue
            bounded_sources.append(source)
            used_bytes += _source_bytes(source)
        source_urls = {source.url.casefold() for source in bounded_sources}
        bounded_citations: list[WebSearchCitation] = []
        for citation in citations:
            if citation.url.casefold() not in source_urls:
                truncated = True
                continue
            if used_bytes + _citation_bytes(citation) > MAX_TOTAL_RESULT_BYTES:
                truncated = True
                continue
            bounded_citations.append(citation)
            used_bytes += _citation_bytes(citation)

        remaining = max(0, MAX_TOTAL_RESULT_BYTES - used_bytes)
        evidence_bytes = evidence.encode()
        if len(evidence_bytes) > remaining:
            evidence = evidence_bytes[:remaining].decode("utf-8", "ignore")
            truncated = True

        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "evidence_text", evidence)
        object.__setattr__(self, "sources", tuple(bounded_sources))
        object.__setattr__(self, "citations", tuple(bounded_citations))
        object.__setattr__(self, "provider_profile", provider_profile)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "truncated", truncated)
        object.__setattr__(self, "metadata", metadata)

    def _fixed_bytes(self) -> int:
        source_bytes = sum(_source_bytes(source) for source in self.sources)
        citation_bytes = sum(_citation_bytes(citation) for citation in self.citations)
        return (
            len(self.query.encode())
            + len(self.provider_profile.encode())
            + len(self.model.encode())
            + source_bytes
            + citation_bytes
        )

    @property
    def total_bytes(self) -> int:
        metadata_bytes = len(str(dict(self.metadata or {})).encode())
        return self._fixed_bytes() + len(self.evidence_text.encode()) + metadata_bytes


@dataclass(frozen=True, slots=True)
class HostedWebSearchEvent:
    """Provider-hosted lifecycle evidence forwarded through a sidecar boundary."""

    provider_profile: str
    model: str
    call_id: str
    name: str
    completed: bool

    def __post_init__(self) -> None:
        _bounded_text(self.provider_profile, name="event provider profile", maximum=128)
        _bounded_text(self.model, name="event model", maximum=MAX_MODEL_CHARS)
        _bounded_text(self.call_id, name="event call id", maximum=256)
        _bounded_text(self.name, name="event tool name", maximum=64)
        if not isinstance(self.completed, bool):
            raise TypeError("event completed must be a boolean")


HostedWebSearchEventSink = Callable[[HostedWebSearchEvent], Awaitable[object]]


class HostedWebSearch(Protocol):
    """Execution port for one configured, hosted search route."""

    @property
    def provider_profile(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> ModelCapabilitySet: ...

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult: ...


WebSearchBackend = HostedWebSearch


class WebSearchQueryPort(Protocol):
    """Application-facing query boundary for the model-visible tool."""

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult: ...


class WebSearchBackendResolver(Protocol):
    """Resolve only the explicitly configured WEB_SEARCH route."""

    def resolve(self, route: ModelRoute) -> HostedWebSearch | None: ...


__all__ = [
    "MAX_CITED_TEXT_CHARS",
    "MAX_DOMAIN_CHARS",
    "MAX_DOMAIN_COUNT",
    "MAX_EVIDENCE_CHARS",
    "MAX_MAX_SOURCES",
    "MAX_MODEL_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_SOURCE_COUNT",
    "MAX_SOURCE_PROVIDER_CHARS",
    "MAX_SOURCE_SNIPPET_CHARS",
    "MAX_SOURCE_TITLE_CHARS",
    "MAX_SOURCE_URL_CHARS",
    "MAX_TOTAL_RESULT_BYTES",
    "HostedWebSearch",
    "HostedWebSearchEvent",
    "HostedWebSearchEventSink",
    "WebSearchBackend",
    "WebSearchBackendResolver",
    "WebSearchCitation",
    "WebSearchError",
    "WebSearchErrorCode",
    "WebSearchExecutionPath",
    "WebSearchMode",
    "WebSearchQueryPort",
    "WebSearchRequest",
    "WebSearchResult",
    "WebSearchSource",
    "resolve_web_search_path",
]
