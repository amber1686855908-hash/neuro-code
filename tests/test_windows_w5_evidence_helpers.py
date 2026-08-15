from __future__ import annotations

from tests.security.test_windows_native_workload_compatibility import _nul_mode_results
from tests.security.windows_token_attestation import token_attestation_is_exact


def _diagnostic(
    *, user: str = "S-1-5-21-online", restricted: tuple[str, ...] = ("S-1-5-21-write",)
) -> dict[str, object]:
    return {
        "security_attestation": {
            "user_sid": user,
            "is_restricted": True,
            "restricted_sids": restricted,
            "change_notify_privilege_enabled": True,
            "unexpected_enabled_privilege_count": 0,
        }
    }


def test_token_attestation_requires_exact_online_identity_and_write_sid() -> None:
    assert token_attestation_is_exact(
        _diagnostic(),
        expected_user_sid="S-1-5-21-online",
        expected_write_sid="S-1-5-21-write",
    )
    assert not token_attestation_is_exact(
        _diagnostic(restricted=("S-1-5-21-write", "S-1-1-0")),
        expected_user_sid="S-1-5-21-online",
        expected_write_sid="S-1-5-21-write",
    )
    assert not token_attestation_is_exact(
        _diagnostic(user="S-1-5-21-controller"),
        expected_user_sid="S-1-5-21-online",
        expected_write_sid="S-1-5-21-write",
    )


def test_nul_mode_projection_preserves_each_access_shape() -> None:
    output = (
        b'W5_NUL_DIRECT={"read":{"create":"PASS","create_error":0,'
        b'"write":"NOT_ATTEMPTED","write_error":0},'
        b'"write":{"create":"FAIL","create_error":5,'
        b'"write":"NOT_ATTEMPTED","write_error":0},'
        b'"read_write":{"create":"FAIL","create_error":5,'
        b'"write":"NOT_ATTEMPTED","write_error":0}}\n'
    )
    assert _nul_mode_results(output, b"") == {
        "read": {
            "create": "PASS",
            "create_error": 0,
            "write": "NOT_ATTEMPTED",
            "write_error": 0,
        },
        "write": {
            "create": "FAIL",
            "create_error": 5,
            "write": "NOT_ATTEMPTED",
            "write_error": 0,
        },
        "read_write": {
            "create": "FAIL",
            "create_error": 5,
            "write": "NOT_ATTEMPTED",
            "write_error": 0,
        },
    }


def test_nul_mode_projection_tolerates_pty_wrapping() -> None:
    output = (
        b'\x1b[?25lW5_NUL_DIRECT={"read":{"create":"PASS"},'
        b'"write":{"create":"FAIL","create_error":5},'
        b'"read_write":{"create":"FAIL","create_error":5}}'
        b"\x1b[?25h\r\n"
    )
    assert _nul_mode_results(output, b"\x1b[2K") == {
        "read": {"create": "PASS"},
        "write": {"create": "FAIL", "create_error": 5},
        "read_write": {"create": "FAIL", "create_error": 5},
    }
