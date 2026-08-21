"""Stable application errors and typed provider failure facts.

Provider failure facts are intentionally separate from retry, circuit, and
failover policy. Adapters report what happened; the resilience boundary decides
what to do next.

稳定的应用错误与类型化 Provider 失败事实.

Provider 失败事实与重试、熔断和故障转移策略明确分离. 适配器只报告发生了什么,
由 resilience 边界决定下一步动作.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

from neuro_code.shared.redaction import redact_sensitive_text

_PROVIDER_DETAIL_LIMIT = 1_000
_PROVIDER_IDENTITY_LIMIT = 200
_MAX_RETRY_AFTER_SECONDS = 3_600.0


class ProviderFailureKind(StrEnum):
    """Typed facts describing why a model provider attempt failed."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_OVERFLOW = "context_overflow"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


class ProviderFailurePhase(StrEnum):
    """The bounded provider lifecycle phase where a failure was observed."""

    REQUEST = "request"
    RESPONSE_HEADERS = "response_headers"
    RESPONSE_BODY = "response_body"
    STREAM = "stream"
    PROTOCOL = "protocol"


def _safe_text(
    value: object,
    *,
    explicit_values: Iterable[str] = (),
    limit: int = _PROVIDER_DETAIL_LIMIT,
) -> str:
    raw = value if isinstance(value, str) else str(value)
    normalized = " ".join(raw.split())
    redacted = redact_sensitive_text(normalized, explicit_values=explicit_values)
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[: max(0, limit - 3)]}..."


def _safe_identity(value: object | None) -> str | None:
    if value is None:
        return None
    text = _safe_text(value, limit=_PROVIDER_IDENTITY_LIMIT)
    return text or None


