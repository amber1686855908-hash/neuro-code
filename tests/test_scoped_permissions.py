from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
    build_permission_request,
)
from neuro_code.application.permissions.policy import PermissionManager
from neuro_code.application.permissions.scopes import (
    PermissionCommandFamily,
    PermissionScopeCandidate,
    PermissionScopeContext,
    PermissionScopeKind,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.infrastructure.tools.filesystem import SearchReplaceTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver


class ScopedPermissionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_search_replace_pipeline_reuses_workspace_scope_for_new_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("one\n", encoding="utf-8")
            second.write_text("two\n", encoding="utf-8")
            broker = SessionApprovalBroker()
            handled: list[PermissionRequest] = []

            async def approve(request: PermissionRequest) -> PermissionApproval:
                handled.append(request)
                self.assertEqual(len(request.scope_candidates), 1)
                return PermissionApproval.allow_scope(request.scope_candidates[0])

            broker.set_handler(approve)
            executor = ToolExecutor(
                tools=ToolRegistry([SearchReplaceTool()]),
                permissions=PermissionManager(interactive=True),
                approver=broker,
                tool_context=ToolContext(root),
                session_store=None,
                workspace_change_observer=FilesystemWorkspaceChangeObserver(),
                context_builder=ContextBuilder(
                    reasoning_effort=ReasoningEffort.HIGH,
                    interaction_mode=InteractionMode.NORMAL,
                    plan=None,
                    instruction_provider=None,
                    skill_provider=None,
                ),
            )
            events: list[AgentEvent] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                event = AgentEvent.create(len(events) + 1, kind, data)
                events.append(event)
                return event

            await executor.execute(
                ToolCall(
                    "edit-1",
                    "search_replace",
                    {"path": "first.py", "old": "one", "new": "updated-one"},
                ),
                [],
                [],
                emit,
                "session-1",
            )
            await executor.execute(
                ToolCall(
                    "edit-2",
                    "search_replace",
                    {"path": "second.py", "old": "two", "new": "updated-two"},
                ),
                [],
                [],
                emit,
                "session-1",
            )

            self.assertEqual(first.read_text(encoding="utf-8"), "updated-one\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "updated-two\n")
            self.assertEqual(len(handled), 1)
            resolved = [
                event for event in events if event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED
            ]
            self.assertEqual(len(resolved), 2)
            self.assertFalse(resolved[0].data["cache_hit"])
            self.assertTrue(resolved[1].data["cache_hit"])

    async def test_representative_workflow_reduces_prompts_without_a_blanket_grant(self) -> None:
        broker = SessionApprovalBroker()
        context = PermissionScopeContext("session-1", "/workspace")
        candidates = {
            "edit": PermissionScopeCandidate(PermissionScopeKind.WORKSPACE_EDITS, "/workspace"),
            "test": PermissionScopeCandidate(
                PermissionScopeKind.COMMAND_FAMILY,
                "/workspace",
                PermissionCommandFamily.TEST,
            ),
            "static": PermissionScopeCandidate(
                PermissionScopeKind.COMMAND_FAMILY,
                "/workspace",
                PermissionCommandFamily.STATIC_CHECK,
            ),
            "git": PermissionScopeCandidate(
                PermissionScopeKind.COMMAND_FAMILY,
                "/workspace",
                PermissionCommandFamily.GIT_READ,
            ),
        }
        requests: list[PermissionRequest] = []
        for index in range(8):
            requests.append(
                build_permission_request(
                    f"edit-{index}",
                    "search_replace",
                    {"path": f"src/file-{index}.py", "old": "a", "new": "b"},
                    "interactive approval required",
                    scope_candidates=(candidates["edit"],),
                    scope_context=context,
                )
            )
        for index in range(3):
            requests.append(
                build_permission_request(
                    f"test-{index}",
                    "bash",
                    {"command": f"pytest -q tests/test_{index}.py"},
                    "interactive approval required",
                    scope_candidates=(candidates["test"],),
                    scope_context=context,
                )
            )
        for index in range(2):
            requests.append(
                build_permission_request(
                    f"static-{index}",
                    "bash",
                    {"command": f"ruff check src/module_{index}"},
                    "interactive approval required",
                    scope_candidates=(candidates["static"],),
                    scope_context=context,
                )
            )
        for command in ("git status --short", "git diff --stat"):
            requests.append(
                build_permission_request(
                    f"git-{len(requests)}",
                    "bash",
                    {"command": command},
                    "interactive approval required",
                    scope_candidates=(candidates["git"],),
                    scope_context=context,
                )
            )

        handled: list[PermissionRequest] = []

        async def approve(request: PermissionRequest) -> PermissionApproval:
            handled.append(request)
            return PermissionApproval.allow_scope(request.scope_candidates[0])

        broker.set_handler(approve)
        for request in requests:
            self.assertTrue((await broker.request(request)).allowed)

        self.assertEqual(len(requests), 15)
        self.assertEqual(len(handled), 4)


if __name__ == "__main__":
    unittest.main()
