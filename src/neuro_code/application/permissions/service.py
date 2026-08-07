"""Application seam for one interactive tool-approval use case.

The service deliberately receives the already bounded ``PermissionRequest``
contract.  It never accepts a raw tool argument mapping and it does not own
the interactive handler, session cache, or permission policy; those remain
with the existing permission port and ``SessionApprovalBroker``.

定义一个交互式工具审批用例的应用接缝.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
)

if TYPE_CHECKING:
    from neuro_code.application.ports.approval import PermissionApprover


@dataclass(frozen=True, slots=True)
class ApproveToolRequest:
    """Validated intent to resolve one existing interactive approval.

    表示解决一个现有交互式审批的已验证意图."""

    permission_request: PermissionRequest

    def __post_init__(self) -> None:
        if not isinstance(self.permission_request, PermissionRequest):
            raise ValueError("approve tool request must contain a canonical permission request")


class ToolApprovalService:
    """Expose ``ApproveTool`` while preserving the existing approval owner.

    ``request`` is the narrow :class:`PermissionApprover` bridge consumed by
    the runtime.  The public use-case method is ``approve_tool`` and accepts a
    typed request, so inbound adapters can adopt the application seam without
    gaining access to raw tool arguments or permission implementation state.

    暴露 ``ApproveTool`` 用例,同时保留现有审批所有者.
    """

    __slots__ = ("_approver",)

    def __init__(self, approver: PermissionApprover) -> None:
        self._approver = approver

    async def approve_tool(self, request: ApproveToolRequest) -> PermissionApproval:
        """Resolve one approval through the configured port.

        通过已配置的端口解决一个审批请求."""

        if not isinstance(request, ApproveToolRequest):
            raise ValueError("approve tool request must be canonical")
        return await self._approver.request(request.permission_request)

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        """Bridge the application use case to the runtime approval port.

        将应用用例连接到运行时审批端口."""

        return await self.approve_tool(ApproveToolRequest(request))


__all__ = ["ApproveToolRequest", "ToolApprovalService"]