def _bounded_status(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or not 100 <= value <= 599:
        return None
    return value


def _bounded_retry_after(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _structured_detail_text(detail: str) -> str:
    """Return safe structured provider fields used only for fact classification."""

    try:
        parsed: Any = json.loads(detail)
    except (TypeError, ValueError):
        return detail.casefold()

    values: list[str] = []

    def collect(value: object, *, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in {
                    "code",
                    "error",
                    "error_code",
                    "message",
                    "status",
                    "type",
                }:
                    collect(item, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:8]:
                collect(item, depth=depth + 1)
        elif isinstance(value, str):
            values.append(value.casefold())

    collect(parsed)
    return " ".join(values) or detail.casefold()


def _classify_http_failure(status_code: int, detail: str) -> ProviderFailureKind:
    """Map response status and structured provider codes to facts.

    This is an adapter-boundary classifier. Policy code never parses this text.
    """

    structured = _structured_detail_text(detail)
    if status_code == 401 or any(
        marker in structured for marker in ("authentication", "unauthenticated", "invalid_api_key")
    ):
        return ProviderFailureKind.AUTHENTICATION
    if status_code in {402, 403} or any(
        marker in structured
        for marker in ("authorization", "unauthorized", "forbidden", "permission")
    ):
        return ProviderFailureKind.AUTHORIZATION
    if status_code == 429 or any(
        marker in structured for marker in ("rate_limit", "ratelimit", "too_many_requests", "quota")
    ):
        return ProviderFailureKind.RATE_LIMIT
    if status_code == 404 or any(
        marker in structured for marker in ("model_not_found", "model not found", "unknown_model")
    ):
        return ProviderFailureKind.MODEL_NOT_FOUND
    if any(
        marker in structured
        for marker in (
            "context_length",
            "context length",
            "maximum context",
            "max_tokens",
            "too many tokens",
            "token limit",
        )
    ):
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if status_code == 408:
        return ProviderFailureKind.TIMEOUT
    if status_code == 413:
        return ProviderFailureKind.CONTEXT_OVERFLOW
    if status_code in {400, 409, 422}:
        return ProviderFailureKind.INVALID_REQUEST
    if 500 <= status_code <= 599:
        return ProviderFailureKind.SERVER
    return ProviderFailureKind.UNKNOWN


def _parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if headers is None:
        return None
    raw_value: str | None = None
    for key, value in headers.items():
        if str(key).casefold() == "retry-after":
            raw_value = value if isinstance(value, str) else str(value)
            break
    if raw_value is None:
        return None
    value = raw_value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return _bounded_retry_after(seconds)


def _transport_kind(error: BaseException) -> ProviderFailureKind:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return ProviderFailureKind.TIMEOUT
    error_type = type(error).__name__.casefold()
    if "timeout" in error_type:
        return ProviderFailureKind.TIMEOUT
    mro_types = {base.__name__.casefold() for base in type(error).__mro__}
    if isinstance(error, OSError) or any(
        name.endswith(("requesterror", "networkerror")) for name in mro_types
    ):
        return ProviderFailureKind.NETWORK
    return ProviderFailureKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """Bounded, redacted provider failure facts.

    ``retryable``, ``failover_allowed`` and ``counts_toward_circuit`` are
    intentionally absent. Those are policy decisions owned by the provider
    resilience boundary.
    """

    kind: ProviderFailureKind
    detail: str
    status_code: int | None = None
    retry_after_seconds: float | None = None
    provider: str | None = None
    model: str | None = None
    phase: ProviderFailurePhase | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _safe_text(self.detail))
        object.__setattr__(self, "status_code", _bounded_status(self.status_code))
        object.__setattr__(
            self,
            "retry_after_seconds",
            _bounded_retry_after(self.retry_after_seconds),
        )
        object.__setattr__(self, "provider", _safe_identity(self.provider))
        object.__setattr__(self, "model", _safe_identity(self.model))


class NeuroCodeError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(NeuroCodeError):
    """Configuration is missing, invalid, or contradictory."""


class ProviderError(NeuroCodeError):
    """A model provider failed or returned an invalid stream."""

    def __init__(self, message: str = "", *, failure: ProviderFailure | None = None) -> None:
        resolved = failure or ProviderFailure(ProviderFailureKind.UNKNOWN, message)
        self.failure = resolved
        super().__init__(resolved.detail)

    @classmethod
    def classified(
        cls,
        kind: ProviderFailureKind,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        phase: ProviderFailurePhase | None = None,
        redaction_values: Iterable[str] = (),
    ) -> ProviderError:
        return cls(
            failure=ProviderFailure(
                kind,
                _safe_text(detail, explicit_values=redaction_values),
                status_code,
                retry_after_seconds,
                provider,
                model,
                phase,
            )
        )

    @classmethod
    def from_http(
        cls,
        status_code: int,
        detail: str,
        *,
        headers: Mapping[str, str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        phase: ProviderFailurePhase = ProviderFailurePhase.RESPONSE_BODY,
        redaction_values: Iterable[str] = (),
    ) -> ProviderError:
        safe_detail = _safe_text(detail, explicit_values=redaction_values)
        visible_detail = f"HTTP {status_code}"
        if safe_detail:
            visible_detail = f"{visible_detail}: {safe_detail}"
        return cls.classified(
            _classify_http_failure(status_code, safe_detail),
            visible_detail,
            status_code=status_code,
            retry_after_seconds=_parse_retry_after(headers),
            provider=provider,
            model=model,
            phase=phase,
            redaction_values=redaction_values,
        )

    @classmethod
    def from_transport(
        cls,
        error: BaseException,
        *,
        provider: str | None = None,
        model: str | None = None,
        phase: ProviderFailurePhase = ProviderFailurePhase.STREAM,
        redaction_values: Iterable[str] = (),
        prefix: str | None = None,
    ) -> ProviderError:
        detail = (
            f"{type(error).__name__}: {_safe_text(str(error), explicit_values=redaction_values)}"
        )
        if prefix:
            detail = f"{prefix}: {detail}"
        return cls.classified(
            _transport_kind(error),
            detail,
            provider=provider,
            model=model,
            phase=phase,
            redaction_values=redaction_values,
        )

    @classmethod
    def protocol(
        cls,
        detail: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        phase: ProviderFailurePhase = ProviderFailurePhase.PROTOCOL,
        redaction_values: Iterable[str] = (),
    ) -> ProviderError:
        return cls.classified(
            ProviderFailureKind.PROTOCOL,
            detail,
            provider=provider,
            model=model,
            phase=phase,
            redaction_values=redaction_values,
        )

    @classmethod
    def failure_for_exception(
        cls,
        error: BaseException,
        *,
        provider: str | None = None,
        model: str | None = None,
        phase: ProviderFailurePhase = ProviderFailurePhase.STREAM,
        redaction_values: Iterable[str] = (),
    ) -> ProviderFailure:
        if isinstance(error, ProviderError):
            return error.failure
        return cls.from_transport(
            error,
            provider=provider,
            model=model,
            phase=phase,
            redaction_values=redaction_values,
        ).failure


class ToolError(NeuroCodeError):
    """A tool request is invalid or could not be completed."""


class BackgroundTaskCapacityError(ToolError):
    """A managed task supervisor cannot accept another task right now."""


class PermissionDenied(ToolError):
    """A tool call was rejected by the permission policy."""


class SandboxError(NeuroCodeError):
    """A requested operating-system sandbox could not be enforced."""


class TerminalError(NeuroCodeError):
    """An interactive terminal request or owned session failed."""


class SessionError(NeuroCodeError):
    """Session persistence or reconstruction failed."""


class SubagentTimeoutError(NeuroCodeError):
    """A bounded subagent run exceeded its wall-clock budget."""


__all__ = [
    "BackgroundTaskCapacityError",
    "ConfigurationError",
    "NeuroCodeError",
    "PermissionDenied",
    "ProviderError",
    "ProviderFailure",
    "ProviderFailureKind",
    "ProviderFailurePhase",
    "SandboxError",
    "SessionError",
    "SubagentTimeoutError",
    "TerminalError",
    "ToolError",
]
