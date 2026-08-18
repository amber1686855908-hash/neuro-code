from __future__ import annotations

import ctypes
import os
import unittest

from neuro_code.infrastructure.sandbox import windows_security_token as token_module
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_security_token import (
    WindowsRestrictedToken,
    WindowsRestrictedTokenRequest,
    WindowsTokenError,
    WindowsTokenInspection,
    _NativeWindowsSecurityTokenApi,
    inspect_windows_process_token,
)


def _native_inspection_fixture() -> tuple[_NativeWindowsSecurityTokenApi, list[object]]:
    """Build Win32-shaped buffers for the portable token parser contract."""

    api = object.__new__(_NativeWindowsSecurityTokenApi)
    keep_alive: list[object] = []
    user_sid = ctypes.create_string_buffer(b"user-sid")
    restricted_sid = ctypes.create_string_buffer(b"restricted-sid")
    keep_alive.extend((user_sid, restricted_sid))

    user = token_module._TokenUserOne()
    user.User.Sid = ctypes.addressof(user_sid)
    user_raw = ctypes.string_at(ctypes.addressof(user), ctypes.sizeof(user))

    restricted = ctypes.create_string_buffer(
        token_module._TokenGroupsOne.Groups.offset + ctypes.sizeof(token_module._SidAndAttributes)
    )
    ctypes.c_uint32.from_buffer(restricted).value = 1
    token_module._SidAndAttributes.from_buffer(
        restricted, token_module._TokenGroupsOne.Groups.offset
    ).Sid = ctypes.addressof(restricted_sid)
    keep_alive.append(restricted)

    privileges = ctypes.create_string_buffer(
        token_module._TokenPrivilegesOne.Privileges.offset
        + 2 * ctypes.sizeof(token_module._LuidAndAttributes)
    )
    ctypes.c_uint32.from_buffer(privileges).value = 2
    first = token_module._LuidAndAttributes.from_buffer(
        privileges, token_module._TokenPrivilegesOne.Privileges.offset
    )
    first.Luid.LowPart = 17
    first.Luid.HighPart = 2
    first.Attributes = token_module._SE_PRIVILEGE_ENABLED
    second = token_module._LuidAndAttributes.from_buffer(
        privileges,
        token_module._TokenPrivilegesOne.Privileges.offset
        + ctypes.sizeof(token_module._LuidAndAttributes),
    )
    second.Luid.LowPart = 99
    second.Luid.HighPart = 3
    second.Attributes = token_module._SE_PRIVILEGE_ENABLED
    keep_alive.append(privileges)

    raw_by_class = {
        token_module._TOKEN_USER: user_raw,
        token_module._TOKEN_RESTRICTED_SIDS: restricted.raw,
        token_module._TOKEN_PRIVILEGES: privileges.raw,
    }

    def get_token_information(
        _handle: object,
        information_class: object,
        output: object,
        size: object,
        returned: object,
    ) -> int:
        raw = raw_by_class[int(information_class)]
        ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(raw)
        if not output or int(size) == 0:
            return 0
        ctypes.memmove(output, raw, len(raw))
        return 1

    sid_texts = {
        ctypes.addressof(user_sid): "S-1-5-21-10-20-30-40",
        ctypes.addressof(restricted_sid): "S-1-16-1-2-3-4",
    }
    sid_text_buffers: list[ctypes.Array[ctypes.c_wchar]] = []

    def convert_sid_to_string(_sid: object, output: object) -> int:
        sid_value = int(ctypes.cast(_sid, ctypes.c_void_p).value or 0)
        text = ctypes.create_unicode_buffer(sid_texts[sid_value])
        sid_text_buffers.append(text)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(text)
        return 1

    def lookup_privilege_value(_system: object, _name: object, output: object) -> int:
        luid = ctypes.cast(output, ctypes.POINTER(token_module._Luid)).contents
        luid.LowPart = 17
        luid.HighPart = 2
        return 1

    api._get_token_information = get_token_information  # type: ignore[assignment]
    api._get_last_error = lambda: token_module._ERROR_INSUFFICIENT_BUFFER  # type: ignore[assignment]
    api._convert_sid_to_string = convert_sid_to_string  # type: ignore[assignment]
    api._local_free = lambda _pointer: 0  # type: ignore[assignment]
    api._is_token_restricted = lambda _handle: 1  # type: ignore[assignment]
    api._lookup_privilege_value = lookup_privilege_value  # type: ignore[assignment]
    keep_alive.append(sid_text_buffers)
    return api, keep_alive


