from __future__ import annotations

from tests.security.test_windows_native_workload_compatibility import _nul_mode_results
from tests.security.test_windows_w5_gate1_runtime_root_cause import _probe_result
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


def test_gate15_probe_projection_separates_profile_and_runtime_facts() -> None:
    assert _probe_result(
        "W5_GATE15_PROBE_STARTED\n"
        "W5_GATE15_TOKEN=PASS\n"
        "W5_GATE15_TOKEN_ERROR=0\n"
        "W5_GATE15_PROFILE_DIRECTORY=AVAILABLE\n"
        "W5_GATE15_PROFILE_DIRECTORY_ERROR=0\n"
        "W5_GATE15_TOKEN_USER=PASS\n"
        "W5_GATE15_TOKEN_USER_ERROR=0\n"
        "W5_GATE15_HKU_SID_STATUS=0\n"
        "W5_GATE15_HKU_SID=LOADED\n"
        "W5_GATE15_CURRENT_USER_STATUS=0\n"
        "W5_GATE15_CURRENT_USER=OPEN\n"
        "W5_GATE15_BCRYPT_LIBRARY=LOADED\n"
        "W5_GATE15_BCRYPT_LIBRARY_ERROR=0\n"
        "W5_GATE15_BCRYPT_MODULE_PATH=AVAILABLE\n"
        "W5_GATE15_BCRYPT_MODULE_PATH_ERROR=0\n"
        "W5_GATE15_BCRYPT_GEN_RANDOM_STATUS=0x00000000\n"
        "W5_GATE15_NCRYPT_OPEN_STATUS=0x00000000\n"
        "W5_GATE15_NUL_READ_CREATE=PASS\n"
        "W5_GATE15_NUL_READ_CREATE_ERROR=0\n"
        "W5_GATE15_NUL_READ_WRITE=NOT_ATTEMPTED\n"
        "W5_GATE15_NUL_READ_WRITE_ERROR=0\n"
        "W5_GATE15_PROBE_FINISHED\nsecret=discarded\n"
    ) == {
        "started": True,
        "finished": True,
        "token": "PASS",
        "token_error": 0,
        "profile_directory_available": True,
        "profile_directory_error": 0,
        "token_user": "PASS",
        "token_user_error": 0,
        "registry_hive_loaded": True,
        "registry_hive_status": 0,
        "current_user_open": True,
        "current_user_status": 0,
        "bcrypt_library": "LOADED",
        "bcrypt_library_error": 0,
        "bcrypt_module_path": "AVAILABLE",
        "bcrypt_module_path_error": 0,
        "bcrypt_gen_random_status": 0,
        "ncrypt_open_status": 0,
        "nul": {
            "read": {
                "create": "PASS",
                "create_error": 0,
                "write": "NOT_ATTEMPTED",
                "write_error": 0,
            },
            "write": {
                "create": "UNKNOWN",
                "create_error": None,
                "write": "UNKNOWN",
                "write_error": None,
            },
            "read_write": {
                "create": "UNKNOWN",
                "create_error": None,
                "write": "UNKNOWN",
                "write_error": None,
            },
        },
    }


def test_gate15_probe_projection_distinguishes_directory_and_hive() -> None:
    assert _probe_result(
        "W5_GATE15_PROBE_STARTED\n"
        "W5_GATE15_PROFILE_DIRECTORY=AVAILABLE\n"
        "W5_GATE15_PROFILE_DIRECTORY_ERROR=0\n"
        "W5_GATE15_HKU_SID_STATUS=2\n"
        "W5_GATE15_HKU_SID=NOT_LOADED\n"
        "W5_GATE15_CURRENT_USER_STATUS=5\n"
        "W5_GATE15_CURRENT_USER=FAILED\n"
    ) == {
        "started": True,
        "finished": False,
        "token": "UNKNOWN",
        "token_error": None,
        "profile_directory_available": True,
        "profile_directory_error": 0,
        "token_user": "UNKNOWN",
        "token_user_error": None,
        "registry_hive_loaded": False,
        "registry_hive_status": 2,
        "current_user_open": False,
        "current_user_status": 5,
        "bcrypt_library": "UNKNOWN",
        "bcrypt_library_error": None,
        "bcrypt_module_path": "UNKNOWN",
        "bcrypt_module_path_error": None,
        "bcrypt_gen_random_status": None,
        "ncrypt_open_status": None,
        "nul": {
            "read": {
                "create": "UNKNOWN",
                "create_error": None,
                "write": "UNKNOWN",
                "write_error": None,
            },
            "write": {
                "create": "UNKNOWN",
                "create_error": None,
                "write": "UNKNOWN",
                "write_error": None,
            },
            "read_write": {
                "create": "UNKNOWN",
                "create_error": None,
                "write": "UNKNOWN",
                "write_error": None,
            },
        },
    }
