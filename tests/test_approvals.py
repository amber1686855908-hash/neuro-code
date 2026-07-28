from __future__ import annotations

import unittest

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
    build_permission_request,
)
from neuro_code.application.runtime.approval import SessionApprovalBroker


class SessionApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_approval_caches_only_the_identical_action(self) -> None:
        broker = SessionApprovalBroker()
        handled: list[str] = []

        async def approve(request: PermissionRequest) -> PermissionApproval:
            handled.append(type(request).__name__)
            return PermissionApproval.allow_session()

        broker.set_handler(approve)
        first = build_permission_request(
            "call-1",
            "bash",
            {"command": "git status"},
            "interactive approval required",
        )
        same_action = build_permission_request(
            "call-2",
            "bash",
            {"command": "git status"},
            "interactive approval required",
        )
        different_action = build_permission_request(
            "call-3",
            "bash",
            {"command": "git push"},
            "interactive approval required",
        )

        first_result = await broker.request(first)
        cached_result = await broker.request(same_action)
        different_result = await broker.request(different_action)

        self.assertTrue(first_result.allowed)
        self.assertTrue(cached_result.allowed)
        self.assertIn("identical action", cached_result.reason)
        self.assertTrue(different_result.allowed)
        self.assertEqual(handled, ["PermissionRequest", "PermissionRequest"])

    async def test_missing_ui_and_one_time_denial_fail_closed_without_caching(self) -> None:
        broker = SessionApprovalBroker()
        request = build_permission_request(
            "call-1",
            "search_replace",
            {"path": "note.txt", "old": "a", "new": "b"},
            "interactive approval required",
        )

        unavailable = await broker.request(request)
        self.assertFalse(unavailable.allowed)
        self.assertIn("unavailable", unavailable.reason)

        calls = 0

        async def deny(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            return PermissionApproval.deny()

        broker.set_handler(deny)
        self.assertFalse((await broker.request(request)).allowed)
        self.assertFalse((await broker.request(request)).allowed)
        self.assertEqual(calls, 2)

    async def test_unscopable_arguments_downgrade_session_approval_to_once(self) -> None:
        broker = SessionApprovalBroker()
        calls = 0

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            return PermissionApproval.allow_session()

        broker.set_handler(approve)
        request = build_permission_request(
            "call-1",
            "custom_tool",
            {"value": object()},
            "interactive approval required",
        )

        first = await broker.request(request)
        second = await broker.request(request)

        self.assertEqual(first.kind.value, "allow_once")
        self.assertEqual(second.kind.value, "allow_once")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
