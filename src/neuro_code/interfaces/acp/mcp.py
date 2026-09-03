"""Live MCP ownership for an ACP connection.

ACP 连接的实时 MCP 所有权.

Configuration conversion remains in :mod:`mcp_config`.  This module owns the
connection callbacks, session-owned MCP handles, and bounded projections of
MCP metadata/resources exposed through the private ACP extension.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

from acp.exceptions import RequestError

from neuro_code.application.acp.contracts import (
    AcpMcpQuery,
    AcpMcpServerConfig,
    AcpMcpTools,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.ports.mcp import McpElicitationHandler, McpSamplingHandler
from neuro_code.interfaces.acp.errors import validated_session_id as _validated_session_id
from neuro_code.interfaces.acp.mcp_config import (
    MAX_MCP_CONFIGURATION_BYTES,
    MAX_MCP_URL_BYTES,
    McpServer,
    _mcp_server_configurations,
)
from neuro_code.interfaces.acp.negotiation import AcpConnectionState
from neuro_code.interfaces.acp.serialization import (
    safe_output_text,
    serialized_size_bytes,
)
from neuro_code.interfaces.acp.session_registry import AcpSessionRegistry
from neuro_code.shared.errors import ConfigurationError

MAX_MCP_SAMPLING_MESSAGES = 128
MAX_MCP_SAMPLING_TOKENS = 1_000_000
MAX_MCP_ELICITATION_MESSAGE_BYTES = 64 * 1024
MAX_MCP_CALLBACK_BYTES = 256 * 1024
MAX_MCP_RESOURCE_BYTES = 512 * 1024


def _safe_mcp_extension_value(
    value: object,
    *,
    explicit_redactions: tuple[str, ...],
    depth: int = 0,
) -> object:
    """Project untrusted MCP metadata into bounded, redacted JSON values."""

    if depth >= 5:
        return "<nested-value-omitted>"
    if isinstance(value, str):
        return safe_output_text(value, 16 * 1024, explicit_redactions=explicit_redactions)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= 64:
                result["<fields-omitted>"] = True
                break
            rendered_key = safe_output_text(
                str(key),
                512,
                explicit_redactions=explicit_redactions,
            )
            result[rendered_key] = _safe_mcp_extension_value(
                nested,
                explicit_redactions=explicit_redactions,
                depth=depth + 1,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [
            _safe_mcp_extension_value(
                nested,
                explicit_redactions=explicit_redactions,
                depth=depth + 1,
            )
            for nested in value[:64]
        ]
    return safe_output_text(str(value), 512, explicit_redactions=explicit_redactions)


class AcpMcpController:
    """Own live MCP callbacks and session-scoped MCP projections."""

    __slots__ = ("_connection", "_registry", "_service")

    def __init__(
        self,
        service: AcpApplicationService,
        connection: AcpConnectionState,
        registry: AcpSessionRegistry,
    ) -> None:
        self._service = service
        self._connection = connection
        self._registry = registry

    def safe_callback_payload(self, value: object) -> dict[str, Any]:
        projected = _safe_mcp_extension_value(
            value,
            explicit_redactions=self._connection.explicit_redactions(),
        )
        if not isinstance(projected, dict):
            raise ConfigurationError("MCP callback payload is not an object")
        if serialized_size_bytes(projected) > MAX_MCP_CALLBACK_BYTES:
            raise ConfigurationError("MCP callback payload is too large")
        return cast(dict[str, Any], projected)

    async def sampling_handler(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_preferences: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> Mapping[str, Any]:
        client = self._connection.client
        if client is None:
            raise ConfigurationError("ACP client is unavailable for MCP sampling")
        if len(messages) > MAX_MCP_SAMPLING_MESSAGES:
            raise ConfigurationError("MCP sampling message count exceeds the limit")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ConfigurationError("MCP sampling system prompt is invalid")
        if (
            system_prompt is not None
            and len(system_prompt.encode("utf-8")) > MAX_MCP_ELICITATION_MESSAGE_BYTES
        ):
            raise ConfigurationError("MCP sampling system prompt is too large")
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= MAX_MCP_SAMPLING_TOKENS
        ):
            raise ConfigurationError("MCP sampling token limit is invalid")
        payload: dict[str, object] = {"messages": tuple(messages)}
        if model_preferences is not None:
            payload["modelPreferences"] = model_preferences
        if system_prompt is not None:
            payload["systemPrompt"] = system_prompt
        if max_tokens is not None:
            payload["maxTokens"] = max_tokens
        response = await client.ext_method(
            "neuro-code/mcp/sampling",
            self.safe_callback_payload(payload),
        )
        return self.safe_callback_payload(response)

    async def elicitation_handler(
        self,
        message: str,
        schema: Mapping[str, Any] | None = None,
        *,
        url: str | None = None,
    ) -> Mapping[str, Any]:
        client = self._connection.client
        if client is None:
            raise ConfigurationError("ACP client is unavailable for MCP elicitation")
        if (
            not isinstance(message, str)
            or len(message.encode("utf-8")) > MAX_MCP_ELICITATION_MESSAGE_BYTES
        ):
            raise ConfigurationError("MCP elicitation message is invalid")
        if url is not None and (
            not isinstance(url, str) or len(url.encode("utf-8")) > MAX_MCP_URL_BYTES
        ):
            raise ConfigurationError("MCP elicitation URL is invalid")
        payload: dict[str, object] = {"message": message}
        if schema is not None:
            payload["schema"] = schema
        if url is not None:
            payload["url"] = url
        response = await client.ext_method(
            "neuro-code/mcp/elicitation",
            self.safe_callback_payload(payload),
        )
        return self.safe_callback_payload(response)

    def server_configurations(
        self,
        servers: list[McpServer] | None,
    ) -> tuple[AcpMcpServerConfig, ...]:
        return _mcp_server_configurations(
            servers,
            protected_environment_variables=self._service.protected_environment_variables,
        )

    async def open_tools(
        self,
        configurations: tuple[AcpMcpServerConfig, ...],
    ) -> AcpMcpTools | None:
        if not configurations:
            return None
        sampling_handler: McpSamplingHandler | None = (
            self.sampling_handler if self._connection.client is not None else None
        )
        elicitation_handler: McpElicitationHandler | None = (
            self.elicitation_handler if self._connection.client is not None else None
        )
        return await self._service.open_mcp_tools(
            configurations,
            sampling_handler=sampling_handler,
            elicitation_handler=elicitation_handler,
        )

    def list_payload(self, mcp_tools: AcpMcpTools) -> dict[str, object]:
        explicit_redactions = self._connection.explicit_redactions()
        payload = {
            "resources": [
                _safe_mcp_extension_value(
                    resource.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for resource in tuple(mcp_tools.resources)[:256]
            ],
            "resourceTemplates": [
                _safe_mcp_extension_value(
                    template.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for template in tuple(mcp_tools.resource_templates)[:256]
            ],
            "prompts": [
                _safe_mcp_extension_value(
                    prompt.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for prompt in tuple(mcp_tools.prompts)[:128]
            ],
            "toolCount": len(tuple(mcp_tools.tools)),
        }
        if serialized_size_bytes(payload) > MAX_MCP_CONFIGURATION_BYTES:
            raise RequestError.internal_error({"reason": "mcp_metadata_too_large"})
        return payload

    async def extension(self, query: AcpMcpQuery) -> dict[str, object]:
        external_session_id = _validated_session_id(query.session_id)
        session = await self._registry.active(external_session_id)
        mcp_snapshot = await session.mcp_snapshot()
        if mcp_snapshot is None:
            raise RequestError.internal_error({"reason": "mcp_unavailable"})
        mcp_tools, mcp_tool_names = mcp_snapshot
        if query.operation == "list":
            return self.list_payload(mcp_tools)
        try:
            if query.operation == "refresh":
                await mcp_tools.refresh()
                binding = await session.active_binding_snapshot()
                if binding is None:
                    raise ConfigurationError("MCP session binding is unavailable")
                binding.runner.replace_external_tools(
                    mcp_tools.tools,
                    mcp_tool_names,
                )
                await session.update_mcp_tool_names(
                    mcp_tools,
                    tuple(tool.definition.name for tool in mcp_tools.tools),
                )
                payload = self.list_payload(mcp_tools)
                payload["refreshed"] = True
                return payload
            if query.operation == "read_resource":
                assert query.uri is not None
                contents = await mcp_tools.read_resource(query.uri)
                explicit_redactions = self._connection.explicit_redactions()
                projected: list[dict[str, object]] = []
                for content in tuple(contents)[:32]:
                    raw = content.to_dict()
                    if "text" in raw:
                        raw["text"] = safe_output_text(
                            raw["text"],
                            MAX_MCP_RESOURCE_BYTES,
                            explicit_redactions=explicit_redactions,
                        )
                    if "blob" in raw:
                        raw["blob"] = safe_output_text(
                            raw["blob"],
                            MAX_MCP_RESOURCE_BYTES,
                            explicit_redactions=explicit_redactions,
                        )
                    projected.append(
                        cast(
                            dict[str, object],
                            _safe_mcp_extension_value(
                                raw,
                                explicit_redactions=explicit_redactions,
                            ),
                        )
                    )
                payload = {"contents": projected}
                if serialized_size_bytes(payload) > MAX_MCP_RESOURCE_BYTES:
                    raise RequestError.internal_error({"reason": "mcp_resource_too_large"})
                return payload
            if query.operation == "get_prompt":
                assert query.name is not None
                messages = await mcp_tools.get_prompt(query.name, dict(query.arguments))
                explicit_redactions = self._connection.explicit_redactions()
                projected_messages = [
                    cast(
                        dict[str, object],
                        _safe_mcp_extension_value(
                            message.to_dict(),
                            explicit_redactions=explicit_redactions,
                        ),
                    )
                    for message in tuple(messages)[:128]
                ]
                payload = {"messages": projected_messages}
                if serialized_size_bytes(payload) > MAX_MCP_CONFIGURATION_BYTES:
                    raise RequestError.internal_error({"reason": "mcp_prompt_too_large"})
                return payload
        except asyncio.CancelledError:
            raise
        except RequestError:
            raise
        except Exception:
            raise RequestError.internal_error({"reason": "mcp_operation_failed"}) from None
        raise RequestError.internal_error({"reason": "mcp_operation_unsupported"})


__all__ = [
    "MAX_MCP_CALLBACK_BYTES",
    "MAX_MCP_ELICITATION_MESSAGE_BYTES",
    "MAX_MCP_SAMPLING_MESSAGES",
    "MAX_MCP_SAMPLING_TOKENS",
    "AcpMcpController",
    "_safe_mcp_extension_value",
]
