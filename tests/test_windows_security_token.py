from __future__ import annotations

import os
import unittest

from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_security_token import (
    WindowsRestrictedToken,
    WindowsRestrictedTokenRequest,
    WindowsTokenError,
    WindowsTokenInspection,
    inspect_windows_process_token,
)


class _FakeSecurityTokenApi:
    def __init__(self) -> None:
        self.source_handle = 101
        self.created_handle = 202
        self.child_token_handle = 303
        self.closed: list[int] = []
        self.create_calls: list[tuple[int, int, tuple[SyntheticWindowsSid, ...]]] = []
        sid = SyntheticWindowsSid.from_components((11, 22, 33, 44))
        self.inspection = WindowsTokenInspection(
            restricted_sid_count=1,
            is_restricted=True,
            user_sid="S-1-5-21-10-20-30-40",
            privilege_count=1,
            enabled_privilege_count=1,
            restricted_sids=(sid.value,),
            change_notify_privilege_enabled=True,
        )
        self.fail_create = False
        self.fail_inspect = False
        self.fail_close = False
        self.default_dacl_sids: tuple[str, ...] | None = None

    def open_current_process_token(self) -> int:
        return self.source_handle

    def open_process_token(self, process_handle: int) -> int:
        if process_handle != 404:
            raise WindowsTokenError("OpenProcessToken(actual child hProcess)", 6)
        return self.child_token_handle

    def create_restricted_token(
        self,
        existing_handle: int,
        flags: int,
        restricted_sids: tuple[SyntheticWindowsSid, ...],
    ) -> int:
        self.create_calls.append((existing_handle, flags, restricted_sids))
        if self.fail_create:
            raise WindowsTokenError("CreateRestrictedToken", 5)
        return self.created_handle

    def inspect_token(self, token_handle: int) -> WindowsTokenInspection:
        if self.fail_inspect:
            raise WindowsTokenError("GetTokenInformation", 5)
        return self.inspection

    def set_default_dacl(self, token_handle: int, sid_texts: tuple[str, ...]) -> None:
        self.default_dacl_sids = sid_texts

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)
        if self.fail_close:
            raise WindowsTokenError("CloseHandle", 6)


class WindowsRestrictedTokenRequestTests(unittest.TestCase):
    def test_write_restricted_request_sets_all_w1_flags(self) -> None:
        sid = SyntheticWindowsSid.from_components((1, 2, 3, 4))
        request = WindowsRestrictedTokenRequest((sid,))
        self.assertEqual(request.flags, 0x1 | 0x4 | 0x8)
        self.assertEqual(request.restricted_sids, (sid,))

    def test_write_restricted_request_disables_maximum_privilege(self) -> None:
        request = WindowsRestrictedTokenRequest(
            (SyntheticWindowsSid.from_components((1, 2, 3, 4)),)
        )
        self.assertNotEqual(request.flags & 0x1, 0)

    def test_write_restricted_requires_a_restricted_sid(self) -> None:
        with self.assertRaises(ValueError):
            WindowsRestrictedTokenRequest(())

    def test_write_restricted_rejects_multiple_restricting_sids(self) -> None:
        with self.assertRaises(ValueError):
            WindowsRestrictedTokenRequest(
                (
                    SyntheticWindowsSid.from_components((1, 2, 3, 4)),
                    SyntheticWindowsSid.from_components((5, 6, 7, 8)),
                )
            )

    def test_non_write_token_can_have_no_restricted_sids(self) -> None:
        request = WindowsRestrictedTokenRequest((), write_restricted=False)
        self.assertEqual(request.flags, 0x1 | 0x4)


