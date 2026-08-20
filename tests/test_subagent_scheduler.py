from __future__ import annotations

import asyncio
import unittest

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.workflows.subagent_scheduler import (
    SubagentRuntimeScope,
    SubagentScheduler,
    SubagentWorkRequest,
)
from neuro_code.shared.errors import ConfigurationError


class _Runtime:
    def __init__(
        self,
        session_id: str,
        *,
        error: BaseException | None = None,
        activity: _Factory | None = None,
    ) -> None:
        self.child_session_id = session_id
        self.error = error
        self.activity = activity
        self.closed = False
        self.writes_enabled = False
        self.allowed_tool_names: tuple[str, ...] = ()

    async def run(self, prompt: str, *, sink: object = None) -> AgentRunResult:
        del sink
        if self.error is not None:
            raise self.error
        if self.activity is not None:
            self.activity.active += 1
            self.activity.maximum = max(self.activity.maximum, self.activity.active)
        try:
            await asyncio.sleep(0.005)
            return AgentRunResult(self.child_session_id, prompt, (), (), (), 1)
        finally:
            if self.activity is not None:
                self.activity.active -= 1

    async def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, errors: list[BaseException | None] | None = None) -> None:
        self.errors = list(errors or [])
        self.runtimes: list[_Runtime] = []
        self.active = 0
        self.maximum = 0

    async def create(
        self, request: SubagentWorkRequest, *, scope: SubagentRuntimeScope
    ) -> _Runtime:
        del request, scope
        error = self.errors.pop(0) if self.errors else None
        runtime = _Runtime(
            f"child-{len(self.runtimes)}",
            error=error,
            activity=self,
        )
        self.runtimes.append(runtime)
        return runtime


class SubagentSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_and_scope_bounds_are_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            SubagentWorkRequest(" ")
        with self.assertRaises(ValueError):
            SubagentWorkRequest("prompt", max_steps=0)
        with self.assertRaises(ValueError):
            SubagentRuntimeScope("parent", depth=2, max_depth=1)
        with self.assertRaises(ValueError):
            SubagentRuntimeScope("parent", allowed_tool_names=("read", "read"))

    async def test_scheduler_rejects_bad_factory_timeout_and_mismatched_runtime(self) -> None:
        with self.assertRaises(ConfigurationError):
            SubagentScheduler(object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SubagentScheduler(_Factory(), timeout_seconds=0)

        factory = _Factory()
        original_create = factory.create

        async def mismatched_create(
            request: SubagentWorkRequest,
            *,
            scope: SubagentRuntimeScope,
        ) -> _Runtime:
            runtime = await original_create(request, scope=scope)
            runtime.child_session_id = scope.parent_session_id
            return runtime

        factory.create = mismatched_create  # type: ignore[method-assign]
        result = await SubagentScheduler(factory).run(
            SubagentWorkRequest("mismatch"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

        slow_factory = _Factory()
        slow_scheduler = SubagentScheduler(slow_factory, timeout_seconds=0.001)
        timeout_result = await slow_scheduler.run(
            SubagentWorkRequest("timeout"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(timeout_result.error_type, "TimeoutError")

    async def test_scheduler_rejects_runtime_tool_escalation_and_too_many_requests(self) -> None:
        factory = _Factory()
        original_create = factory.create

        async def tool_escalating_create(
            request: SubagentWorkRequest,
            *,
            scope: SubagentRuntimeScope,
        ) -> _Runtime:
            runtime = await original_create(request, scope=scope)
            runtime.allowed_tool_names = ("write",)
            return runtime

        factory.create = tool_escalating_create  # type: ignore[method-assign]
        result = await SubagentScheduler(factory).run(
            SubagentWorkRequest("tool scope"),
            scope=SubagentRuntimeScope("parent", allowed_tool_names=("read",)),
        )
        self.assertEqual(result.error_type, "ConfigurationError")
        with self.assertRaises(ValueError):
            await SubagentScheduler(factory).run_many(
                tuple(SubagentWorkRequest(str(index)) for index in range(17)),
                scope=SubagentRuntimeScope("parent"),
            )

    def test_scope_requires_explicit_recursive_and_depth_permission(self) -> None:
        scope = SubagentRuntimeScope("parent", recursive=True, max_depth=2)
        child = scope.child("child")
        self.assertEqual(child.depth, 1)
        self.assertEqual(child.parent_session_id, "child")
        with self.assertRaises(ConfigurationError):
            SubagentRuntimeScope("parent").child("child")
        with self.assertRaises(ConfigurationError):
            child.child("grandchild").child("great-grandchild")

    async def test_run_many_is_bounded_and_keeps_request_order(self) -> None:
        factory = _Factory()
        scheduler = SubagentScheduler(factory, max_parallel=2)
        requests = tuple(SubagentWorkRequest(f"prompt-{index}") for index in range(5))
        results = await scheduler.run_many(
            requests,
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual([result.request_index for result in results], list(range(5)))
        self.assertEqual(
            [result.result.response for result in results if result.result],
            [f"prompt-{index}" for index in range(5)],
        )
        self.assertGreater(factory.maximum, 1)
        self.assertLessEqual(factory.maximum, 2)

    async def test_retry_uses_a_fresh_runtime_and_closes_each_attempt(self) -> None:
        factory = _Factory([RuntimeError("transient")])
        scheduler = SubagentScheduler(factory, max_retries=1)
        result = await scheduler.run(
            SubagentWorkRequest("retry"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertIsNotNone(result.result)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(factory.runtimes), 2)
        self.assertTrue(all(runtime.closed for runtime in factory.runtimes))

    async def test_scope_rejects_runtime_that_escalates_to_writes(self) -> None:
        factory = _Factory()
        original_create = factory.create

        async def create(request: SubagentWorkRequest, *, scope: SubagentRuntimeScope) -> _Runtime:
            runtime = await original_create(request, scope=scope)
            runtime.writes_enabled = True
            return runtime

        factory.create = create  # type: ignore[method-assign]
        scheduler = SubagentScheduler(factory)
        result = await scheduler.run(
            SubagentWorkRequest("read-only"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertIsNone(result.result)
        self.assertEqual(result.error_type, "ConfigurationError")


if __name__ == "__main__":
    unittest.main()
