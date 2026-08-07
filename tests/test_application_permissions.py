from __future__ import annotations

import asyncio
import unittest
from typing import cast

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
)
from neuro_code.application.permissions.service import ApproveToolRequest, ToolApprovalService


class ApprovalPortFixture:
    def __init__(self, approval: PermissionApproval) -> None:
        self.approval = approval
        self.requests: list[PermissionRequest] = []
        self.cancel = False

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        self.requests.append(request)
        if self.cancel:
            raise asyncio.CancelledError
        return self.approval


def _request() -> PermissionRequest:
    return PermissionRequest(
        call_id="call-1",
        tool_name="bash",
        summary="Run shell command:\npwd",
        reason="interactive approval required",
        scope_key="scope-1",
    )


class ToolApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_tool_forwards_bounded_request_and_result(self) -> None:
        permission = _request()
        approver = ApprovalPortFixture(PermissionApproval.allow_once())
        service = ToolApprovalService(approver)

        result = await service.approve_tool(ApproveToolRequest(permission))

        self.assertTrue(result.allowed)
        self.assertEqual(approver.requests, [permission])

    async def test_runtime_port_bridge_uses_the_same_typed_use_case(self) -> None:
        permission = _request()
        approver = ApprovalPortFixture(PermissionApproval.deny("user denied"))
        service = ToolApprovalService(approver)

        result = await service.request(permission)

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "user denied")
        self.assertEqual(approver.requests, [permission])

    async def test_noncanonical_requests_fail_closed(self) -> None:
        approver = ApprovalPortFixture(PermissionApproval.allow_once())
        service = ToolApprovalService(approver)

        with self.assertRaises(ValueError):
            ApproveToolRequest(cast(PermissionRequest, object()))
        with self.assertRaises(ValueError):
            await service.approve_tool(cast(ApproveToolRequest, object()))

        self.assertEqual(approver.requests, [])

    async def test_cancellation_from_approval_port_is_preserved(self) -> None:
        approver = ApprovalPortFixture(PermissionApproval.allow_once())
        approver.cancel = True
        service = ToolApprovalService(approver)

        with self.assertRaises(asyncio.CancelledError):
            await service.approve_tool(ApproveToolRequest(_request()))

        self.assertEqual(len(approver.requests), 1)
