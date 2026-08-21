from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.workflows.subagent_capabilities import (
    NetworkAccess,
    SubagentCapabilitySet,
    _intersect_roots,
    _sandbox_satisfies,
)
from neuro_code.application.workflows.subagent_scheduler import (
    ScopedSubagentRuntime,
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
    sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    mcp_tools: tuple[str, ...] = (),
    mcp_servers: tuple[str, ...] = (),
    max_steps: int = 8,
) -> SubagentCapabilitySet:
    return SubagentCapabilitySet.from_runtime(
        tool_names=tools,
        cwd=cwd,
        additional_workspace_roots=roots,
        sandbox_profile=sandbox_profile,
        enable_background_tasks="bash" in tools,
        max_steps=max_steps,
        mcp_tool_names=mcp_tools,
        mcp_server_names=mcp_servers,
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
            "mcp_lookup",
            "web_search",
        ),
        cwd=Path("/workspace"),
        roots=(Path("/"),),
        mcp_tools=("mcp_lookup",),
        mcp_servers=("fixture",),
        max_steps=12,
    )


class _Runtime:
    def __init__(self, capabilities: SubagentCapabilitySet, *, error: BaseException | None = None):
        self.child_session_id = "child"
        self.capability_fingerprint = capabilities.fingerprint
        self.error = error
        self.closed = False

    async def run(self, prompt: str, *, sink: object = None) -> AgentRunResult:
        del sink
        if self.error is not None:
            raise self.error
        return AgentRunResult(self.child_session_id, prompt, (), (), (), 1)

    async def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, errors: list[BaseException | None] | None = None):
        self.errors = list(errors or [])
        self.received: list[SubagentCapabilitySet] = []

    async def create(
        self,
        request: SubagentWorkRequest,
        *,
        scope: SubagentRuntimeScope,
        capabilities: SubagentCapabilitySet,
    ) -> _Runtime:
        del request, scope
        self.received.append(capabilities)
        error = self.errors.pop(0) if self.errors else None
        return _Runtime(capabilities, error=error)


def _scheduler(factory: _Factory, parent: SubagentCapabilitySet) -> SubagentScheduler:
    return SubagentScheduler(
        factory,
        parent_capabilities=parent,
        global_policy=_global_policy(),
    )


class SubagentCapabilityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_escalation_is_rejected_before_factory_creation(self) -> None:
        factory = _Factory()
        parent = _capability()
        result = await _scheduler(factory, parent).run(
            SubagentWorkRequest("tool", _capability(("read_file", "grep", "bash"))),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")
        self.assertEqual(factory.received, [])

    async def test_write_escalation_is_rejected_for_read_only_parent(self) -> None:
        parent = _capability()
        result = await _scheduler(parent=parent, factory=_Factory()).run(
            SubagentWorkRequest("write", _capability(("read_file", "grep", "apply_patch"))),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_terminal_escalation_is_rejected(self) -> None:
        parent = _capability()
        result = await _scheduler(_Factory(), parent).run(
            SubagentWorkRequest(
                "terminal",
                _capability(("read_file", "grep", "create_terminal", "terminal_start")),
            ),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_mcp_server_escalation_is_rejected(self) -> None:
        parent = _capability()
        result = await _scheduler(_Factory(), parent).run(
            SubagentWorkRequest(
                "mcp",
                _capability(
                    ("read_file", "grep", "mcp_lookup"),
                    mcp_tools=("mcp_lookup",),
                    mcp_servers=("fixture",),
                ),
            ),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_workspace_root_expansion_is_rejected(self) -> None:
        parent = _capability()
        result = await _scheduler(_Factory(), parent).run(
            SubagentWorkRequest(
                "workspace",
                _capability(
                    cwd=Path("/workspace"),
                    roots=(Path("/workspace"),),
                ),
            ),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_sandbox_downgrade_is_rejected(self) -> None:
        parent = _capability(sandbox_profile=SandboxProfile.READ_ONLY)
        result = await _scheduler(_Factory(), parent).run(
            SubagentWorkRequest(
                "sandbox",
                _capability(sandbox_profile=SandboxProfile.OFF),
            ),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_runtime_without_capability_metadata_fails_closed(self) -> None:
        class MissingMetadataRuntime:
            child_session_id = "child"

            async def run(
                self,
                prompt: str,
                *,
                sink: object = None,
            ) -> AgentRunResult:
                del sink
                return AgentRunResult("child", prompt, (), (), (), 1)

            async def close(self) -> None:
                return None

        class MissingMetadataFactory(_Factory):
            async def create(
                self,
                request: SubagentWorkRequest,
                *,
                scope: SubagentRuntimeScope,
                capabilities: SubagentCapabilitySet,
            ) -> ScopedSubagentRuntime:
                del request, scope, capabilities
                return MissingMetadataRuntime()

        result = await _scheduler(MissingMetadataFactory(), _capability()).run(
            SubagentWorkRequest("missing", _capability()),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_broken_factory_fingerprint_fails_closed(self) -> None:
        class EscalatingFactory(_Factory):
            async def create(
                self,
                request: SubagentWorkRequest,
                *,
                scope: SubagentRuntimeScope,
                capabilities: SubagentCapabilitySet,
            ) -> _Runtime:
                del request, scope
                expanded = _capability(("read_file", "grep", "bash"))
                self.received.append(capabilities)
                return _Runtime(expanded)

        result = await _scheduler(EscalatingFactory(), _capability()).run(
            SubagentWorkRequest("broken", _capability()),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertEqual(result.error_type, "ConfigurationError")

    async def test_retry_reuses_one_effective_capability_fingerprint(self) -> None:
        factory = _Factory([RuntimeError("first attempt")])
        result = await SubagentScheduler(
            factory,
            parent_capabilities=_capability(),
            global_policy=_global_policy(),
            max_retries=1,
        ).run(
            SubagentWorkRequest("retry", _capability()),
            scope=SubagentRuntimeScope("parent"),
        )
        self.assertIsNotNone(result.result)
        self.assertEqual(len(factory.received), 2)
        self.assertEqual(factory.received[0].fingerprint, factory.received[1].fingerprint)

    async def test_recursive_scope_cannot_expand_depth_or_enable_recursion(self) -> None:
        restricted = SubagentRuntimeScope("parent", max_depth=2, recursive=False)
        with self.assertRaises(ConfigurationError):
            restricted.child("child")
        recursive = SubagentRuntimeScope("parent", max_depth=2, recursive=True)
        child = recursive.child("child")
        self.assertEqual(child.max_depth, recursive.max_depth)
        with self.assertRaises(ConfigurationError):
            child.child("grandchild").child("great-grandchild")

    def test_missing_parent_or_global_capability_metadata_is_not_allowed(self) -> None:
        with self.assertRaises(ConfigurationError):
            SubagentScheduler(_Factory())
        with self.assertRaises(ConfigurationError):
            SubagentCapabilitySet.resolve_child(
                parent=_capability(),
                requested=_capability(),
                global_policy=None,  # type: ignore[arg-type]
            )

    def test_capability_is_immutable_comparable_intersectable_and_fingerprintable(self) -> None:
        capability = _capability()
        self.assertEqual(capability, capability.intersection(capability))
        self.assertTrue(capability <= capability)
        self.assertEqual(capability.fingerprint, capability.fingerprint)
        with self.assertRaises(AttributeError):
            capability.max_steps = 1  # type: ignore[misc]

    def test_invalid_capability_metadata_fails_closed(self) -> None:
        capability = _capability()
        invalid_factories = (
            lambda: replace(capability, cwd="not-a-path"),
            lambda: replace(capability, filesystem_read=False),
            lambda: replace(
                _capability(("read_file", "grep", "apply_patch")),
                filesystem_write=False,
            ),
            lambda: replace(_capability(("read_file", "grep", "bash")), bash=False),
            lambda: replace(
                _capability(("read_file", "grep", "terminal_exec")),
                terminal=False,
            ),
            lambda: replace(capability, background_tasks=True),
            lambda: replace(capability, mcp_tool_names=frozenset({"mcp_lookup"})),
            lambda: replace(capability, mcp_server_names=frozenset({"fixture"})),
            lambda: replace(capability, network_access=cast(NetworkAccess, object())),
            lambda: replace(
                SubagentCapabilitySet(
                    allowed_tool_names=frozenset({"web_search"}),
                    filesystem_read=False,
                    filesystem_write=False,
                    bash=False,
                    terminal=False,
                    background_tasks=False,
                    mcp_tool_names=frozenset(),
                    mcp_server_names=frozenset(),
                    network_access=NetworkAccess.NONE,
                    cwd=Path("/workspace/project"),
                    workspace_roots=(Path("/workspace/project"),),
                    sandbox_profile=SandboxProfile.OFF,
                    max_steps=8,
                ),
                network_access=NetworkAccess.NONE,
            ),
            lambda: replace(capability, workspace_roots=()),
            lambda: replace(
                capability,
                workspace_roots=(Path("/workspace"), Path("/workspace/project")),
            ),
            lambda: replace(capability, sandbox_profile=cast(SandboxProfile, object())),
        )
        for factory in invalid_factories:
            with self.assertRaises((ConfigurationError, TypeError)):
                factory()

        with self.assertRaises(ConfigurationError):
            SubagentCapabilitySet(
                allowed_tool_names=frozenset(f"tool-{index}" for index in range(257)),
                filesystem_read=False,
                filesystem_write=False,
                bash=False,
                terminal=False,
                background_tasks=False,
                mcp_tool_names=frozenset(),
                mcp_server_names=frozenset(),
                network_access=NetworkAccess.NONE,
                cwd=Path("/workspace/project"),
                workspace_roots=(Path("/workspace/project"),),
                sandbox_profile=SandboxProfile.OFF,
                max_steps=8,
            )
        with self.assertRaises(ConfigurationError):
            replace(capability, allowed_tool_names=frozenset({""}))

        with (
            patch.object(Path, "resolve", side_effect=OSError("resolve failed")),
            self.assertRaises(ConfigurationError),
        ):
            replace(capability, cwd=Path("/workspace/project"))
        with (
            patch.object(Path, "resolve", return_value=Path("relative")),
            self.assertRaises(ConfigurationError),
        ):
            replace(capability, cwd=Path("relative"))

        with self.assertRaises(TypeError):
            _sandbox_satisfies(cast(SandboxProfile, object()), SandboxProfile.OFF)

        with self.assertRaises(TypeError):
            SubagentCapabilitySet.from_runtime(
                tool_names=("read_file",),
                cwd=Path("/workspace/project"),
                sandbox_profile=SandboxProfile.OFF,
                enable_background_tasks=cast(bool, object()),
                max_steps=8,
            )
        with self.assertRaises(ConfigurationError):
            SubagentCapabilitySet.from_runtime(
                tool_names=("read_file",),
                cwd=Path("/workspace/project"),
                sandbox_profile=SandboxProfile.OFF,
                enable_background_tasks=True,
                max_steps=8,
            )
        with self.assertRaises(ValueError):
            SubagentCapabilitySet.from_runtime(
                tool_names=("read_file",),
                cwd=Path("/workspace/project"),
                sandbox_profile=SandboxProfile.OFF,
                enable_background_tasks=False,
                max_steps=True,
            )
        with self.assertRaises(ValueError):
            SubagentCapabilitySet.from_runtime(
                tool_names=("read_file",),
                cwd=Path("/workspace/project"),
                sandbox_profile=SandboxProfile.OFF,
                enable_background_tasks=False,
                max_steps=97,
            )

    def test_capability_intersection_and_resolution_fail_closed_on_incompatible_inputs(
        self,
    ) -> None:
        parent = _capability(
            cwd=Path("/workspace/project"),
            roots=(Path("/workspace/project"), Path("/workspace")),
        )
        child = _capability(
            cwd=Path("/workspace/project/src"),
            roots=(Path("/workspace/project/src"),),
        )
        intersection = parent.intersection(child)
        self.assertEqual(intersection.cwd, child.cwd)
        self.assertEqual(intersection.workspace_roots, child.workspace_roots)
        with self.assertRaises(ConfigurationError):
            _intersect_roots((Path("/workspace/project"),), (Path("/unrelated"),))
        with self.assertRaises(ConfigurationError):
            _capability(cwd=Path("/workspace/project")).intersection(
                _capability(cwd=Path("/unrelated"), roots=(Path("/unrelated"),))
            )
        read_only = _capability(sandbox_profile=SandboxProfile.READ_ONLY)
        strict = _capability(sandbox_profile=SandboxProfile.STRICT)
        self.assertEqual(read_only.intersection(strict).sandbox_profile, SandboxProfile.READ_ONLY)
        with self.assertRaises(TypeError):
            capability = _capability()
            capability.is_subset_of(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            capability = _capability()
            capability.intersection(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SubagentCapabilitySet.resolve_child(
                parent=object(),  # type: ignore[arg-type]
                requested=_capability(),
                global_policy=_global_policy(),
            )
        with self.assertRaises(TypeError):
            SubagentCapabilitySet.resolve_child(
                parent=_capability(),
                requested=object(),  # type: ignore[arg-type]
                global_policy=_global_policy(),
            )
        with self.assertRaises(ConfigurationError):
            SubagentCapabilitySet.resolve_child(
                parent=_capability(),
                requested=_capability(),
                global_policy=None,  # type: ignore[arg-type]
            )
        with self.assertRaises(ConfigurationError):
            SubagentCapabilitySet.resolve_child(
                parent=_capability(
                    ("read_file", "grep", "apply_patch"),
                ),
                requested=_capability(("read_file", "grep", "apply_patch")),
                global_policy=_capability(),
            )


if __name__ == "__main__":
    unittest.main()