class WindowsRestrictedTokenTests(unittest.TestCase):
    def test_creation_attests_token_and_closes_source_handle(self) -> None:
        api = _FakeSecurityTokenApi()
        sid = SyntheticWindowsSid.from_components((11, 22, 33, 44))
        request = WindowsRestrictedTokenRequest((sid,))

        token = WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(token.handle, api.created_handle)
        self.assertEqual(token.inspection.restricted_sid_count, 1)
        self.assertEqual(token.inspection.restricted_sids, (sid.value,))
        self.assertTrue(token.inspection.is_restricted)
        self.assertTrue(token.inspection.change_notify_privilege_enabled)
        self.assertEqual(api.closed, [api.source_handle])
        self.assertEqual(
            api.create_calls,
            [(api.source_handle, request.flags, (sid,))],
        )
        token.close()
        token.close()
        self.assertEqual(api.closed, [api.source_handle, api.created_handle])

    def test_runtime_can_set_bounded_default_dacl(self) -> None:
        api = _FakeSecurityTokenApi()
        token = WindowsRestrictedToken.create_from_current_process(
            WindowsRestrictedTokenRequest((SyntheticWindowsSid.from_components((11, 22, 33, 44)),)),
            api=api,
        )
        token.set_default_dacl(("S-1-5-5-0-1", "S-1-1-0"))
        self.assertEqual(api.default_dacl_sids, ("S-1-5-5-0-1", "S-1-1-0"))
        token.close()

    def test_inspection_failure_closes_created_and_source_handles(self) -> None:
        api = _FakeSecurityTokenApi()
        api.fail_inspect = True
        request = WindowsRestrictedTokenRequest(
            (SyntheticWindowsSid.from_components((11, 22, 33, 44)),)
        )
        with self.assertRaises(WindowsTokenError):
            WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(api.closed, [api.source_handle, api.created_handle])

    def test_create_failure_closes_source_handle(self) -> None:
        api = _FakeSecurityTokenApi()
        api.fail_create = True
        request = WindowsRestrictedTokenRequest(
            (SyntheticWindowsSid.from_components((11, 22, 33, 44)),)
        )
        with self.assertRaises(WindowsTokenError):
            WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(api.closed, [api.source_handle])

    def test_close_marks_handle_closed_even_when_close_fails(self) -> None:
        api = _FakeSecurityTokenApi()
        token = WindowsRestrictedToken(
            api.created_handle,
            WindowsRestrictedTokenRequest((SyntheticWindowsSid.from_components((11, 22, 33, 44)),)),
            api.inspection,
            api,
        )
        api.fail_close = True
        with self.assertRaises(WindowsTokenError):
            token.close()
        with self.assertRaises(WindowsTokenError):
            _ = token.handle

    def test_actual_process_inspection_uses_query_handle_and_closes_it(self) -> None:
        api = _FakeSecurityTokenApi()
        inspection = inspect_windows_process_token(404, api=api)
        self.assertEqual(inspection.user_sid, "S-1-5-21-10-20-30-40")
        self.assertEqual(api.closed, [api.child_token_handle])

    def test_actual_process_open_failure_is_preserved(self) -> None:
        api = _FakeSecurityTokenApi()
        with self.assertRaisesRegex(WindowsTokenError, "actual child hProcess") as raised:
            inspect_windows_process_token(405, api=api)
        self.assertEqual(raised.exception.error_code, 6)
        self.assertEqual(api.closed, [])

    @unittest.skipUnless(os.name == "nt", "native restricted-token attestation requires Windows")
    def test_native_restricted_token_has_restricted_sid(self) -> None:
        request = WindowsRestrictedTokenRequest(
            (SyntheticWindowsSid.from_components((1, 2, 3, 4)),)
        )
        with WindowsRestrictedToken.create_from_current_process(request) as token:
            self.assertTrue(token.inspection.is_restricted)
            self.assertEqual(token.inspection.restricted_sids, (request.restricted_sids[0].value,))
            self.assertTrue(token.inspection.change_notify_privilege_enabled)
            self.assertLessEqual(token.inspection.enabled_privilege_count, 1)


if __name__ == "__main__":
    unittest.main()
