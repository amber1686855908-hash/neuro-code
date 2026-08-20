from __future__ import annotations

import asyncio
import unittest

from neuro_code.application.runtime.tool_scheduler import (
    ToolBatchExecutionError,
    ToolScheduler,
)
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode


class _Tool:
    def __init__(self, name: str, *, side_effecting: bool, mode: ToolExecutionMode) -> None:
        self.definition = ToolDefinition(name, name, {}, execution_mode=mode)
        self.side_effecting = side_effecting


class _Tools:
    def __init__(self, *tools: _Tool) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> _Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


def _call(name: str) -> ToolCall:
    return ToolCall(f"call-{name}", name, {})


class ToolSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_calls_are_bounded_and_exclusive_tools_wait(self) -> None:
        tools = _Tools(
            _Tool("read-a", side_effecting=False, mode=ToolExecutionMode.PARALLEL),
            _Tool("read-b", side_effecting=False, mode=ToolExecutionMode.PARALLEL),
            _Tool("write", side_effecting=True, mode=ToolExecutionMode.PARALLEL),
        )
        scheduler = ToolScheduler(tools, max_parallel=2)
        active = 0
        maximum = 0
        order: list[str] = []

        async def runner(call: ToolCall, isolated: bool) -> str:
            nonlocal active, maximum
            self.assertEqual(isolated, call.name.startswith("read"))
            active += 1
            maximum = max(maximum, active)
            order.append(f"start:{call.name}")
            await asyncio.sleep(0.005)
            order.append(f"done:{call.name}")
            active -= 1
            return call.id

        calls = (_call("read-a"), _call("read-b"), _call("write"))
        results = await scheduler.run(calls, runner)
        self.assertEqual(results, tuple(call.id for call in calls))
        self.assertEqual(maximum, 2)
        self.assertGreater(order.index("start:write"), order.index("done:read-a"))
        self.assertGreater(order.index("start:write"), order.index("done:read-b"))

    async def test_failure_reports_calls_not_started_after_the_failed_chunk(self) -> None:
        tools = _Tools(
            *(
                _Tool(name, side_effecting=False, mode=ToolExecutionMode.PARALLEL)
                for name in ("ok", "bad", "later-a", "later-b")
            )
        )
        scheduler = ToolScheduler(tools, max_parallel=2)

        async def runner(call: ToolCall, isolated: bool) -> str:
            del isolated
            if call.name == "bad":
                raise RuntimeError("failure")
            return call.id

        calls = tuple(_call(name) for name in ("ok", "bad", "later-a", "later-b"))
        with self.assertRaises(ToolBatchExecutionError) as raised:
            await scheduler.run(calls, runner)
        error = raised.exception
        self.assertEqual(error.index, 1)
        self.assertEqual(tuple(call.name for call in error.not_started), ("later-a", "later-b"))


if __name__ == "__main__":
    unittest.main()
