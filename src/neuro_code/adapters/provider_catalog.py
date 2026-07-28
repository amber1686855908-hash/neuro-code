from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.domain.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.shared.redaction import redact_sensitive_text

_MAX_RESPONSE_BYTES = 1_048_576
_MAX_MODELS = 200
_MAX_MODEL_CHARACTERS = 512


def _without_known_operation(base_url: str) -> str:
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if base_url.endswith(suffix):
            return base_url.removesuffix(suffix)
    return base_url


def _catalog_endpoint(spec: ProviderConnectionSpec) -> str:
    base_url = _without_known_operation(spec.base_url)
    if spec.protocol == "anthropic-messages":
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return f"{base_url}/models"
    if spec.protocol == "gemini-generate-content":
        if not base_url.endswith(("/v1", "/v1beta")):
            base_url = f"{base_url}/v1beta"
        return f"{base_url}/models"
    return f"{base_url}/models"


def _headers(spec: ProviderConnectionSpec) -> dict[str, str]:
    headers = {"accept": "application/json", "user-agent": "neuro-code/provider-catalog"}
    if spec.protocol == "anthropic-messages":
        headers.update(
            {
                "anthropic-version": "2023-06-01",
                "x-api-key": spec.api_key,
            }
        )
    elif spec.protocol == "gemini-generate-content":
        headers["x-goog-api-key"] = spec.api_key
    else:
        headers["authorization"] = f"Bearer {spec.api_key}"
    return headers


async def _bounded_body(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise ProviderCatalogError("response_too_large")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ProviderCatalogError("response_too_large")
    return bytes(body)


def _model_entries(payload: object) -> Sequence[object]:
    if isinstance(payload, Mapping):
        entries = payload.get("models")
        if entries is None:
            entries = payload.get("data")
    else:
        entries = payload
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        raise ProviderCatalogError("invalid_response")
    return entries


def _model_identifier(entry: object, *, gemini: bool) -> str | None:
    if isinstance(entry, str):
        identifier = entry
    elif isinstance(entry, Mapping):
        if gemini:
            methods = entry.get("supportedGenerationMethods")
            if (
                isinstance(methods, Sequence)
                and not isinstance(methods, (str, bytes, bytearray))
                and not {"generateContent", "streamGenerateContent"}.intersection(
                    method for method in methods if isinstance(method, str)
                )
            ):
                return None
        candidate = entry.get("name") if gemini else entry.get("id", entry.get("name"))
        if not isinstance(candidate, str):
            return None
        identifier = candidate
    else:
        return None
    identifier = identifier.strip()
    if gemini:
        identifier = identifier.removeprefix("models/")
    if (
        not identifier
        or len(identifier) > _MAX_MODEL_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in identifier)
    ):
        return None
    return identifier


def _parse_catalog(payload: object, *, gemini: bool) -> ProviderCatalogResult:
    entries = _model_entries(payload)
    identifiers = {
        identifier
        for entry in entries
        if (identifier := _model_identifier(entry, gemini=gemini)) is not None
    }
    if entries and not identifiers:
        raise ProviderCatalogError("invalid_response")
    ordered = sorted(identifiers, key=str.casefold)
    return ProviderCatalogResult(
        tuple(ordered[:_MAX_MODELS]),
        truncated=len(ordered) > _MAX_MODELS,
    )


def _http_error(status_code: int) -> ProviderCatalogError:
    if status_code in {401, 403}:
        kind = "authentication"
    elif status_code in {404, 405}:
        kind = "endpoint"
    elif status_code in {408, 504}:
        kind = "timeout"
    elif status_code == 429:
        kind = "rate_limit"
    elif status_code >= 500:
        kind = "server"
    else:
        kind = "http"
    return ProviderCatalogError(kind, status_code=status_code)


class HttpProviderCatalog:
    """Bounded, read-only model discovery over each provider's catalog endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        transport: Any | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult:
        endpoint = _catalog_endpoint(spec)
        try:
            options = http_policy.client_options(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
            async with (
                httpx.AsyncClient(**options) as client,
                client.stream(
                    "GET",
                    endpoint,
                    headers=_headers(spec),
                ) as response,
            ):
                if response.status_code >= 400:
                    raise _http_error(response.status_code)
                raw_body = await _bounded_body(response)
        except ProviderCatalogError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderCatalogError("timeout") from error
        except httpx.ProxyError as error:
            detail = http_policy.redact(str(error), spec.api_key, limit=300)
            raise ProviderCatalogError(
                "proxy",
                detail=redact_sensitive_text(detail, explicit_values=(spec.api_key,)),
            ) from error
        except httpx.RequestError as error:
            detail = http_policy.redact(str(error), spec.api_key, limit=300)
            raise ProviderCatalogError(
                "network",
                detail=redact_sensitive_text(detail, explicit_values=(spec.api_key,)),
            ) from error
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderCatalogError("invalid_response") from error
        return _parse_catalog(
            payload,
            gemini=spec.protocol == "gemini-generate-content",
        )


__all__ = ["HttpProviderCatalog"]
