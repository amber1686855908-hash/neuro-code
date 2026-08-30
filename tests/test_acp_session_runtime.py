from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from neuro_code.application.acp.contracts import AcpMcpTools
from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.sessions.binding import (
    ConversationBinding,
    ConversationBindingResourceScope,
)
from neuro_code.interfaces.acp.session import (
    AcpSessionApprovalAlreadyPendingError,
    AcpSessionIdentityConflictError,
    AcpSessionInactiveError,
    AcpSessionPromptAlreadyActiveError,
    AcpSessionRuntime,
)
from neuro_code.interfaces.acp.updates import _AcpEventMapper


class _McpFixture:
    def __init__(self, events: list[str]) -> None:
        self.tools: tuple[Any, ...] = ()
        self.resources: tuple[Any, ...] = ()
        self.resource_templates: tuple[Any, ...] = ()
        self.prompts: tuple[Any, ...] = ()
        self.events = events
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def refresh(self) -> None:
        return

    async def read_resource(self, uri: str) -> tuple[Any, ...]:
        del uri
        return ()

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> tuple[Any, ...]:
        del name, arguments
        return ()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()
        self.events.append("mcp")


class _TerminalFixture:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.events.append("terminal")


def _binding(events: list[str]) -> ConversationBinding:
    async def close_binding() -> None:
        events.append("binding")

    return ConversationBinding(
        cast(Any, object()),
        cast(Any, object()),
        resource_scope=ConversationBindingResourceScope(close_binding),
    )


def _runtime(
    events: list[str],
    *,
    mcp: _McpFixture | None = None,
    terminal: _TerminalFixture | None = None,
) -> AcpSessionRuntime:
    mcp_fixture = mcp if mcp is not None else _McpFixture(events)
    if mcp is None:
        mcp_fixture.close_release.set()
    terminal_fixture = terminal or _TerminalFixture(events)
    return AcpSessionRuntime(
        "acp-runtime-test",
        _binding(events),
        SessionApprovalBroker(),
        32_000,
        cast(AcpMcpTools, mcp_fixture),
        mcp_tool_names=("fixture_tool",),
        client_terminal=cast(ClientTerminal, terminal_fixture),
    )


async def _park(release: asyncio.Event) -> None:
    await release.wait()


async def _finish_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_owns_state_and_only_one_prompt_can_begin() -> None:
    events: list[str] = []
    runtime = _runtime(events)
    release_one = asyncio.Event()
    release_two = asyncio.Event()
    task_one = asyncio.create_task(_park(release_one))
    task_two = asyncio.create_task(_park(release_two))
    mapper_one = cast(_AcpEventMapper, object())
    mapper_two = cast(_AcpEventMapper, object())

    try:
        started = await runtime.begin_prompt(task_one, mapper_one)
        assert started.binding is runtime.binding
        assert started.context_window_tokens == 32_000
        assert started.internal_session_id is None
        assert await runtime.active_binding_snapshot() is runtime.binding
        assert await runtime.mcp_snapshot() is not None

        with pytest.raises(AcpSessionPromptAlreadyActiveError):
            await runtime.begin_prompt(task_two, mapper_two)

        assert await runtime.finish_prompt_if_owner(task_one)
        assert await runtime.begin_prompt(task_two, mapper_two)
        assert not await runtime.finish_prompt_if_owner(task_one)
        assert not await runtime.prompt_should_stop()
        assert await runtime.finish_prompt_if_owner(task_two)
    finally:
        release_one.set()
        release_two.set()
        await _finish_task(task_one)
        await _finish_task(task_two)
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_runtime_cancel_targets_current_prompt_and_races_completion_safely() -> None:
    events: list[str] = []
    runtime = _runtime(events)
    release_one = asyncio.Event()
    release_two = asyncio.Event()
    task_one = asyncio.create_task(_park(release_one))
    task_two = asyncio.create_task(_park(release_two))
    await runtime.begin_prompt(task_one, cast(_AcpEventMapper, object()))
    barrier = asyncio.Barrier(2)

    async def cancel() -> asyncio.Task[Any] | None:
        await barrier.wait()
        return await runtime.request_cancel()

    async def finish() -> bool:
        await barrier.wait()
        return await runtime.finish_prompt_if_owner(task_one)

    cancel_result, finish_result = await asyncio.gather(cancel(), finish())
    assert cancel_result is None or cancel_result is task_one
    assert finish_result in {True, False}
    if not finish_result:
        assert await runtime.finish_prompt_if_owner(task_one)

    assert await runtime.begin_prompt(task_two, cast(_AcpEventMapper, object()))
    assert not await runtime.finish_prompt_if_owner(task_one)
    assert await runtime.request_cancel() is task_two
    assert await runtime.prompt_should_stop()
    assert await runtime.finish_prompt_if_owner(task_two)
    assert await runtime.request_cancel() is None

    release_one.set()
    release_two.set()
    await _finish_task(task_one)
    await _finish_task(task_two)
    await runtime.cleanup()


