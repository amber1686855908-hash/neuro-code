"""Shared test-only exact restricted-token attestation helpers."""

from __future__ import annotations

from typing import Any


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
    return bool(
        attestation.get("is_restricted") is True
        and tuple(restricted_sids or ()) == (expected_write_sid,)
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
