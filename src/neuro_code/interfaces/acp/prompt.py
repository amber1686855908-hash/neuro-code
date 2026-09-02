"""ACP prompt, permission, identity capture, and cancellation control."""

from __future__ import annotations

import asyncio
from typing import Any

from acp.exceptions import RequestError
from acp.schema import PermissionOption, PromptResponse

from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.interfaces.acp.content import PromptBlock, convert_prompt_content
from neuro_code.interfaces.acp.errors import (
    SESSION_BUSY,
)
from neuro_code.interfaces.acp.errors import (
    session_not_active as _session_not_active,
)
from neuro_code.interfaces.acp.negotiation import AcpConnectionState
from neuro_code.interfaces.acp.serialization import (
    execution_outcome_metadata,
    execution_outcome_stop_reason,
)
from neuro_code.interfaces.acp.session import (
    AcpSessionApprovalAlreadyPendingError,
    AcpSessionIdentityConflictError,
    AcpSessionInactiveError,
    AcpSessionPromptAlreadyActiveError,
    AcpSessionRuntime,
)
from neuro_code.interfaces.acp.session_registry import (
    ACP_SESSION_ALIAS_NAMESPACE,
    AcpSessionRegistry,
)
from neuro_code.interfaces.acp.updates import _AcpEventMapper
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError


