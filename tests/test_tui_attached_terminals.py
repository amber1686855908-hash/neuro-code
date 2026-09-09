from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from neuro_code.application.ports.terminal import InteractiveTerminalManager
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.execution import TurnCancellationPolicy
from neuro_code.domain.terminal import TerminalOutputChunk, TerminalSignal, TerminalSize
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import AttachedTerminalPanel
from neuro_code.shared.ui_language import UiLanguage


class _Session:
    def __init__(self, manager: _Manager, output: bytes) -> None:
        self._manager = manager
        self._output = output
        self.session_id = "terminal-1"
        self.process_id = 4242
        self._size = TerminalSize(80, 24)
        self.writes: list[bytes] = []
        self.resizes: list[TerminalSize] = []
        self.closed = False

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
        del wait_seconds
        data = self._output[after_offset : after_offset + max_bytes]
        return TerminalOutputChunk(
            data,
            next_offset=after_offset + len(data),
            dropped_bytes=0,
            eof=self.closed and after_offset + len(data) == len(self._output),
        )

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        self._output += data

    async def resize(self, size: TerminalSize) -> None:
        self.resizes.append(size)
        self._size = size

    async def send_signal(self, signal: TerminalSignal) -> None:
        del signal

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None:
        del timeout_seconds
        return 143 if self.closed else None

    async def close(self) -> None:
        self.closed = True
        self._manager.sessions.pop(self.session_id, None)


class _Manager(InteractiveTerminalManager):
    def __init__(self, *, output: bytes = b"literal [bold] output\n") -> None:
        self.sessions: dict[str, _Session] = {}
        session = _Session(self, output)
        self.sessions[session.session_id] = session

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
        authorization: Any = None,
    ) -> _Session:
        del call_id, executable, arguments, cwd, env, output_capacity, authorization
        session = _Session(self, b"")
        session._size = size
        self.sessions[session.session_id] = session
        return session

    async def get_session(self, session_id: str) -> _Session | None:
        return self.sessions.get(session_id)

    async def list_sessions(self) -> tuple[_Session, ...]:
        return tuple(self.sessions.values())

    async def shutdown(self) -> None:
        for session in tuple(self.sessions.values()):
            await session.close()


class _Runner:
    def __init__(self, manager: _Manager) -> None:
        self.interactive_terminals = manager
        self.session_id: str | None = None
        self.items: tuple[SessionItem, ...] = ()

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, sink, cancellation_policy
        raise AssertionError("terminal fixture must not start a model turn")

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        raise AssertionError("terminal fixture must not start a background wake")

    async def load_background_wake_state(self) -> BackgroundWakeState:
        raise AssertionError("terminal fixture must not load background state")

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None:
        del state
        raise AssertionError("terminal fixture must not save background state")

    async def compact_now(self) -> Any:
        raise AssertionError("terminal fixture must not compact context")


class AttachedTerminalTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_projects_bounded_output_and_routes_only_focused_input(self) -> None:
        manager = _Manager()
        app = NeuroCodeApp(
            _Runner(manager),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 32)):
            panel = app.query_one(AttachedTerminalPanel)
            await app._poll_attached_terminals()
            self.assertTrue(panel.display)
            self.assertIn(
                "Attached terminals", str(panel.query_one("#attached-terminal-summary").renderable)
            )
            self.assertIn(
                "literal [bold] output",
                str(panel.query_one("#attached-terminal-output").renderable),
            )

            event = AttachedTerminalPanel.InputSubmitted(panel, "before-focus")
            await app.on_attached_terminal_panel_input_submitted(event)
            session = await manager.get_session("terminal-1")
            assert session is not None
            self.assertEqual([], session.writes)

            app.action_focus_attached_terminal()
            await app.on_attached_terminal_panel_input_submitted(
                AttachedTerminalPanel.InputSubmitted(panel, "after-focus")
            )
            self.assertEqual([b"after-focus\n"], session.writes)

            await app._attached_terminal_resize_selected()
            self.assertEqual(1, len(session.resizes))
            self.assertGreaterEqual(session.resizes[0].columns, 1)
            self.assertGreaterEqual(session.resizes[0].rows, 1)

            await app._attached_terminal_stop_selected()
            self.assertTrue(session.closed)
            await app._poll_attached_terminals()
            self.assertFalse(panel.display)

    async def test_empty_panel_uses_the_localized_empty_state(self) -> None:
        manager = _Manager()
        manager.sessions.clear()
        app = NeuroCodeApp(
            _Runner(manager),
            language=UiLanguage.SIMPLIFIED_CHINESE,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(90, 28)):
            panel = app.query_one(AttachedTerminalPanel)
            await app._poll_attached_terminals()
            self.assertEqual(
                "没有附加终端。", str(panel.query_one("#attached-terminal-summary").renderable)
            )
            self.assertFalse(panel.display)


if __name__ == "__main__":
    unittest.main()