@pytest.mark.asyncio
async def test_runtime_approval_claim_and_release_are_owner_safe() -> None:
    events: list[str] = []
    runtime = _runtime(events)
    release = asyncio.Event()
    task = asyncio.create_task(_park(release))
    mapper = cast(_AcpEventMapper, object())

    try:
        await runtime.begin_prompt(task, mapper)
        assert await runtime.begin_approval("approval-a") is mapper
        with pytest.raises(AcpSessionApprovalAlreadyPendingError):
            await runtime.begin_approval("approval-b")
        assert not await runtime.finish_approval_if_owner("approval-b")
        with pytest.raises(AcpSessionApprovalAlreadyPendingError):
            await runtime.begin_approval("approval-b")
        assert await runtime.finish_approval_if_owner("approval-a")
        assert await runtime.begin_approval("approval-b") is mapper
        assert await runtime.finish_approval_if_owner("approval-b")
        assert await runtime.finish_prompt_if_owner(task)
    finally:
        release.set()
        await _finish_task(task)
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_runtime_identity_is_reserved_and_cannot_switch() -> None:
    runtime = _runtime([])
    assert await runtime.active_internal_session_identity() is None

    token = await runtime.begin_internal_session_identity("internal-a")
    with pytest.raises(AcpSessionIdentityConflictError):
        await runtime.begin_internal_session_identity("internal-b")
    await runtime.commit_internal_session_identity("internal-a", token)
    assert await runtime.active_internal_session_identity() == "internal-a"

    same_token = await runtime.begin_internal_session_identity("internal-a")
    await runtime.commit_internal_session_identity("internal-a", same_token)
    with pytest.raises(AcpSessionIdentityConflictError):
        await runtime.begin_internal_session_identity("internal-b")

    await runtime.cleanup()


@pytest.mark.asyncio
async def test_concurrent_runtime_cleanup_is_idempotent_and_binding_remains_authority() -> None:
    events: list[str] = []
    mcp = _McpFixture(events)
    terminal = _TerminalFixture(events)
    runtime = _runtime(events, mcp=mcp, terminal=terminal)

    first = asyncio.create_task(runtime.cleanup())
    await mcp.close_started.wait()
    second = asyncio.create_task(runtime.cleanup())
    mcp.close_release.set()
    await asyncio.gather(first, second)
    await runtime.cleanup()

    assert events == ["mcp", "terminal", "binding"]
    assert mcp.close_calls == 1
    assert terminal.shutdown_calls == 1
    assert not await runtime.is_active()
    assert await runtime.active_binding_snapshot() is None


@pytest.mark.asyncio
async def test_closing_runtime_rejects_new_interface_operations() -> None:
    runtime = _runtime([])
    pending_token = await runtime.begin_internal_session_identity("internal")
    started, identity = await runtime.begin_close()
    assert started
    assert identity is None
    assert not await runtime.is_active()
    assert await runtime.request_cancel() is None
    assert await runtime.active_binding_snapshot() is None
    with pytest.raises(AcpSessionInactiveError):
        await runtime.prompt_context()
    with pytest.raises(AcpSessionInactiveError):
        await runtime.commit_internal_session_identity("internal", pending_token)
    with pytest.raises(AcpSessionInactiveError):
        await runtime.begin_internal_session_identity("internal")
    with pytest.raises(AcpSessionInactiveError):
        await runtime.begin_prompt(
            cast(asyncio.Task[Any], asyncio.current_task()),
            cast(_AcpEventMapper, object()),
        )
    assert await runtime.begin_approval("approval") is None
    await runtime.cleanup()