class AcpPromptController:
    """Own ACP turn execution and its connection-facing approval flow."""

    __slots__ = ("_connection", "_registry", "_service")

    def __init__(
        self,
        service: AcpApplicationService,
        registry: AcpSessionRegistry,
        connection: AcpConnectionState,
    ) -> None:
        self._service = service
        self._registry = registry
        self._connection = connection

    async def _bind_internal_session(
        self,
        session: AcpSessionRuntime,
        internal_session_id: str,
    ) -> None:
        try:
            token = await session.begin_internal_session_identity(internal_session_id)
        except (AcpSessionIdentityConflictError, AcpSessionInactiveError) as error:
            raise SessionError(str(error)) from None
        try:
            await self._service.bind_session_alias(
                ACP_SESSION_ALIAS_NAMESPACE,
                session.session_id,
                internal_session_id,
            )
            try:
                await session.commit_internal_session_identity(internal_session_id, token)
            except (AcpSessionIdentityConflictError, AcpSessionInactiveError) as error:
                raise SessionError(str(error)) from None
        except BaseException:
            await session.abort_internal_session_identity(token)
            raise

    async def _capture_runner_session(
        self,
        session: AcpSessionRuntime,
        binding: ConversationBinding,
        *,
        suppress_errors: bool,
    ) -> None:
        internal_session_id = binding.runner.session_id
        if internal_session_id is None:
            return
        try:
            await asyncio.shield(self._bind_internal_session(session, internal_session_id))
        except Exception:
            if not suppress_errors:
                raise

    async def prompt(
        self,
        session_id: str,
        prompt: list[PromptBlock],
        **_kwargs: Any,
    ) -> PromptResponse:
        self._connection.require_initialized()
        session = await self._registry.active(session_id)
        converted = convert_prompt_content(prompt)
        current_task = asyncio.current_task()
        if current_task is None:
            raise RequestError.internal_error({"reason": "prompt_task_unavailable"})
        client = self._connection.client
        if client is None:
            raise RequestError.internal_error({"reason": "client_unavailable"})
        try:
            context_window_tokens, _internal_session_id = await session.prompt_context()
        except AcpSessionInactiveError:
            raise _session_not_active(session_id) from None
        mapper = _AcpEventMapper(
            client=client,
            session_id=session_id,
            context_window_tokens=context_window_tokens,
            explicit_redactions=self._connection.explicit_redactions(),
            on_session_started=lambda internal_id: self._bind_internal_session(
                session,
                internal_id,
            ),
        )
        try:
            prompt_start = await session.begin_prompt(current_task, mapper)
        except AcpSessionInactiveError:
            raise _session_not_active(session_id) from None
        except AcpSessionPromptAlreadyActiveError:
            raise RequestError(
                SESSION_BUSY,
                "Session already has an active prompt",
                {"reason": "prompt_already_active"},
            ) from None
        binding = prompt_start.binding

        try:
            turn_service = self._service.bind_runner(binding.runner)
            result = await turn_service.run_turn(
                RunTurnRequest(
                    converted.content,
                    content_parts=converted.content_parts,
                    expected_session_id=prompt_start.internal_session_id,
                ),
                sink=mapper,
            )
            if result.session_id is None:
                raise RequestError.internal_error({"reason": "session_identity_unavailable"})
            await self._bind_internal_session(session, result.session_id)
            if await session.prompt_should_stop():
                return PromptResponse(stop_reason="cancelled")
            return PromptResponse(
                stop_reason=execution_outcome_stop_reason(result.outcome) or mapper.stop_reason,
                field_meta=execution_outcome_metadata(result.outcome),
            )
        except asyncio.CancelledError:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            return PromptResponse(stop_reason="cancelled")
        except ProviderError as error:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if await session.prompt_should_stop():
                return PromptResponse(stop_reason="cancelled")
            if "exceeded the maximum" in str(error):
                return PromptResponse(stop_reason="max_turn_requests")
            raise RequestError.internal_error({"reason": "provider_failure"}) from None
        except ConfigurationError as error:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if await session.prompt_should_stop():
                return PromptResponse(stop_reason="cancelled")
            if "unresolved interrupted turn" in str(error):
                raise RequestError.internal_error({"reason": "turn_recovery_required"}) from None
            raise RequestError.internal_error({"reason": "prompt_configuration"}) from None
        except RequestError:
            raise
        except Exception:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if await session.prompt_should_stop():
                return PromptResponse(stop_reason="cancelled")
            raise RequestError.internal_error({"reason": "prompt_failure"}) from None
        finally:
            await session.finish_prompt_if_owner(current_task)

    async def request_permission(
        self,
        session_id: str,
        request: PermissionRequest,
    ) -> PermissionApproval:
        session = await self._registry.active(session_id)
        client = self._connection.client
        try:
            mapper = await session.begin_approval(request.call_id)
        except AcpSessionApprovalAlreadyPendingError:
            return PermissionApproval.deny("another ACP approval is already pending")
        if client is None or mapper is None:
            if mapper is not None:
                await session.finish_approval_if_owner(request.call_id)
            return PermissionApproval.deny("ACP client approval interface is unavailable")
        try:
            options = [
                PermissionOption(
                    option_id="allow_once",
                    name="Allow once",
                    kind="allow_once",
                ),
                PermissionOption(
                    option_id="allow_session",
                    name="Allow identical actions for this session",
                    kind="allow_always",
                ),
                PermissionOption(
                    option_id="deny",
                    name="Deny",
                    kind="reject_once",
                ),
            ]
            response = await client.request_permission(
                session_id,
                mapper.permission_tool_call(request),
                options,
            )
            outcome = response.outcome
            if outcome.outcome != "selected":
                return PermissionApproval.deny("ACP client cancelled approval")
            if outcome.option_id == "allow_once":
                return PermissionApproval.allow_once("approved once by ACP client")
            if outcome.option_id == "allow_session":
                if request.scope_key is None:
                    return PermissionApproval.allow_once(
                        "unscoped action approved once by ACP client"
                    )
                return PermissionApproval.allow_session("approved for this ACP session by client")
            return PermissionApproval.deny("denied by ACP client")
        except asyncio.CancelledError:
            raise
        except Exception:
            return PermissionApproval.deny("ACP client approval failed")
        finally:
            await session.finish_approval_if_owner(request.call_id)

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        session = await self._registry.lookup(session_id)
        if session is None:
            return
        task = await session.request_cancel()
        if task is not None and not task.done():
            task.cancel()


__all__ = ["AcpPromptController"]
