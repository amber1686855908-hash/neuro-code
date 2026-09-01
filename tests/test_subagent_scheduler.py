from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.workflows.subagent_capabilities import (
    NetworkAccess,
    SubagentCapabilitySet,
)
from neuro_code.application.workflows.subagent_scheduler import (
    SubagentRuntimeScope,
    SubagentScheduler,
    SubagentWorkRequest,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.shared.errors import ConfigurationError


def _capability(
    tools: tuple[str, ...] = ("read_file", "grep"),
    *,
    cwd: Path = Path("/workspace/project"),
    roots: tuple[Path, ...] = (Path("/workspace/project"),),
    sandbox_profile: SandboxProfile | None = None,
    mcp_tools: tuple[str, ...] = (),
    mcp_servers: tuple[str, ...] = (),
    max_steps: int = 8,
) -> SubagentCapabilitySet:
    profile = SandboxProfile.OFF if sandbox_profile is None else sandbox_profile
    tool_set = frozenset(tools)
    network = (
        NetworkAccess.INHERIT
        if tool_set.intersection(
            {"web_fetch", "web_search", "google_search", "url_context", *mcp_tools}
        )
        else NetworkAccess.NONE
    )
    if network is NetworkAccess.NONE and tool_set.intersection(
        {"bash", "create_terminal", "terminal_start"}
    ):
        network = (
            NetworkAccess.ISOLATED if profile.restricts_child_network else NetworkAccess.INHERIT
        )
    return SubagentCapabilitySet(
        allowed_tool_names=tool_set,
        filesystem_read=bool(tool_set.intersection({"read_file", "grep"})),
        filesystem_write=bool(tool_set.intersection({"search_replace", "apply_patch"})),
        bash="bash" in tool_set,
        terminal=bool(tool_set.intersection({"create_terminal", "terminal_start"})),
        background_tasks=bool(tool_set.intersection({"bash", "task_output", "wait_tasks"})),
        mcp_tool_names=frozenset(mcp_tools),
        mcp_server_names=frozenset(mcp_servers),
        network_access=network,
        cwd=cwd,
        workspace_roots=(cwd, *roots),
        sandbox_profile=profile,
        max_steps=max_steps,
    )


def _global_policy() -> SubagentCapabilitySet:
    return _capability(
        (
            "read_file",
            "grep",
            "search_replace",
            "apply_patch",
            "bash",
            "create_terminal",
            "terminal_start",
            "task_output",
            "wait_tasks",
            "mcp_lookup",
            "web_search",
        ),
        cwd=Path("/workspace"),
        roots=(Path("/"),),
        mcp_tools=("mcp_lookup",),
        mcp_servers=("fixture",),
        max_steps=12,
    )


def _request(prompt: str, capabilities: SubagentCapabilitySet | None = None) -> SubagentWorkRequest:
    return SubagentWorkRequest(prompt, capabilities or _capability())


def _scheduler(
    factory: _Factory,
    *,
    parent: SubagentCapabilitySet | None = None,
    **kwargs: object,
) -> SubagentScheduler:
    return SubagentScheduler(
        factory,
        parent_capabilities=parent or _capability(),
        global_policy=_global_policy(),
        **kwargs,
    )


class _Runtime:
    def __init__(
        self,
        session_id: str,
        *,
        capabilities: SubagentCapabilitySet,
        error: BaseException | None = None,
        activity: _Factory | None = None,
    ) -> None:
        self.child_session_id = session_id
        self.error = error
        self.activity = activity
        self.closed = False
        self.capability_fingerprint = capabilities.fingerprint
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
        self,
        request: SubagentWorkRequest,
        *,
        scope: SubagentRuntimeScope,
        capabilities: SubagentCapabilitySet,
    ) -> _Runtime:
        del request, scope
        error = self.errors.pop(0) if self.errors else None
        runtime = _Runtime(
            f"child-{len(self.runtimes)}",
            capabilities=capabilities,
            error=error,
            activity=self,
        )
        self.runtimes.append(runtime)
        return runtime


class SubagentSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_and_scope_bounds_are_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            _request(" ")
        with self.assertRaises(ValueError):
            _request("prompt", _capability(max_steps=0))
        with self.assertRaises(ValueError):
            SubagentRuntimeScope("parent", depth=2, max_depth=1)
        with self.assertRaises(TypeError):
            SubagentWorkRequest("prompt", object())  # type: ignore[arg-type]

    async def test_scheduler_rejects_bad_factory_timeout_and_mismatched_runtime(self) -> None:
        with self.assertRaises(ConfigurationError):
            SubagentScheduler(object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _scheduler(_Factory(), timeout_seconds=0)

        factory = _Factory()
        original_create = factory.create

        async def mismatched_create(
            request: SubagentWorkRequest,
            *,
            scope: SubagentRuntimeScope,
            capabilities: SubagentCapabilitySet,
        ) -> _Runtime:
            runtime = await original_create(request, scope=scope, capabilities=capabilities)
            runtime.child_session_id = scope.parent_session_id
            return runtime

        factory.create = mismatched_create  # type: ignore[method-assign]
        result = await _scheduler(factory).run(
            _request("mismatch"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

        slow_factory = _Factory()
        slow_scheduler = _scheduler(slow_factory, timeout_seconds=0.001)
        timeout_result = await slow_scheduler.run(
            _request("timeout"),
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
            capabilities: SubagentCapabilitySet,
        ) -> _Runtime:
            runtime = await original_create(request, scope=scope, capabilities=capabilities)
            runtime.capability_fingerprint = _capability(("read_file", "grep", "bash")).fingerprint
            runtime.allowed_tool_names = ("write",)
            return runtime

        factory.create = tool_escalating_create  # type: ignore[method-assign]
        result = await _scheduler(factory).run(
            _request("tool scope"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")
        with self.assertRaises(ValueError):
            await _scheduler(factory).run_many(
                tuple(_request(str(index)) for index in range(17)),
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
        scheduler = _scheduler(factory, max_parallel=2)
        requests = tuple(_request(f"prompt-{index}") for index in range(5))
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
        scheduler = _scheduler(factory, max_retries=1)
        result = await scheduler.run(
            _request("retry"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertIsNotNone(result.result)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(factory.runtimes), 2)
        self.assertTrue(all(runtime.closed for runtime in factory.runtimes))

    async def test_scope_rejects_runtime_that_escalates_to_writes(self) -> None:
        factory = _Factory()
        original_create = factory.create

        async def create(
            request: SubagentWorkRequest,
            *,
            scope: SubagentRuntimeScope,
            capabilities: SubagentCapabilitySet,
        ) -> _Runtime:
            runtime = await original_create(request, scope=scope, capabilities=capabilities)
            runtime.capability_fingerprint = _capability(
                ("read_file", "grep", "apply_patch")
            ).fingerprint
            runtime.writes_enabled = True
            return runtime

        factory.create = create  # type: ignore[method-assign]
        scheduler = _scheduler(factory)
        result = await scheduler.run(
            _request("read-only"),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertIsNone(result.result)
        self.assertEqual(result.error_type, "ConfigurationError")


if __name__ == "__main__":
    unittest.main()
