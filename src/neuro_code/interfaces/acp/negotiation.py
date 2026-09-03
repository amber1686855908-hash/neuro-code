"""ACP connection negotiation and client capability adapters.

ACP 连接协商以及客户端能力适配.

The connection state is deliberately separate from the session registry.  It
owns only the one ACP client's negotiated state and the small adapters that
turn negotiated client capabilities into application ports.
"""

from __future__ import annotations

import asyncio
from typing import Any

from acp.exceptions import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    ClientCapabilities,
    Implementation,
    InitializeResponse,
    McpCapabilities,
    PromptCapabilities,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionDeleteCapabilities,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionResumeCapabilities,
)

from neuro_code import __version__
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.interfaces.acp.client_io import _AcpClientFileSystem, _AcpClientTerminal

ACP_PROTOCOL_VERSION = 1


class AcpConnectionState:
    """Own one connection's client identity, capabilities, and initialization."""

    __slots__ = (
        "_client",
        "_client_capabilities",
        "_client_info",
        "_initialize_lock",
        "_initialized",
        "_service",
    )

    def __init__(self, service: AcpApplicationService) -> None:
        self._service = service
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None
        self._client_info: Implementation | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    @property
    def client(self) -> Client | None:
        return self._client

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        return self._client_capabilities

    @property
    def client_info(self) -> Implementation | None:
        return self._client_info

    def on_connect(self, conn: Client) -> None:
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **_kwargs: Any,
    ) -> InitializeResponse:
        async with self._initialize_lock:
            if self._initialized:
                raise RequestError.invalid_request({"reason": "already_initialized"})
            self._client_capabilities = client_capabilities
            self._client_info = client_info
            self._initialized = True
        negotiated = (
            protocol_version if protocol_version == ACP_PROTOCOL_VERSION else ACP_PROTOCOL_VERSION
        )
        return InitializeResponse(
            protocol_version=negotiated,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    audio=True,
                    embedded_context=True,
                ),
                mcp_capabilities=McpCapabilities(http=True, sse=True),
                auth=None,
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    delete=SessionDeleteCapabilities(),
                    fork=SessionForkCapabilities(),
                    resume=SessionResumeCapabilities(),
                    close=SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name="neuro-code",
                title="Neuro Code",
                version=__version__,
            ),
        )

    def require_initialized(self) -> None:
        if not self._initialized:
            raise RequestError.invalid_request({"reason": "not_initialized"})

    def client_file_system(self, session_id: str) -> ClientFileSystem | None:
        client = self._client
        capabilities = self._client_capabilities
        filesystem = capabilities.fs if capabilities is not None else None
        if client is None or filesystem is None:
            return None
        supports_read = filesystem.read_text_file is True
        supports_write = filesystem.write_text_file is True
        if not supports_read and not supports_write:
            return None
        return _AcpClientFileSystem(
            client,
            session_id,
            supports_read=supports_read,
            supports_write=supports_write,
        )

    def client_terminal(self, session_id: str) -> ClientTerminal | None:
        client = self._client
        capabilities = self._client_capabilities
        if client is None or capabilities is None or capabilities.terminal is not True:
            return None
        return _AcpClientTerminal(client, session_id)

    def explicit_redactions(self) -> tuple[str, ...]:
        return self._service.explicit_redactions()


__all__ = ["ACP_PROTOCOL_VERSION", "AcpConnectionState"]
