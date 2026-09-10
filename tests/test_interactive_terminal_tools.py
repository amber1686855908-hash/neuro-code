from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.ports.sandbox import (
    LocalProcessLifecycleCapability,
    LocalProcessSandbox,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.terminal import (
    InteractiveTerminalManager,
    InteractiveTerminalSession,
    TerminalCreationAuthorization,
    TerminalEofHandler,
    TerminalErrorHandler,
    TerminalOutputHandler,
    TerminalPlatformSession,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.application.sessions.binding import ConversationBindingResourceScope
from neuro_code.application.sessions.terminal_sessions import LocalInteractiveTerminalManager
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalOutputChunk, TerminalSignal, TerminalSize
from neuro_code.infrastructure.tools.interactive_terminal import (
    CreateTerminalTool,
    TerminalKillTool,
    TerminalOutputTool,
    TerminalResizeTool,
    TerminalWaitTool,
    TerminalWriteTool,
)
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.workspace.paths import FilesystemWorkspacePathResolver
from neuro_code.shared.errors import TerminalError, ToolError


class _Session:
    def __init__(self, session_id: str = "terminal-1", output: bytes = b"ready\nlater\n") -> None:
        self.session_id = session_id
        self.process_id = 4242
        self._size = TerminalSize(80, 24)
        self.output = output
        self.exit_code: int | None = None
        self.closed = False
        self.read_calls: list[tuple[int, int, float]] = []
        self.writes: list[bytes] = []
        self.resizes: list[TerminalSize] = []

    @property
    def size(self) -> TerminalSize:
        return self._size

    async def read(
        self,
        *,
        after_offset: int = 0,
        max_bytes: int = 65_536,
        wait_seconds: float = 0.0,
    ) -> TerminalOutputChunk:
        if after_offset > len(self.output):
            raise TerminalError("offset is beyond the fixture output")
        self.read_calls.append((after_offset, max_bytes, wait_seconds))
        data = self.output[after_offset : after_offset + max_bytes]
        next_offset = after_offset + len(data)
        return TerminalOutputChunk(
            data,
            next_offset=next_offset,
            dropped_bytes=0,
            eof=self.exit_code is not None and next_offset == len(self.output),
        )

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise TerminalError("session is closed")
        self.writes.append(data)
        self.output += b"ack:" + data

    async def resize(self, size: TerminalSize) -> None:
        if self.closed:
            raise TerminalError("session is closed")
        self.resizes.append(size)
        self._size = size

    async def send_signal(self, signal: TerminalSignal) -> None:
        del signal

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None:
        del timeout_seconds
        return self.exit_code

    async def close(self) -> None:
        self.closed = True
        if self.exit_code is None:
            self.exit_code = 143


class _Manager:
    def __init__(self, *, sessions: list[_Session] | None = None) -> None:
        self.sessions = {session.session_id: session for session in sessions or []}
        self.create_calls: list[dict[str, Any]] = []
        self.shutdown_calls = 0

    async def create_exec(
        self,
        call_id: str,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: str,
        env: dict[str, str],
        size: TerminalSize,
        output_capacity: int,
        authorization: TerminalCreationAuthorization | None = None,
    ) -> _Session:
        session = _Session(session_id=f"terminal-{len(self.sessions) + 1}")
        session._size = size
        self.sessions[session.session_id] = session
        self.create_calls.append(
            {
                "call_id": call_id,
                "command": executable,
                "args": arguments,
                "cwd": cwd,
                "env": env,
                "size": size,
                "output_capacity": output_capacity,
                "authorization": authorization,
            }
        )
        return session

    async def get_session(self, session_id: str) -> InteractiveTerminalSession | None:
        return self.sessions.get(session_id)

    async def list_sessions(self) -> tuple[InteractiveTerminalSession, ...]:
        return tuple(self.sessions.values())

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        for session in tuple(self.sessions.values()):
            await session.close()


class _Approver:
    def __init__(self, approval: PermissionApproval) -> None:
        self.approval = approval
        self.requests: list[PermissionRequest] = []

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        self.requests.append(request)
        return self.approval


def _context(root: Path, manager: InteractiveTerminalManager) -> ToolContext:
    return ToolContext(root, interactive_terminals=manager)


class _AcceptancePlatformSession:
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def __init__(self, platform: _AcceptancePlatform) -> None:
        self._platform = platform
        self.process_id = 9001
        self.writes: list[bytes] = []
        self.resizes: list[TerminalSize] = []
        self.exit_code: int | None = None
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        self._platform.emit(b"stdin acknowledged\n")

    def resize(self, size: TerminalSize) -> None:
        self.resizes.append(size)

    def send_signal(self, signal: TerminalSignal) -> None:
        del signal

    def poll_exit(self) -> int | None:
        return self.exit_code

    def close(self) -> None:
        self.closed = True
        self.exit_code = 0


class _AcceptancePlatform:
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def __init__(self) -> None:
        self.session = _AcceptancePlatformSession(self)
        self.spawn_started = threading.Event()
        self._on_output: TerminalOutputHandler | None = None
        self._on_eof: TerminalEofHandler | None = None
        self._on_error: TerminalErrorHandler | None = None
        self.spawn_arguments: tuple[str, ...] = ()

    def spawn_exec(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        del cwd, env, size
        self.spawn_arguments = (executable, *arguments)
        self._on_output = on_output
        self._on_eof = on_eof
        self._on_error = on_error
        self.spawn_started.set()
        on_output(b"READY\n")
        return self.session

    def emit(self, data: bytes) -> None:
        assert self._on_output is not None
        self._on_output(data)


class _AcceptanceSandbox(LocalProcessSandbox):
    def __init__(self, platform: _AcceptancePlatform) -> None:
        self.platform = platform
        self.requests: list[SandboxedProcessRequest] = []

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return self.platform.lifecycle_capability

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        raise AssertionError(f"unexpected non-terminal spawn: {request.purpose.value}")

    def spawn_terminal(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        self.requests.append(request)
        assert request.executable is not None
        return self.platform.spawn_exec(
            request.executable,
            request.arguments,
            cwd=request.cwd,
            env=request.environment_policy.variables,
            size=size,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
        )


class InteractiveTerminalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_exposes_local_family_only_when_manager_is_present(self) -> None:
        local = default_tool_registry(interactive_terminals=object())
        self.assertEqual(
            {
                "create_terminal",
                "terminal_output",
                "terminal_write",
                "terminal_resize",
                "terminal_wait",
                "terminal_kill",
            },
            set(local.names()).intersection(
                {
                    "create_terminal",
                    "terminal_output",
                    "terminal_write",
                    "terminal_resize",
                    "terminal_wait",
                    "terminal_kill",
                }
            ),
        )
        self.assertEqual(
            (),
            tuple(
                name
                for name in default_tool_registry().names()
                if name.startswith("terminal_") or name == "create_terminal"
            ),
        )
        self.assertNotIn(
            "create_terminal",
            default_tool_registry(client_terminal=object(), interactive_terminals=object()).names(),
        )
        filtered = default_tool_registry(
            interactive_terminals=object(),
            allowed_tool_names={"create_terminal", "terminal_write"},
        )
        self.assertEqual({"create_terminal", "terminal_write"}, set(filtered.names()))

    async def test_create_requires_pipeline_authorization_and_uses_one_outer_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = _Manager()
            with self.assertRaisesRegex(ToolError, "authorized"):
                await CreateTerminalTool().execute(
                    {"command": "python"},
                    _context(root, manager),
                )

            approver = _Approver(PermissionApproval.allow_once())
            executor = ToolExecutor(
                tools=ToolRegistry([CreateTerminalTool()]),
                permissions=PermissionManager(interactive=True),
                approver=approver,
                tool_context=_context(root, manager),
                session_store=None,
                workspace_change_observer=_workspace_observer(),
                context_builder=_context_builder(),
            )
            events: list[AgentEvent] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                event = AgentEvent.create(len(events) + 1, kind, data)
                events.append(event)
                return event

            observation = await executor.execute(
                ToolCall("create-1", "create_terminal", {"command": "python"}),
                [],
                [],
                emit,
                "session-1",
            )
            self.assertIsNotNone(observation)
            self.assertEqual(1, len(approver.requests))
            self.assertEqual(1, len(manager.create_calls))
            authorization = manager.create_calls[0]["authorization"]
            self.assertIsInstance(authorization, TerminalCreationAuthorization)
            self.assertIn(AgentEventKind.TOOL_STARTED, [event.kind for event in events])
            self.assertIn(AgentEventKind.TOOL_COMPLETED, [event.kind for event in events])

            with self.assertRaises(TypeError):
                TerminalCreationAuthorization("create-1")  # type: ignore[call-arg]

    async def test_end_to_end_local_terminal_lifecycle_uses_one_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform = _AcceptancePlatform()
            approver = _Approver(PermissionApproval.allow_once())
            permissions = PermissionManager(
                mode=PermissionMode.DEFAULT,
                rules=(
                    PermissionRule(PermissionEffect.ASK, "create_terminal"),
                    PermissionRule(PermissionEffect.ALLOW, "terminal_write"),
                    PermissionRule(PermissionEffect.ALLOW, "terminal_resize"),
                    PermissionRule(PermissionEffect.ALLOW, "terminal_kill"),
                ),
                interactive=True,
            )
            manager = LocalInteractiveTerminalManager(
                workspace=root,
                workspace_path_resolver=FilesystemWorkspacePathResolver(),
                permissions=permissions,
                approver=approver,
                sandbox_profile=SandboxProfile.OFF,
                local_process_sandbox=_AcceptanceSandbox(platform),
                protected_environment_variables=frozenset({"PRIVATE"}),
            )
            executor = ToolExecutor(
                tools=ToolRegistry(
                    [
                        CreateTerminalTool(),
                        TerminalOutputTool(),
                        TerminalWriteTool(),
                        TerminalResizeTool(),
                        TerminalWaitTool(),
                        TerminalKillTool(),
                    ]
                ),
                permissions=permissions,
                approver=approver,
                tool_context=_context(root, manager),
                session_store=None,
                workspace_change_observer=_workspace_observer(),
                context_builder=_context_builder(),
            )
            events: list[AgentEvent] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                event = AgentEvent.create(len(events) + 1, kind, data)
                events.append(event)
                return event

            async def execute(call: ToolCall) -> dict[str, Any]:
                observation = await executor.execute(
                    call,
                    [],
                    [],
                    emit,
                    "acceptance-session",
                )
                self.assertIsNotNone(observation)
                completed = [
                    event
                    for event in events
                    if event.kind is AgentEventKind.TOOL_COMPLETED
                    and event.data.get("id") == call.id
                ]
                self.assertEqual(1, len(completed))
                content = completed[0].data.get("content")
                self.assertIsInstance(content, str)
                return json.loads(content)

            create_payload = await execute(
                ToolCall(
                    "create-1",
                    "create_terminal",
                    {
                        "command": "python",
                        "args": ["-c", "print('fixture')"],
                        "cwd": ".",
                        "env": {"PRIVATE": "not-approved-in-summary"},
                        "columns": 80,
                        "rows": 24,
                        "output_capacity": 4_096,
                    },
                )
            )
            self.assertTrue(platform.spawn_started.is_set())
            self.assertEqual(["python", "-c", "print('fixture')"], list(platform.spawn_arguments))
            self.assertEqual(1, len(approver.requests))
            self.assertIn("print('fixture')", approver.requests[0].summary)
            self.assertNotIn("not-approved-in-summary", approver.requests[0].summary)
            self.assertEqual("running", create_payload["status"])

            sessions = await manager.list_sessions()
            self.assertEqual(1, len(sessions))
            terminal_id = sessions[0].session_id
            first_output = await execute(
                ToolCall(
                    "output-1",
                    "terminal_output",
                    {"terminal_id": terminal_id, "max_bytes": 6},
                )
            )
            self.assertEqual("READY\n", first_output["data"])
            first_offset = first_output["next_offset"]
            self.assertEqual(6, first_offset)

            platform.emit(b"later\n")
            later_output = await execute(
                ToolCall(
                    "output-2",
                    "terminal_output",
                    {"terminal_id": terminal_id, "after_offset": first_offset},
                )
            )
            self.assertEqual("later\n", later_output["data"])
            later_offset = later_output["next_offset"]

            await execute(
                ToolCall(
                    "write-1",
                    "terminal_write",
                    {"terminal_id": terminal_id, "text": "input", "newline": True},
                )
            )
            self.assertEqual([b"input\n"], platform.session.writes)
            input_output = await execute(
                ToolCall(
                    "output-3",
                    "terminal_output",
                    {"terminal_id": terminal_id, "after_offset": later_offset},
                )
            )
            self.assertEqual("stdin acknowledged\n", input_output["data"])

            await execute(
                ToolCall(
                    "resize-1",
                    "terminal_resize",
                    {"terminal_id": terminal_id, "columns": 100, "rows": 40},
                )
            )
            self.assertEqual([TerminalSize(100, 40)], platform.session.resizes)
            waiting = await execute(
                ToolCall(
                    "wait-1",
                    "terminal_wait",
                    {"terminal_id": terminal_id, "timeout_seconds": 0},
                )
            )
            self.assertEqual(
                {"terminal_id": terminal_id, "status": "running", "exit_code": None},
                waiting,
            )

            stopped = await execute(
                ToolCall("kill-1", "terminal_kill", {"terminal_id": terminal_id})
            )
            self.assertEqual("exited", stopped["status"])
            self.assertEqual(0, stopped["exit_code"])
            self.assertTrue(platform.session.closed)
            self.assertEqual((), await manager.list_sessions())
            self.assertEqual(
                1,
                sum(event.kind is AgentEventKind.TOOL_APPROVAL_REQUESTED for event in events),
            )

            resource_scope = ConversationBindingResourceScope(manager.shutdown)
            await resource_scope.close()
            await resource_scope.close()
            self.assertEqual((), await manager.list_sessions())

    async def test_denying_model_terminal_write_never_reaches_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _Session()
            manager = _Manager(sessions=[session])
            executor = ToolExecutor(
                tools=ToolRegistry([TerminalWriteTool()]),
                permissions=PermissionManager(mode=PermissionMode.DONT_ASK),
                approver=None,
                tool_context=_context(root, manager),
                session_store=None,
                workspace_change_observer=_workspace_observer(),
                context_builder=_context_builder(),
            )

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                return AgentEvent.create(1, kind, data)

            await executor.execute(
                ToolCall(
                    "write-1",
                    "terminal_write",
                    {"terminal_id": session.session_id, "text": "secret"},
                ),
                [],
                [],
                emit,
                "session-1",
            )
            self.assertEqual([], session.writes)

    async def test_output_is_cursor_addressed_and_keeps_output_out_of_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _Session(output=b"ready\nlater\n")
            manager = _Manager(sessions=[session])
            tool = TerminalOutputTool()
            first = await tool.execute(
                {"terminal_id": session.session_id, "max_bytes": 6},
                _context(Path(directory), manager),
            )
            second = await tool.execute(
                {"terminal_id": session.session_id, "after_offset": 6},
                _context(Path(directory), manager),
            )

        self.assertEqual("ready\n", json.loads(first.content)["data"])
        self.assertEqual("later\n", json.loads(second.content)["data"])
        self.assertEqual([(0, 6, 0.0), (6, 65_536, 0.0)], session.read_calls)
        self.assertNotIn("data", first.metadata or {})
        self.assertNotIn("data", second.metadata or {})

    async def test_write_resize_wait_kill_and_unknown_session_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = _Session()
            manager = _Manager(sessions=[session])
            context = _context(root, manager)

            written = await TerminalWriteTool().execute(
                {"terminal_id": session.session_id, "text": "hello", "newline": True},
                context,
            )
            resized = await TerminalResizeTool().execute(
                {"terminal_id": session.session_id, "columns": 120, "rows": 40},
                context,
            )
            session.exit_code = 7
            waited = await TerminalWaitTool().execute(
                {"terminal_id": session.session_id, "timeout_seconds": 0},
                context,
            )
            killed = await TerminalKillTool().execute(
                {"terminal_id": session.session_id},
                context,
            )

            self.assertEqual([b"hello\n"], session.writes)
            self.assertEqual([TerminalSize(120, 40)], session.resizes)
            self.assertEqual(6, written.metadata and written.metadata["bytes_written"])
            self.assertEqual(
                {"columns": 120, "rows": 40}, resized.metadata and resized.metadata["size"]
            )
            self.assertEqual("exited", waited.metadata and waited.metadata["status"])
            self.assertTrue(session.closed)
            self.assertNotIn("data", killed.metadata or {})
            with self.assertRaisesRegex(ToolError, "not found"):
                await TerminalOutputTool().execute(
                    {"terminal_id": "missing"},
                    context,
                )

    async def test_terminal_platform_errors_become_tool_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _Session()
            session.closed = True
            manager = _Manager(sessions=[session])
            with self.assertRaisesRegex(ToolError, "terminal operation failed"):
                await TerminalWriteTool().execute(
                    {"terminal_id": session.session_id, "text": "hello"},
                    _context(Path(directory), manager),
                )


def _context_builder() -> ContextBuilder:
    return ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=None,
        skill_provider=None,
    )


def _workspace_observer() -> Any:
    from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver

    return FilesystemWorkspaceChangeObserver()


if __name__ == "__main__":
    unittest.main()
