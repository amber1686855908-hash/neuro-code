from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
    build_permission_request,
)
from neuro_code.application.permissions.scopes import (
    PermissionScopeCandidate,
    PermissionScopeContext,
    PermissionScopeKind,
)


class SessionApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _scoped_request(
        call_id: str,
        *,
        session: str = "session-a",
        root: Path | None = None,
        path: str = "src/one.py",
    ) -> tuple[PermissionRequest, PermissionScopeCandidate]:
        root_path = Path.cwd() if root is None else root
        root_text = str(root_path.resolve(strict=False))
        candidate = PermissionScopeCandidate(
            PermissionScopeKind.WORKSPACE_EDITS,
            root_text,
        )
        context = PermissionScopeContext(session, root_text)
        request = build_permission_request(
            call_id,
            "search_replace",
            {"path": path, "old": "before", "new": "after"},
            "interactive approval required",
            scope_candidates=(candidate,),
            scope_context=context,
        )
        return request, candidate

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

    async def test_workspace_scope_caches_different_edit_arguments_in_one_context(self) -> None:
        broker = SessionApprovalBroker()
        calls = 0
        first, candidate = self._scoped_request("call-1", path="src/one.py")
        second, same_candidate = self._scoped_request("call-2", path="src/two.py")

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            return PermissionApproval.allow_scope(candidate)

        broker.set_handler(approve)
        first_result = await broker.request(first)
        cached_result = await broker.request(second)

        self.assertEqual(candidate, same_candidate)
        self.assertEqual(calls, 1)
        self.assertEqual(first_result.kind.value, "allow_scope")
        self.assertTrue(cached_result.allowed)
        self.assertTrue(cached_result.cache_hit)
        self.assertEqual(cached_result.scope_candidate, candidate)

    async def test_scope_cache_isolated_by_session_and_workspace(self) -> None:
        broker = SessionApprovalBroker()
        calls = 0

        async def approve(request: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            assert request.scope_candidates
            return PermissionApproval.allow_scope(request.scope_candidates[0])

        broker.set_handler(approve)
        workspace_a = Path.cwd() / "workspace-a"
        workspace_b = Path.cwd() / "workspace-b"
        first, _ = self._scoped_request("call-1", session="session-a", root=workspace_a)
        other_session, _ = self._scoped_request(
            "call-2",
            session="session-b",
            root=workspace_a,
        )
        other_workspace, _ = self._scoped_request(
            "call-3",
            session="session-a",
            root=workspace_b,
        )

        await broker.request(first)
        self.assertFalse((await broker.request(other_session)).cache_hit)
        self.assertFalse((await broker.request(other_workspace)).cache_hit)
        self.assertEqual(calls, 3)

    async def test_new_broker_does_not_reuse_memory_only_scope_grants(self) -> None:
        first, candidate = self._scoped_request("call-1")
        first_broker = SessionApprovalBroker()
        first_broker.set_handler(lambda _: _allow_scope(candidate))
        await first_broker.request(first)

        second_broker = SessionApprovalBroker()
        calls = 0

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            return PermissionApproval.allow_scope(candidate)

        second_broker.set_handler(approve)
        await second_broker.request(self._scoped_request("call-2")[0])
        self.assertEqual(calls, 1)

    async def test_queued_equivalent_requests_recheck_a_scope_grant_before_second_modal(
        self,
    ) -> None:
        broker = SessionApprovalBroker()
        first, candidate = self._scoped_request("call-1")
        second, _ = self._scoped_request("call-2", path="src/two.py")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return PermissionApproval.allow_scope(candidate)

        broker.set_handler(approve)
        first_task = asyncio.create_task(broker.request(first))
        await started.wait()
        second_task = asyncio.create_task(broker.request(second))
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()

        first_result, second_result = await asyncio.gather(first_task, second_task)
        self.assertTrue(first_result.allowed)
        self.assertTrue(second_result.allowed)
        self.assertTrue(second_result.cache_hit)
        self.assertEqual(calls, 1)

    async def test_allow_once_does_not_grant_a_queued_equivalent_request(self) -> None:
        broker = SessionApprovalBroker()
        first, _ = self._scoped_request("call-1")
        second, _ = self._scoped_request("call-2", path="src/two.py")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return PermissionApproval.allow_once()

        broker.set_handler(approve)
        first_task = asyncio.create_task(broker.request(first))
        await started.wait()
        second_task = asyncio.create_task(broker.request(second))
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)

        self.assertEqual(first_result.kind.value, "allow_once")
        self.assertEqual(second_result.kind.value, "allow_once")
        self.assertFalse(second_result.cache_hit)
        self.assertEqual(calls, 2)

    async def test_cancelled_scope_owner_does_not_grant_a_queued_request(self) -> None:
        broker = SessionApprovalBroker()
        first, _ = self._scoped_request("call-1")
        second, _ = self._scoped_request("call-2", path="src/two.py")
        started = asyncio.Event()
        calls = 0

        async def approve(_: PermissionRequest) -> PermissionApproval:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()
            return PermissionApproval.allow_once()

        broker.set_handler(approve)
        first_task = asyncio.create_task(broker.request(first))
        await started.wait()
        second_task = asyncio.create_task(broker.request(second))
        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task
        result = await second_task

        self.assertTrue(result.allowed)
        self.assertEqual(result.kind.value, "allow_once")
        self.assertFalse(result.cache_hit)
        self.assertEqual(calls, 2)


async def _allow_scope(candidate: PermissionScopeCandidate) -> PermissionApproval:
    return PermissionApproval.allow_scope(candidate)


if __name__ == "__main__":
    unittest.main()