class _FakeSecurityTokenApi:
    def __init__(self) -> None:
        self.source_handle = 101
        self.created_handle = 202
        self.child_token_handle = 303
        self.closed: list[int] = []
        self.create_calls: list[
            tuple[int, int, tuple[SyntheticWindowsSid, ...], tuple[str, ...]]
        ] = []
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
        additional_restricting_sids: tuple[str, ...] = (),
    ) -> int:
        self.create_calls.append(
            (existing_handle, flags, restricted_sids, additional_restricting_sids)
        )
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

    def test_request_rejects_noncanonical_sid_tuple_and_flag_types(self) -> None:
        sid = SyntheticWindowsSid.from_components((1, 2, 3, 4))
        with self.assertRaises(TypeError):
            WindowsRestrictedTokenRequest([sid])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsRestrictedTokenRequest((object(),))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsRestrictedTokenRequest((sid,), disable_max_privilege=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsRestrictedTokenRequest((sid,), lua_token=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsRestrictedTokenRequest((sid,), write_restricted=1)  # type: ignore[arg-type]

    def test_request_validates_identity_restricting_sids_without_making_them_capabilities(
        self,
    ) -> None:
        sid = SyntheticWindowsSid.from_components((1, 2, 3, 4))
        request = WindowsRestrictedTokenRequest(
            (sid,),
            additional_restricting_sids=("S-1-5-5-0-123", "S-1-1-0"),
        )
        self.assertEqual(
            request.all_restricting_sids,
            (sid.value, "S-1-5-5-0-123", "S-1-1-0"),
        )
        with self.assertRaises(ValueError):
            WindowsRestrictedTokenRequest(
                (sid,),
                additional_restricting_sids=(sid.value,),
            )
        with self.assertRaises(ValueError):
            WindowsRestrictedTokenRequest(
                (sid,),
                additional_restricting_sids=("S-1-1-0", "S-1-1-0"),
            )


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
            [(api.source_handle, request.flags, (sid,), ())],
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

    def test_token_inspection_validates_exact_shape_and_sid_count(self) -> None:
        sid = "S-1-5-21-10-20-30-40"
        invalid = (
            {"restricted_sid_count": -1},
            {"restricted_sid_count": 1, "is_restricted": 1},
            {"restricted_sid_count": 0, "is_restricted": False, "user_sid": "not-a-sid"},
            {
                "restricted_sid_count": 0,
                "is_restricted": False,
                "privilege_count": -1,
            },
            {
                "restricted_sid_count": 0,
                "is_restricted": False,
                "privilege_count": 1,
                "enabled_privilege_count": 2,
            },
            {
                "restricted_sid_count": 1,
                "is_restricted": True,
                "restricted_sids": (sid,),
                "change_notify_privilege_enabled": 1,
            },
            {
                "restricted_sid_count": 1,
                "is_restricted": True,
                "restricted_sids": (sid,),
                "unexpected_enabled_privilege_count": -1,
            },
            {
                "restricted_sid_count": 1,
                "is_restricted": True,
                "restricted_sids": (sid,),
                "enabled_privilege_count": 1,
                "unexpected_enabled_privilege_count": 2,
            },
        )
        for values in invalid:
            with self.assertRaises((TypeError, ValueError)):
                WindowsTokenInspection(**values)  # type: ignore[arg-type]

    def test_native_token_parser_attests_sids_and_privileges(self) -> None:
        api, keep_alive = _native_inspection_fixture()
        inspection = api.inspect_token(99)
        self.assertEqual(inspection.user_sid, "S-1-5-21-10-20-30-40")
        self.assertEqual(inspection.restricted_sids, ("S-1-16-1-2-3-4",))
        self.assertTrue(inspection.is_restricted)
        self.assertEqual(inspection.restricted_sid_count, 1)
        self.assertEqual(inspection.privilege_count, 2)
        self.assertEqual(inspection.enabled_privilege_count, 2)
        self.assertTrue(inspection.change_notify_privilege_enabled)
        self.assertEqual(inspection.unexpected_enabled_privilege_count, 1)
        self.assertTrue(keep_alive)

    def test_native_token_facade_creates_restricted_token_and_default_dacl(self) -> None:
        api = object.__new__(_NativeWindowsSecurityTokenApi)
        sid = SyntheticWindowsSid.from_components((1, 2, 3, 4))
        allocated: list[object] = []
        freed: list[object] = []

        def convert_sid(_value: object, output: object) -> int:
            buffer = ctypes.create_string_buffer(16)
            allocated.append(buffer)
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(
                buffer
            )
            return 1

        def create_token(*args: object) -> int:
            ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p)).contents.value = 500
            return 1

        def local_free(pointer: object) -> int:
            freed.append(pointer)
            return 0

        acl_calls: list[int] = []

        def set_acl(count: object, _entries: object, _old: object, output: object) -> int:
            acl_calls.append(int(count))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 600
            return 0

        token_calls: list[int] = []

        def set_token(token: object, _info_class: object, _info: object, _size: object) -> int:
            token_calls.append(int(token))
            return 1

        api._convert_string_sid = convert_sid  # type: ignore[assignment]
        api._create_restricted_token = create_token  # type: ignore[assignment]
        api._local_free = local_free  # type: ignore[assignment]
        api._set_entries_in_acl = set_acl  # type: ignore[assignment]
        api._set_token_information = set_token  # type: ignore[assignment]
        api._get_last_error = lambda: 5  # type: ignore[assignment]

        self.assertEqual(api.create_restricted_token(10, 0x8, (sid,)), 500)
        api.set_default_dacl(500, ("S-1-5-5-0-1", "S-1-1-0"))
        self.assertEqual(acl_calls, [2])
        self.assertEqual(token_calls, [500])
        self.assertGreaterEqual(len(freed), 3)

        failing = object.__new__(_NativeWindowsSecurityTokenApi)
        failing._convert_string_sid = convert_sid  # type: ignore[assignment]
        failing._create_restricted_token = lambda *args: 0  # type: ignore[assignment]
        failing._local_free = local_free  # type: ignore[assignment]
        failing._get_last_error = lambda: 5  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            failing.create_restricted_token(10, 0x8, (sid,))

    def test_native_token_facade_opens_query_handles_and_rejects_invalid_process(self) -> None:
        api = object.__new__(_NativeWindowsSecurityTokenApi)
        calls: list[tuple[int, int]] = []
        api._get_current_process = lambda: 44  # type: ignore[assignment]
        api._get_last_error = lambda: 6  # type: ignore[assignment]

        def open_token(process: object, access: object, output: object) -> int:
            calls.append((int(process), int(access)))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 88
            return 1

        api._open_process_token = open_token  # type: ignore[assignment]
        self.assertEqual(api.open_current_process_token(), 88)
        self.assertEqual(api.open_process_token(77), 88)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(WindowsTokenError):
            api.open_process_token(0)

        api._open_process_token = lambda *_args: 0  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            api.open_current_process_token()

    def test_process_token_inspection_preserves_close_failure(self) -> None:
        api = _FakeSecurityTokenApi()
        api.fail_close = True
        with self.assertRaises(WindowsTokenError) as raised:
            inspect_windows_process_token(404, api=api)
        self.assertEqual(raised.exception.operation, "CloseHandle")
        self.assertEqual(api.closed, [api.child_token_handle])

    def test_token_creation_rejects_invalid_handles_and_preserves_cleanup(self) -> None:
        sid = SyntheticWindowsSid.from_components((11, 22, 33, 44))
        request = WindowsRestrictedTokenRequest((sid,))
        api = _FakeSecurityTokenApi()
        api.open_current_process_token = lambda: 0  # type: ignore[method-assign]
        with self.assertRaises(WindowsTokenError):
            WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(api.closed, [0])

        api = _FakeSecurityTokenApi()
        api.create_restricted_token = lambda *_args: 0  # type: ignore[method-assign]
        with self.assertRaises(WindowsTokenError):
            WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(api.closed, [api.source_handle, 0])

        api = _FakeSecurityTokenApi()
        api.fail_close = True
        with self.assertRaises(WindowsTokenError):
            WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(api.closed, [api.source_handle, api.created_handle])

    def test_token_lifecycle_properties_and_closed_dacl_fail_closed(self) -> None:
        api = _FakeSecurityTokenApi()
        request = WindowsRestrictedTokenRequest(
            (SyntheticWindowsSid.from_components((11, 22, 33, 44)),)
        )
        token = WindowsRestrictedToken.create_from_current_process(request, api=api)
        self.assertEqual(token.request, request)
        self.assertEqual(token.inspection, api.inspection)
        with token as entered:
            self.assertIs(entered, token)
        with self.assertRaises(WindowsTokenError):
            token.set_default_dacl(("S-1-1-0",))

    def test_native_token_parser_errors_are_fail_closed(self) -> None:
        api, _ = _native_inspection_fixture()
        with self.assertRaises(WindowsTokenError):
            api._sid_to_text(0, context="TokenUser")

        api._convert_sid_to_string = lambda *_args: 0  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            api._sid_to_text(123, context="TokenUser")

        api._convert_sid_to_string = lambda _sid, output: 1  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            api._sid_to_text(123, context="TokenUser")

        bad_sid_text = ctypes.create_unicode_buffer("not-a-sid")
        api._convert_sid_to_string = lambda _sid, output: (  # type: ignore[assignment]
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.__setattr__(
                "value", ctypes.addressof(bad_sid_text)
            )
            or 1
        )
        with self.assertRaises(WindowsTokenError):
            api._sid_to_text(123, context="TokenUser")

        api._get_token_information = lambda *_args: 0  # type: ignore[assignment]
        api._get_last_error = lambda: 5  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            api._query_token_information_buffer(1, token_module._TOKEN_USER)

        api._get_last_error = lambda: token_module._ERROR_INSUFFICIENT_BUFFER  # type: ignore[assignment]
        with self.assertRaises(WindowsTokenError):
            api._query_token_information_buffer(1, token_module._TOKEN_USER)

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
