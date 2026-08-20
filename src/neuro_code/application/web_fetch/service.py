"""Application service for bounded local Web Fetch.

有界本地 Web Fetch 应用服务.

The service owns the credential boundary and result projection.  The concrete
HTTP client is injected at composition time, so this module never chooses a
proxy, performs DNS, or knows a provider wire protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import unquote

from neuro_code.application.ports.web_fetch import (
    WebFetchBackend,
    WebFetchError,
    WebFetchErrorCode,
    WebFetchQueryPort,
    WebFetchRequest,
    WebFetchResult,
)
from neuro_code.shared.redaction import redact_sensitive_text, redact_sensitive_value


class WebFetchService:
    """Execute one local fetch and return only redacted bounded text."""

    def __init__(
        self,
        backend: WebFetchBackend,
        *,
        redaction_values: tuple[str, ...] = (),
    ) -> None:
        self._backend = backend
        self._redaction_values = tuple(redaction_values)

    def _contains_secret(self, value: str) -> bool:
        decoded = unquote(value)
        return any(
            secret and (secret in value or secret in decoded) for secret in self._redaction_values
        )

    def _safe_request(self, request: WebFetchRequest) -> WebFetchRequest:
        if self._contains_secret(request.url):
            # Do not redact and send the URL: the fetch boundary must fail
            # closed before DNS, TCP, or TLS can observe it.
            raise WebFetchError(
                WebFetchErrorCode.SECRET_IN_URL,
                "fetch URL contains a configured secret",
            )
        return request

    def _safe_result(self, result: WebFetchResult) -> WebFetchResult:
        if self._contains_secret(result.requested_url) or self._contains_secret(result.final_url):
            raise WebFetchError(
                WebFetchErrorCode.SECRET_IN_URL,
                "fetch result URL contains a configured secret",
            )
        safe_title = redact_sensitive_text(
            result.title,
            explicit_values=self._redaction_values,
        )
        safe_content = redact_sensitive_text(
            result.content,
            explicit_values=self._redaction_values,
        )
        safe_metadata_value = redact_sensitive_value(
            result.metadata,
            explicit_values=self._redaction_values,
        )
        safe_metadata = (
            dict(safe_metadata_value) if isinstance(safe_metadata_value, Mapping) else None
        )
        return replace(
            result,
            title=safe_title,
            content=safe_content,
            metadata=safe_metadata,
        )

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        safe_request = self._safe_request(request)
        try:
            result = await self._backend.fetch(safe_request)
        except WebFetchError:
            raise
        except Exception as error:
            # A backend implementation must not leak response details or
            # credentials through an untyped exception boundary.
            raise WebFetchError(
                WebFetchErrorCode.HTTP_ERROR,
                f"local web fetch failed: {type(error).__name__}",
            ) from error
        return self._safe_result(result)


__all__ = ["WebFetchQueryPort", "WebFetchService"]
