"""Explicit bounded scheduler for isolated subagent runtimes.

显式且有界的隔离子代理运行时调度器.

The scheduler owns only concurrency, retry, depth, and capability checks. A
factory remains responsible for creating a fresh child conversation and for
enforcing the selected permission/tool set at the runtime boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.workflows.subagent import MAX_SUBAGENT_PROMPT_BYTES, MAX_SUBAGENT_STEPS
from neuro_code.shared.errors import ConfigurationError

MAX_SCHEDULED_SUBAGENTS = 16
MAX_SUBAGENT_PARALLELISM = 4
MAX_SUBAGENT_RETRIES = 2
MAX_SUBAGENT_DEPTH = 4


@dataclass(frozen=True, slots=True)
class SubagentRuntimeScope:
    """Capabilities inherited by exactly one explicitly created child."""

    parent_session_id: str
    depth: int = 0
    max_depth: int = 1
    recursive: bool = False
    allow_writes: bool = False
    allowed_tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.parent_session_id, str) or not self.parent_session_id.strip():
            raise ValueError("subagent scope requires a parent session")
        if "\x00" in self.parent_session_id:
            raise ValueError("subagent scope parent session is invalid")
        for name, value, lower, upper in (
            ("depth", self.depth, 0, MAX_SUBAGENT_DEPTH),
            ("max_depth", self.max_depth, 0, MAX_SUBAGENT_DEPTH),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"subagent scope {name} is out of bounds")
        if self.depth > self.max_depth:
            raise ValueError("subagent scope depth exceeds max_depth")
        if not isinstance(self.recursive, bool) or not isinstance(self.allow_writes, bool):
            raise TypeError("subagent scope flags must be bools")
        names = tuple(self.allowed_tool_names)
        if len(names) > 128 or len(set(names)) != len(names):
            raise ValueError("subagent scope tool set is invalid")
        if not all(isinstance(name, str) and name and "\x00" not in name for name in names):
            raise ValueError("subagent scope tool set is invalid")
        object.__setattr__(self, "allowed_tool_names", names)

    def child(self, parent_session_id: str) -> SubagentRuntimeScope:
        """Derive a child scope, enforcing the recursive-spawn gate."""

        if not self.recursive:
            raise ConfigurationError("recursive subagents are disabled")
        if self.depth >= self.max_depth:
            raise ConfigurationError("subagent depth limit reached")
        return SubagentRuntimeScope(
            parent_session_id=parent_session_id,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            recursive=self.recursive,
            allow_writes=self.allow_writes,
            allowed_tool_names=self.allowed_tool_names,
        )


@dataclass(frozen=True, slots=True)
class SubagentWorkRequest:
    """One bounded independent child prompt."""

    prompt: str
    max_steps: int = 8

    def __post_init__(self) -> None:
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or "\x00" in self.prompt
            or len(self.prompt.encode("utf-8")) > MAX_SUBAGENT_PROMPT_BYTES
        ):
            raise ValueError("subagent prompt must be non-empty and bounded")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_SUBAGENT_STEPS
        ):
            raise ValueError("subagent max_steps is out of bounds")


@runtime_checkable
class ScopedSubagentRuntime(Protocol):
    """Fresh runtime returned by a scope-aware factory."""

    @property
    def child_session_id(self) -> str: ...

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult: ...

    async def close(self) -> None: ...


@runtime_checkable
class ScopedSubagentRuntimeFactory(Protocol):
    async def create(
        self,
        request: SubagentWorkRequest,
        *,
        scope: SubagentRuntimeScope,
    ) -> ScopedSubagentRuntime: ...


@dataclass(frozen=True, slots=True)
class ScheduledSubagentResult:
    """Bounded scheduler result; raw child transcript stays with the caller."""

    request_index: int
    result: AgentRunResult | None
    attempts: int
    error_type: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.request_index, bool) or self.request_index < 0:
            raise ValueError("subagent result index is invalid")
        if isinstance(self.attempts, bool) or not 1 <= self.attempts <= MAX_SUBAGENT_RETRIES + 1:
            raise ValueError("subagent result attempts are invalid")
        if (self.result is None) == (self.error_type is None):
            raise ValueError("subagent result must contain exactly one result or error")
        if self.error_type is not None and (
            not isinstance(self.error_type, str)
            or not self.error_type
            or len(self.error_type) > 128
        ):
            raise ValueError("subagent result error type is invalid")


class SubagentScheduler:
    """Run explicit child requests with bounded parallelism and fresh retries."""

    def __init__(
        self,
        factory: ScopedSubagentRuntimeFactory,
        *,
        max_parallel: int = MAX_SUBAGENT_PARALLELISM,
        max_retries: int = 0,
        timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(factory, ScopedSubagentRuntimeFactory):
            raise ConfigurationError("subagent runtime factory is invalid")
        if isinstance(max_parallel, bool) or not 1 <= max_parallel <= MAX_SUBAGENT_PARALLELISM:
            raise ValueError("subagent max_parallel is out of bounds")
        if isinstance(max_retries, bool) or not 0 <= max_retries <= MAX_SUBAGENT_RETRIES:
            raise ValueError("subagent max_retries is out of bounds")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("subagent timeout_seconds must be positive")
        self._factory = factory
        self._max_parallel = max_parallel
        self._max_retries = max_retries
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds is not None else None

    @staticmethod
    def _validate_runtime(runtime: ScopedSubagentRuntime, scope: SubagentRuntimeScope) -> None:
        if not isinstance(runtime, ScopedSubagentRuntime):
            raise ConfigurationError("subagent factory returned an invalid runtime")
        if not runtime.child_session_id or runtime.child_session_id == scope.parent_session_id:
            raise ConfigurationError("subagent runtime returned an invalid child session")
        writes_enabled = getattr(runtime, "writes_enabled", False)
        if writes_enabled is True and not scope.allow_writes:
            raise ConfigurationError("subagent runtime exceeded its read-only scope")
        runtime_tools = getattr(runtime, "allowed_tool_names", None)
        if (
            scope.allowed_tool_names
            and runtime_tools is not None
            and not set(runtime_tools).issubset(scope.allowed_tool_names)
        ):
            raise ConfigurationError("subagent runtime exceeded its tool scope")

    async def run(
        self,
        request: SubagentWorkRequest,
        *,
        scope: SubagentRuntimeScope,
        request_index: int = 0,
        sink: EventSink | None = None,
    ) -> ScheduledSubagentResult:
        if not isinstance(request, SubagentWorkRequest):
            raise TypeError("subagent request must be canonical")
        if not isinstance(scope, SubagentRuntimeScope):
            raise TypeError("subagent scope must be canonical")
        last_error: BaseException | None = None
        for attempt in range(1, self._max_retries + 2):
            runtime: ScopedSubagentRuntime | None = None
            try:
                runtime = await self._factory.create(request, scope=scope)
                self._validate_runtime(runtime, scope)
                if self._timeout_seconds is None:
                    result = await runtime.run(request.prompt, sink=sink)
                else:
                    async with asyncio.timeout(self._timeout_seconds):
                        result = await runtime.run(request.prompt, sink=sink)
                if result.session_id != runtime.child_session_id:
                    raise ConfigurationError("subagent runtime returned a mismatched session")
                return ScheduledSubagentResult(request_index, result, attempt)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
            finally:
                if runtime is not None:
                    await asyncio.gather(runtime.close(), return_exceptions=True)
        assert last_error is not None
        return ScheduledSubagentResult(
            request_index, None, self._max_retries + 1, type(last_error).__name__
        )

    async def run_many(
        self,
        requests: Sequence[SubagentWorkRequest],
        *,
        scope: SubagentRuntimeScope,
        sink: EventSink | None = None,
    ) -> tuple[ScheduledSubagentResult, ...]:
        if len(requests) > MAX_SCHEDULED_SUBAGENTS:
            raise ValueError("too many scheduled subagents")
        semaphore = asyncio.Semaphore(self._max_parallel)

        async def run_one(index: int, request: SubagentWorkRequest) -> ScheduledSubagentResult:
            async with semaphore:
                return await self.run(request, scope=scope, request_index=index, sink=sink)

        return tuple(
            await asyncio.gather(
                *(run_one(index, request) for index, request in enumerate(requests))
            )
        )


__all__ = [
    "MAX_SCHEDULED_SUBAGENTS",
    "MAX_SUBAGENT_DEPTH",
    "MAX_SUBAGENT_PARALLELISM",
    "MAX_SUBAGENT_RETRIES",
    "ScheduledSubagentResult",
    "ScopedSubagentRuntime",
    "ScopedSubagentRuntimeFactory",
    "SubagentRuntimeScope",
    "SubagentScheduler",
    "SubagentWorkRequest",
]
