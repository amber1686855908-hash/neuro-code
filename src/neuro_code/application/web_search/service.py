"""Application service for the explicitly configured WEB_SEARCH route.

WEB_SEARCH 路由的应用服务.

This service owns route selection and the capability gate.  Concrete Responses
adapters are supplied by a resolver at the composition boundary; the service
never imports or names a provider implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from neuro_code.application.ports.model import CapabilityStatus, ModelCapability
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import (
    HostedWebSearch,
    HostedWebSearchEventSink,
    WebSearchBackendResolver,
    WebSearchCitation,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResult,
    WebSearchSource,
)
from neuro_code.shared.redaction import redact_sensitive_text, redact_sensitive_value


class WebSearchRouteSource(Protocol):
    """The small route-reading surface required by WebSearchService."""

    def route(self, role: RuntimeRole) -> ModelRoute | None: ...


class WebSearchService:
    """Execute one bounded search against the configured WEB_SEARCH route."""

    def __init__(
        self,
        routes: WebSearchRouteSource,
        resolver: WebSearchBackendResolver,
        *,
        redaction_values: tuple[str, ...] = (),
    ) -> None:
        self._routes = routes
        self._resolver = resolver
        self._redaction_values = tuple(redaction_values)

    def _safe_request(self, request: WebSearchRequest) -> WebSearchRequest:
        query = redact_sensitive_text(
            request.query,
            explicit_values=self._redaction_values,
        )
        if not query.strip():
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_INVALID_REQUEST,
                "web search query is empty after redaction",
            )
        return replace(request, query=query)

    def _safe_result(
        self,
        result: WebSearchResult,
        *,
        request: WebSearchRequest,
        backend: HostedWebSearch,
    ) -> WebSearchResult:
        """Apply the same secret boundary to canonical provider evidence."""

        safe_evidence = redact_sensitive_text(
            result.evidence_text,
            explicit_values=self._redaction_values,
        )
        evidence_redacted = safe_evidence != result.evidence_text
        safe_sources: list[WebSearchSource] = []
        safe_urls: set[str] = set()
        for source in result.sources:
            safe_url = redact_sensitive_text(source.url, explicit_values=self._redaction_values)
            if safe_url != source.url:
                continue
            safe_source = replace(
                source,
                title=redact_sensitive_text(
                    source.title,
                    explicit_values=self._redaction_values,
                ),
                snippet=(
                    redact_sensitive_text(
                        source.snippet,
                        explicit_values=self._redaction_values,
                    )
                    if source.snippet is not None
                    else None
                ),
            )
            safe_sources.append(safe_source)
            safe_urls.add(source.url.casefold())

        safe_citations: list[WebSearchCitation] = []
        for citation in result.citations:
            safe_url = redact_sensitive_text(
                citation.url,
                explicit_values=self._redaction_values,
            )
            if safe_url != citation.url or citation.url.casefold() not in safe_urls:
                continue
            safe_cited_text = (
                redact_sensitive_text(
                    citation.cited_text,
                    explicit_values=self._redaction_values,
                )
                if citation.cited_text is not None
                else None
            )
            preserve_span = (
                not evidence_redacted
                and safe_cited_text == citation.cited_text
                and (
                    citation.start is None
                    or (citation.end is not None and citation.end <= len(safe_evidence))
                )
            )
            safe_citations.append(
                replace(
                    citation,
                    title=redact_sensitive_text(
                        citation.title,
                        explicit_values=self._redaction_values,
                    ),
                    cited_text=safe_cited_text,
                    start=citation.start if preserve_span else None,
                    end=citation.end if preserve_span else None,
                )
            )

        safe_metadata_value = redact_sensitive_value(
            result.metadata,
            explicit_values=self._redaction_values,
        )
        safe_metadata = (
            dict(safe_metadata_value) if isinstance(safe_metadata_value, Mapping) else None
        )

        return WebSearchResult(
            query=request.query,
            evidence_text=safe_evidence,
            sources=tuple(safe_sources),
            citations=tuple(safe_citations),
            provider_profile=redact_sensitive_text(
                backend.provider_profile,
                explicit_values=self._redaction_values,
            ),
            model=redact_sensitive_text(
                backend.model,
                explicit_values=self._redaction_values,
            ),
            truncated=result.truncated,
            metadata=safe_metadata,
        )

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        safe_request = self._safe_request(request)
        route = self._routes.route(RuntimeRole.WEB_SEARCH)
        if route is None:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "WEB_SEARCH route is not configured",
            )
        if route.role is not RuntimeRole.WEB_SEARCH:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_INVALID_REQUEST,
                "WEB_SEARCH route has an invalid runtime role",
            )
        backend = self._resolver.resolve(route)
        if backend is None:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "WEB_SEARCH route has no executable hosted-search backend",
            )
        if (
            backend.capabilities.status(ModelCapability.HOSTED_WEB_SEARCH)
            is not CapabilityStatus.SUPPORTED
        ):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNSUPPORTED,
                "WEB_SEARCH backend does not explicitly support hosted search",
            )
        try:
            result = await backend.search(safe_request, event_sink=event_sink)
        except WebSearchError:
            raise
        except Exception as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_PROVIDER_ERROR,
                f"hosted search failed: {type(error).__name__}",
            ) from error
        return self._safe_result(result, request=safe_request, backend=backend)


__all__ = ["WebSearchRouteSource", "WebSearchService"]
