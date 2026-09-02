"""Private ACP extension dispatch and bounded result projections."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any

from acp.exceptions import RequestError

from neuro_code.application.acp.contracts import (
    AcpMcpQuery,
    AcpMcpQueryError,
    AcpReadOnlySubagentQuery,
    AcpReadOnlySubagentQueryError,
    AcpSessionCommandQuery,
    AcpSubagentLifecycleQuery,
    AcpSubagentLifecycleQueryError,
    AcpToolOutputArtifactQuery,
    AcpToolOutputArtifactQueryError,
    AcpTurnRecoveryQuery,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.ports.tools import MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
from neuro_code.application.sessions.subagent_queries import SubagentRelationshipAction
from neuro_code.application.tools.service import SessionToolOutputArtifact
from neuro_code.interfaces.acp.errors import (
    invalid_params as _invalid_params,
)
from neuro_code.interfaces.acp.errors import (
    session_not_found as _session_not_found,
)
from neuro_code.interfaces.acp.errors import (
    validated_session_id as _validated_session_id,
)
from neuro_code.interfaces.acp.mcp import AcpMcpController
from neuro_code.interfaces.acp.negotiation import AcpConnectionState
from neuro_code.interfaces.acp.serialization import (
    safe_output_text,
    serialize_subagent_lifecycle_action,
    serialize_subagent_result,
)
from neuro_code.interfaces.acp.session_registry import AcpSessionRegistry
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError

ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION = "neuro-code/session/artifacts"
ACP_READ_ONLY_SUBAGENT_EXTENSION = "neuro-code/session/subagent"
ACP_SUBAGENT_LIFECYCLE_EXTENSION = "neuro-code/session/subagents"
ACP_MCP_EXTENSION = "neuro-code/session/mcp"
ACP_CONTEXT_COMPACTION_EXTENSION = "neuro-code/session/compact"
ACP_TURN_RECOVERY_EXTENSION = "neuro-code/session/recovery"


def _artifact_list_payload(
    artifacts: Sequence[SessionToolOutputArtifact],
) -> dict[str, list[dict[str, int | str | bool]]]:
    """Serialize only canonical, non-sensitive artifact facts for ACP."""

    payload: list[dict[str, int | str | bool]] = []
    for reference in artifacts:
        artifact = reference.artifact
        if not re.fullmatch(r"[0-9a-f]{32}", artifact.artifact_id):
            continue
        payload.append(
            {
                "artifactId": artifact.artifact_id,
                "byteCount": artifact.byte_count,
                "truncated": artifact.truncated,
                "eventSequence": reference.event_sequence,
            }
        )
    return {"artifacts": payload}


def _artifact_read_payload(
    artifact_id: str,
    content: str,
    read_truncated: bool,
    *,
    explicit_redactions: tuple[str, ...],
) -> dict[str, str | bool]:
    """Serialize one bounded redacted artifact without its storage path."""

    return {
        "artifactId": artifact_id,
        "content": safe_output_text(
            content,
            MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
            explicit_redactions=explicit_redactions,
        ),
        "readTruncated": read_truncated,
    }


class AcpExtensionController:
    """Own private ACP method dispatch while keeping each domain service narrow."""

    __slots__ = ("_connection", "_mcp", "_registry", "_service")

    def __init__(
        self,
        service: AcpApplicationService,
        connection: AcpConnectionState,
        registry: AcpSessionRegistry,
        mcp: AcpMcpController,
    ) -> None:
        self._service = service
        self._connection = connection
        self._registry = registry
        self._mcp = mcp

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve private, bounded session extensions."""

        self._connection.require_initialized()
        if method == ACP_MCP_EXTENSION:
            try:
                mcp_query = AcpMcpQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            return await self._mcp.extension(mcp_query)

        if method == ACP_CONTEXT_COMPACTION_EXTENSION:
            try:
                command_query = AcpSessionCommandQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            session = await self._registry.active(_validated_session_id(command_query.session_id))
            binding = await session.active_binding_snapshot()
            if binding is None:
                raise RequestError.internal_error({"reason": "session_binding_unavailable"})
            try:
                compact_result = await binding.runner.compact_now()
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise RequestError.internal_error({"reason": "provider_failure"}) from None
            except ConfigurationError:
                raise RequestError.internal_error({"reason": "compaction_unavailable"}) from None
            except Exception:
                raise RequestError.internal_error({"reason": "compaction_failed"}) from None
            payload: dict[str, object] = {
                "status": compact_result.status.value,
                "triggered": compact_result.triggered,
            }
            for name in (
                "compaction_id",
                "source_item_count",
                "candidate_item_count",
                "summary_tokens",
                "summary_truncated",
            ):
                value = getattr(compact_result, name)
                if value is not None:
                    payload[name] = value
            if compact_result.outcome is not None:
                payload["outcome"] = {
                    "status": compact_result.outcome.status.value,
                    "reason_code": (
                        compact_result.outcome.reason_code.value
                        if compact_result.outcome.reason_code is not None
                        else None
                    ),
                    "finalized": compact_result.outcome.finalized,
                    "recoverable": compact_result.outcome.recoverable,
                }
            return payload

        if method == ACP_TURN_RECOVERY_EXTENSION:
            try:
                recovery_query = AcpTurnRecoveryQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            session = await self._registry.active(_validated_session_id(recovery_query.session_id))
            binding = await session.active_binding_snapshot()
            if binding is None:
                raise RequestError.internal_error({"reason": "session_binding_unavailable"})
            try:
                if recovery_query.operation == "inspect":
                    inspections = await binding.runner.inspect_recovery()
                    return {
                        "attempts": [inspection.to_dict() for inspection in inspections],
                    }
                assert recovery_query.turn_id is not None
                if recovery_query.operation == "abandon":
                    inspection = await binding.runner.abandon_recovery(
                        recovery_query.turn_id,
                        reason=recovery_query.reason,
                    )
                    return inspection.to_dict()
                result = await binding.runner.retry_recovery(recovery_query.turn_id)
                return {
                    "status": "retried",
                    "sessionId": recovery_query.session_id,
                    "steps": result.steps,
                }
            except ConfigurationError as error:
                message = str(error)
                if "retry" in message and "unavailable" in message:
                    reason = "recovery_retry_unavailable"
                elif "indeterminate" in message or "safely_retryable" not in message:
                    reason = "recovery_not_safe"
                else:
                    reason = "recovery_retry_unavailable"
                raise RequestError.internal_error({"reason": reason}) from None
            except SessionError:
                raise _session_not_found(recovery_query.session_id) from None
            except Exception:
                raise RequestError.internal_error({"reason": "recovery_operation_failed"}) from None

        if method == ACP_SUBAGENT_LIFECYCLE_EXTENSION:
            try:
                lifecycle_query = AcpSubagentLifecycleQuery.from_payload(params)
            except AcpSubagentLifecycleQueryError as error:
                raise _invalid_params(error.reason) from None
            external_session_id = _validated_session_id(lifecycle_query.session_id)
            if not self._service.subagent_lifecycle_available:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_unavailable"})
            internal_session_id = await self._registry.artifact_internal_session_id(
                external_session_id
            )
            try:
                lifecycle_result = await self._service.run_subagent_relationship_action(
                    internal_session_id,
                    lifecycle_query.task_id,
                    lifecycle_query.action,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            except ConfigurationError:
                raise RequestError.internal_error(
                    {"reason": "subagent_relationship_invalid"}
                ) from None
            except Exception:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_failed"}) from None

            if (
                lifecycle_result.parent_session_id != internal_session_id
                or lifecycle_result.parent_task_id != lifecycle_query.task_id
                or lifecycle_result.action is not lifecycle_query.action
            ):
                raise RequestError.internal_error({"reason": "subagent_lifecycle_invalid_result"})
            if lifecycle_result.action is SubagentRelationshipAction.DELETE:
                return serialize_subagent_lifecycle_action(lifecycle_result.action, deleted=True)
            if lifecycle_result.action is SubagentRelationshipAction.RESUME:
                external_child_id = await self._registry.lifecycle_external_session_id(
                    lifecycle_result.child_session_id
                )
                return serialize_subagent_lifecycle_action(
                    lifecycle_result.action,
                    session_id=external_child_id,
                )
            if lifecycle_result.forked_session_id is None:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_invalid_result"})
            external_forked_id = await self._registry.lifecycle_external_session_id(
                lifecycle_result.forked_session_id
            )
            return serialize_subagent_lifecycle_action(
                lifecycle_result.action,
                session_id=external_forked_id,
            )

        if method == ACP_READ_ONLY_SUBAGENT_EXTENSION:
            try:
                subagent_query = AcpReadOnlySubagentQuery.from_payload(params)
            except AcpReadOnlySubagentQueryError as error:
                raise _invalid_params(error.reason) from None
            external_session_id = _validated_session_id(subagent_query.session_id)
            if not self._service.read_only_subagent_available:
                raise RequestError.internal_error({"reason": "subagent_unavailable"})
            # A persisted session summary is not an authorization manifest.
            # The explicit child may run only while its actual ACP parent
            # binding is active and can supply immutable capability metadata.
            parent_session = await self._registry.active(external_session_id)
            parent_binding = await parent_session.active_binding_snapshot()
            if parent_binding is None or parent_binding.capabilities is None:
                raise RequestError.internal_error({"reason": "parent_capability_unavailable"})
            parent_capabilities = parent_binding.capabilities
            internal_session_id = await self._registry.artifact_internal_session_id(
                external_session_id
            )
            try:
                projection = await self._service.run_read_only_subagent(
                    internal_session_id,
                    subagent_query.prompt,
                    parent_capabilities=parent_capabilities,
                    max_steps=subagent_query.max_steps,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            except ProviderError:
                raise RequestError.internal_error({"reason": "provider_failure"}) from None
            except ConfigurationError:
                raise RequestError.internal_error({"reason": "subagent_unavailable"}) from None
            except Exception:
                raise RequestError.internal_error({"reason": "subagent_failed"}) from None
            return serialize_subagent_result(projection)

        if method != ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION:
            raise RequestError.method_not_found(f"_{method}")
        try:
            artifact_query = AcpToolOutputArtifactQuery.from_payload(params)
        except AcpToolOutputArtifactQueryError as error:
            raise _invalid_params(error.reason) from None
        external_session_id = _validated_session_id(artifact_query.session_id)
        if not self._service.tool_output_artifacts_available:
            raise RequestError.internal_error({"reason": "artifact_query_unavailable"})
        internal_session_id = await self._registry.artifact_internal_session_id(external_session_id)
        if artifact_query.artifact_id is None:
            try:
                artifacts = await self._service.list_tool_output_artifacts(
                    internal_session_id,
                    limit=artifact_query.limit,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            return _artifact_list_payload(artifacts)

        try:
            artifact = await self._service.read_tool_output_artifact(
                internal_session_id,
                artifact_query.artifact_id,
                max_bytes=artifact_query.max_bytes,
            )
        except SessionError:
            raise _invalid_params("artifact_not_found") from None
        return _artifact_read_payload(
            artifact.artifact.artifact_id,
            artifact.content,
            artifact.read_truncated,
            explicit_redactions=self._connection.explicit_redactions(),
        )


__all__ = [
    "ACP_CONTEXT_COMPACTION_EXTENSION",
    "ACP_MCP_EXTENSION",
    "ACP_READ_ONLY_SUBAGENT_EXTENSION",
    "ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION",
    "ACP_TURN_RECOVERY_EXTENSION",
    "MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES",
    "AcpExtensionController",
    "_artifact_list_payload",
    "_artifact_read_payload",
]
