"""Provider-owned structured failure classification.

The shared error module owns only conservative HTTP fallback facts.  This
module owns exact, bounded envelope fields for the protocols implemented by
Neuro Code; human-readable messages are deliberately ignored.

Provider 结构化失败分类.

共享错误模块只负责保守的 HTTP fallback; 本模块负责已实现协议的精确、有限 envelope
字段, 故意不把人类可读 message 当作策略依据.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum

from neuro_code.shared.errors import ProviderFailureKind


class ProviderFailureProtocol(StrEnum):
    """Wire protocols with independently documented failure envelopes."""

    OPENAI_COMPATIBLE = "openai-compatible"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC = "anthropic"
    GEMINI_GENERATE_CONTENT = "gemini-generate-content"
    GEMINI_INTERACTIONS = "gemini-interactions"


_STRUCTURED_KEYS = frozenset({"code", "type", "status", "reason", "error_code", "error_type"})
_NESTED_KEYS = frozenset({"error", "errors", "details", "cause", "response", "data"})
_MAX_VALUES = 64


def _structured_values(detail: str) -> frozenset[str]:
    """Extract bounded exact code/type/status/reason values from JSON."""

    try:
        parsed: object = json.loads(detail)
    except (TypeError, ValueError):
        return frozenset()

    values: list[str] = []

    def collect(value: object, *, depth: int = 0) -> None:
        if depth > 6 or len(values) >= _MAX_VALUES:
            return
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).casefold()
                if key in _STRUCTURED_KEYS and isinstance(child, str):
                    values.append(child.casefold())
                if key in _STRUCTURED_KEYS or key in _NESTED_KEYS:
                    collect(child, depth=depth + 1)
                elif isinstance(child, (Mapping, list, tuple)):
                    # Walk unknown containers to find a nested structured
                    # field, but never collect arbitrary string values.
                    collect(child, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value[:8]:
                collect(child, depth=depth + 1)

    collect(parsed)
    return frozenset(values)


def _first(values: frozenset[str], candidates: frozenset[str]) -> str | None:
    for candidate in candidates:
        if candidate in values:
            return candidate
    return None


def _classify_openai(values: frozenset[str]) -> ProviderFailureKind | None:
    if _first(
        values,
        frozenset(
            {
                "credit_balance_exhausted",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
                "insufficient_quota",
            }
        ),
    ):
        return ProviderFailureKind.AUTHORIZATION
    if _first(values, frozenset({"authentication_error", "invalid_api_key"})):
        return ProviderFailureKind.AUTHENTICATION
    if _first(values, frozenset({"permission_error", "authorization_error"})):
        return ProviderFailureKind.AUTHORIZATION
    if _first(values, frozenset({"rate_limit_exceeded", "rate_limit_error"})):
        return ProviderFailureKind.RATE_LIMIT
    if _first(values, frozenset({"model_not_found", "unknown_model"})):
        return ProviderFailureKind.MODEL_NOT_FOUND
    if _first(values, frozenset({"context_length_exceeded"})):
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if _first(values, frozenset({"server_error", "api_error"})):
        return ProviderFailureKind.SERVER
    if _first(values, frozenset({"invalid_request_error", "invalid_prompt"})):
        return ProviderFailureKind.INVALID_REQUEST
    return None


def _classify_anthropic(values: frozenset[str]) -> ProviderFailureKind | None:
    if _first(values, frozenset({"authentication_error"})):
        return ProviderFailureKind.AUTHENTICATION
    if _first(values, frozenset({"billing_error", "permission_error"})):
        return ProviderFailureKind.AUTHORIZATION
    if _first(values, frozenset({"invalid_request_error", "request_too_large"})):
        return ProviderFailureKind.INVALID_REQUEST
    # Anthropic documents that this envelope can also represent a monthly
    # spend cap and may omit Retry-After. Keep it unknown until the response
    # contains an unambiguous transient signal rather than retrying billing.
    if _first(values, frozenset({"rate_limit_error"})):
        return ProviderFailureKind.UNKNOWN
    if _first(values, frozenset({"api_error", "overloaded_error"})):
        return ProviderFailureKind.SERVER
    if _first(values, frozenset({"timeout_error"})):
        return ProviderFailureKind.TIMEOUT
    return None


def _classify_gemini_generate(values: frozenset[str]) -> ProviderFailureKind | None:
    if _first(values, frozenset({"api_key_invalid"})):
        return ProviderFailureKind.AUTHENTICATION
    if _first(values, frozenset({"permission_denied", "failed_precondition"})):
        return ProviderFailureKind.AUTHORIZATION
    if _first(values, frozenset({"invalid_argument", "not_found", "out_of_range"})):
        return ProviderFailureKind.INVALID_REQUEST
    # RESOURCE_EXHAUSTED covers RPM/TPM/RPD and spend/quota in the official
    # GenerateContent contract; it is intentionally not made retryable here.
    if _first(values, frozenset({"resource_exhausted"})):
        return ProviderFailureKind.UNKNOWN
    if _first(values, frozenset({"deadline_exceeded"})):
        return ProviderFailureKind.TIMEOUT
    if _first(values, frozenset({"internal", "unavailable", "aborted"})):
        return ProviderFailureKind.SERVER
    return None


def _classify_gemini_interactions(values: frozenset[str]) -> ProviderFailureKind | None:
    if _first(values, frozenset({"authentication"})):
        return ProviderFailureKind.AUTHENTICATION
    if _first(values, frozenset({"permission_denied", "failed_precondition", "quota_exceeded"})):
        return ProviderFailureKind.AUTHORIZATION
    if _first(values, frozenset({"model_not_found"})):
        return ProviderFailureKind.MODEL_NOT_FOUND
    if _first(
        values,
        frozenset(
            {
                "invalid_request",
                "parameter_unknown",
                "out_of_range",
                "not_found",
                "already_exists",
                "unimplemented",
            }
        ),
    ):
        return ProviderFailureKind.INVALID_REQUEST
    if _first(values, frozenset({"rate_limit_exceeded"})):
        return ProviderFailureKind.RATE_LIMIT
    if _first(values, frozenset({"deadline_exceeded"})):
        return ProviderFailureKind.TIMEOUT
    if _first(values, frozenset({"api_error", "service_unavailable", "aborted"})):
        return ProviderFailureKind.SERVER
    return None


def classify_provider_failure(
    protocol: ProviderFailureProtocol,
    detail: str,
) -> ProviderFailureKind | None:
    """Classify an exact provider envelope, or return ``None`` for fallback.

    ``None`` is meaningful: the caller must retain the generic HTTP fallback
    rather than treating an unrecognized structured value as a server outage.
    """

    values = _structured_values(detail)
    if not values:
        return None
    classifier = {
        ProviderFailureProtocol.OPENAI_COMPATIBLE: _classify_openai,
        ProviderFailureProtocol.OPENAI_RESPONSES: _classify_openai,
        ProviderFailureProtocol.ANTHROPIC: _classify_anthropic,
        ProviderFailureProtocol.GEMINI_GENERATE_CONTENT: _classify_gemini_generate,
        ProviderFailureProtocol.GEMINI_INTERACTIONS: _classify_gemini_interactions,
    }.get(protocol)
    return classifier(values) if classifier is not None else None


__all__ = ["ProviderFailureProtocol", "classify_provider_failure"]
