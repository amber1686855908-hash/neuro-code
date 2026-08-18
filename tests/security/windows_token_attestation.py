"""Shared test-only exact restricted-token attestation helpers."""

from __future__ import annotations

import re
from typing import Any

_WINDOWS_LOGON_SID = re.compile(r"^S-1-5-5-\d+-\d+$")
_WORLD_SID = "S-1-1-0"


def token_attestation_is_exact(
    diagnostic: object,
    *,
    expected_user_sid: str | None,
    expected_write_sid: str,
) -> bool:
    """Return whether diagnostics match the frozen W1-W4 token contract."""

    if not isinstance(diagnostic, dict):
        return False
    attestation = diagnostic.get("security_attestation")
    if not isinstance(attestation, dict):
        return False
    if expected_user_sid is not None and attestation.get("user_sid") != expected_user_sid:
        return False
    restricted_sids = attestation.get("restricted_sids")
    user_sid = attestation.get("user_sid")
    if not isinstance(user_sid, str):
        return False
    restricting = tuple(restricted_sids or ())
    expected_restricting = (
        expected_write_sid,
        user_sid if expected_user_sid is None else expected_user_sid,
    )
    if len(restricting) != 4 or restricting[:2] != expected_restricting:
        return False
    if not isinstance(restricting[2], str) or _WINDOWS_LOGON_SID.fullmatch(restricting[2]) is None:
        return False
    if restricting[3] != _WORLD_SID:
        return False
    return bool(
        attestation.get("is_restricted") is True
        and attestation.get("change_notify_privilege_enabled") is True
        and attestation.get("unexpected_enabled_privilege_count") == 0
    )


def token_attestation_projection(diagnostic: object) -> dict[str, Any] | None:
    """Project only non-secret token facts into evidence artifacts."""

    if not isinstance(diagnostic, dict):
        return None
    attestation = diagnostic.get("security_attestation")
    if not isinstance(attestation, dict):
        return None
    return {
        key: attestation.get(key)
        for key in (
            "user_sid",
            "is_restricted",
            "restricted_sids",
            "change_notify_privilege_enabled",
            "unexpected_enabled_privilege_count",
        )
    }
