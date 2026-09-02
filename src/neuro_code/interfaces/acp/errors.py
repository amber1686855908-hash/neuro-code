"""ACP request errors and external session identity validation.

ACP 请求错误和外部 session identity 校验.

This module owns the stable protocol error projections shared by the ACP
controllers. It deliberately contains no session state or application
orchestration.
"""

from __future__ import annotations

from acp.exceptions import RequestError

from neuro_code.interfaces.acp.serialization import _bounded_identifier

MAX_SESSION_ID_BYTES = 512

SESSION_NOT_ACTIVE = -32001
SESSION_NOT_FOUND = -32002
SESSION_BUSY = -32003


def invalid_params(reason: str, details: str | None = None) -> RequestError:
    data = {"reason": reason}
    if details is not None:
        data["details"] = details
    return RequestError.invalid_params(data)


def session_not_active(session_id: str) -> RequestError:
    return RequestError(
        SESSION_NOT_ACTIVE,
        "Session not active",
        {"reason": "session_not_active", "sessionId": _bounded_identifier(session_id)},
    )


def session_not_found(session_id: str) -> RequestError:
    return RequestError(
        SESSION_NOT_FOUND,
        "Session not found",
        {"reason": "session_not_found", "sessionId": _bounded_identifier(session_id)},
    )


def session_busy(session_id: str, reason: str) -> RequestError:
    return RequestError(
        SESSION_BUSY,
        "Session is busy",
        {"reason": reason, "sessionId": _bounded_identifier(session_id)},
    )


def validated_session_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise invalid_params("session_id_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise invalid_params("session_id_invalid")
    if len(value.encode("utf-8")) > MAX_SESSION_ID_BYTES:
        raise invalid_params("session_id_too_large")
    return value


__all__ = [
    "MAX_SESSION_ID_BYTES",
    "SESSION_BUSY",
    "SESSION_NOT_ACTIVE",
    "SESSION_NOT_FOUND",
    "invalid_params",
    "session_busy",
    "session_not_active",
    "session_not_found",
    "validated_session_id",
]
