"""Bounded tool-batch scheduling above the existing security pipeline.

有界工具批次调度,位于现有安全流水线之上。

The scheduler never executes a tool itself.  It only decides whether calls
may overlap, while the supplied callback remains responsible for permission,
approval, sandbox, workspace, redaction, and event handling.  Side-effecting
and interaction-control tools are always exclusive even if a definition is
incorrectly marked parallel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from neuro_code.application.ports.tools import InteractionControlTool, ToolCollection
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.tools import ToolExecutionMode

T = TypeVar("T")
ToolRunner = Callable[[ToolCall, bool], Awaitable[T]]


@dataclass(frozen=True, slots=True)
class ToolBatchExecutionError(Exception):
    """Preserve the original failure and the calls not yet started."""

    index: int
    cause: BaseException
    not_started: tuple[ToolCall, ...]

    def __str__(self) -> str:
        return str(self.cause)


def resolved_execution_mode(
    tools: ToolCollection,
    call: ToolCall,
) -> ToolExecutionMode:
    """Resolve a definition fail-closed against the executable tool object."""

    tool = tools.get(call.name)
    if tool is None or tool.side_effecting or isinstance(tool, InteractionControlTool):
        return ToolExecutionMode.EXCLUSIVE
    mode = tool.definition.execution_mode
    if mode is ToolExecutionMode.EXCLUSIVE:
        return mode
    # AUTO and explicit PARALLEL are both safe only for a non-side-effecting
    # executable tool.  The checks above are deliberately repeated at this
    # boundary so an untrusted definition cannot elevate a writer.
    return ToolExecutionMode.PARALLEL


class ToolScheduler[T]:
    """Run independent read-only calls in bounded groups.

    Results retain model order.  A parallel group is isolated by the callback
    so the caller can merge its append-only transcript suffix in that same
    order.  Any call that has entered a parallel group is considered started;
    only later groups are returned as ``not_started``.
    """

    __slots__ = ("_max_parallel", "_tools")

    def __init__(self, tools: ToolCollection, *, max_parallel: int = 4) -> None:
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
            raise TypeError("max_parallel must be an integer")
        if not 1 <= max_parallel <= 16:
            raise ValueError("max_parallel must be between 1 and 16")
        self._tools = tools
        self._max_parallel = max_parallel

    async def run(
        self,
        calls: Sequence[ToolCall],
        runner: ToolRunner[T],
    ) -> tuple[T, ...]:
        normalized = tuple(calls)
        results: list[T] = []
        index = 0
        while index < len(normalized):
            mode = resolved_execution_mode(self._tools, normalized[index])
            if mode is ToolExecutionMode.EXCLUSIVE:
                try:
                    results.append(await runner(normalized[index], False))
                except BaseException as error:
                    raise ToolBatchExecutionError(
                        index,
                        error,
                        normalized[index + 1 :],
                    ) from error
                index += 1
                continue

            end = index
            while end < len(normalized):
                if (
                    resolved_execution_mode(self._tools, normalized[end])
                    is not ToolExecutionMode.PARALLEL
                ):
                    break
                end += 1
            group = normalized[index:end]
            for start in range(0, len(group), self._max_parallel):
                chunk = group[start : start + self._max_parallel]

                async def run_parallel(call: ToolCall) -> T:
                    return await runner(call, True)

                tasks: list[asyncio.Task[T]] = [
                    asyncio.create_task(
                        run_parallel(call),
                        name=f"neuro-code-tool-{call.id}",
                    )
                    for call in chunk
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for offset, outcome in enumerate(outcomes):
                    if isinstance(outcome, BaseException):
                        raise ToolBatchExecutionError(
                            index + start + offset,
                            outcome,
                            (*group[start + len(chunk) :], *normalized[end:]),
                        ) from outcome
                    results.append(outcome)
            index = end
        return tuple(results)


__all__ = [
    "ToolBatchExecutionError",
    "ToolScheduler",
    "resolved_execution_mode",
]
