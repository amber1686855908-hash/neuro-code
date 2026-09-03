"""Canonical ACP transport and framing boundary.

ACP 传输和帧处理的规范边界.

This module owns only the ACP SDK connection, stdio framing, and WebSocket
framing/lifecycle adapters.  The injected agent facade and its focused
controllers own ACP protocol semantics, capabilities, session state, and
application orchestration.
本模块只负责 ACP SDK connection、stdio framing 以及 WebSocket framing/lifecycle
适配. 注入的 Agent facade 及其专用控制器负责 ACP 协议语义、能力、会话状态和
application 编排.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from acp.agent.router import build_agent_router
from acp.core import Connection
from acp.interfaces import Agent, Client
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.router import MessageRouter
from acp.schema import (
    DeleteSessionRequest,
    PermissionOption,
    RequestPermissionRequest,
    RequestPermissionResponse,
    SessionNotification,
    ToolCallUpdate,
)
from acp.stdio import stdio_streams
from acp.utils import normalize_result, notify_model, request_model

from neuro_code.shared.errors import ConfigurationError

ACP_STDIO_BUFFER_LIMIT_BYTES = 1024 * 1024


class _TransportAgent(Protocol):
    def on_connect(self, conn: Client) -> None: ...

    async def shutdown(self) -> None: ...


_ConnectionFactory = Callable[
    [_TransportAgent, asyncio.StreamWriter, asyncio.StreamReader], "_AcpSdkConnection"
]
_StdioStreamsFactory = Callable[
    ...,
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]
_WebSocketWriterFactory = Callable[[Any], Any]


def _build_acp_router(agent: Agent) -> MessageRouter:
    """Extend the SDK 0.11 router with its generated stable delete route.

    使用生成的稳定删除路由扩展 SDK 0.11 路由器.
    """

    router = build_agent_router(agent, use_unstable_protocol=True)
    router.route_request(
        AGENT_METHODS["session_delete"],
        DeleteSessionRequest,
        agent,
        "delete_session",
        adapt_result=normalize_result,
    )
    return router


class _AcpSdkConnection:
    """Small SDK connection adapter until its agent router registers delete.

    在 Agent 路由器注册删除操作前使用的小型 SDK 连接适配器.
    """

    def __init__(
        self,
        agent: _TransportAgent,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
    ) -> None:
        self._connection = Connection(
            _build_acp_router(cast(Agent, agent)),
            writer,
            reader,
            listening=False,
        )
        agent.on_connect(cast(Client, self))

    async def listen(self) -> None:
        await self._connection.main_loop()

    async def close(self) -> None:
        await self._connection.close()

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        await notify_model(
            self._connection,
            CLIENT_METHODS["session_update"],
            SessionNotification(
                session_id=session_id,
                update=update,
                field_meta=kwargs or None,
            ),
        )

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        return await request_model(
            self._connection,
            CLIENT_METHODS["session_request_permission"],
            RequestPermissionRequest(
                session_id=session_id,
                tool_call=tool_call,
                options=options,
                field_meta=kwargs or None,
            ),
            RequestPermissionResponse,
        )


class _WebSocketWriter:
    """Minimal asyncio writer bridge for ACP's newline JSON sender."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._pending = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket ACP writer is closed")
        self._pending.extend(data)

    async def drain(self) -> None:
        if self._closed or not self._pending:
            return
        payload = bytes(self._pending)
        self._pending.clear()
        await self._websocket.send(payload)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default


async def serve_websocket(
    agent_factory: Callable[[], _TransportAgent],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    connection_factory: _ConnectionFactory = _AcpSdkConnection,
    writer_factory: _WebSocketWriterFactory = _WebSocketWriter,
) -> None:
    """Serve one injected ACP Agent per WebSocket connection.

    每个 WebSocket connection 使用一个由调用方注入的 ACP Agent.

    The optional factories are narrow compatibility seams for the historical
    private ACP tests.  They do not change ownership or provide a second
    transport implementation.
    """

    if not isinstance(host, str) or not host or "\x00" in host or len(host.encode("utf-8")) > 256:
        raise ConfigurationError("WebSocket ACP host is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ConfigurationError("WebSocket ACP port is invalid")
    try:
        from websockets.asyncio.server import serve
    except ImportError:
        raise ConfigurationError(
            "WebSocket ACP support requires the websockets dependency"
        ) from None

    async def handle(websocket: Any) -> None:
        agent = agent_factory()
        reader = asyncio.StreamReader(limit=ACP_STDIO_BUFFER_LIMIT_BYTES)
        writer = writer_factory(websocket)
        connection = connection_factory(agent, cast(asyncio.StreamWriter, writer), reader)
        feeder: asyncio.Task[None] | None = None

        async def feed_messages() -> None:
            try:
                async for message in websocket:
                    if isinstance(message, str):
                        data = message.encode("utf-8")
                    elif isinstance(message, bytes):
                        data = message
                    else:
                        raise ConnectionError("WebSocket ACP message type is unsupported")
                    if not data or len(data) > ACP_STDIO_BUFFER_LIMIT_BYTES:
                        raise ConnectionError("WebSocket ACP message exceeds the size limit")
                    if not data.endswith(b"\n"):
                        data += b"\n"
                    reader.feed_data(data)
            finally:
                reader.feed_eof()

        try:
            feeder = asyncio.create_task(feed_messages(), name="neuro-code-acp-websocket-reader")
            await connection.listen()
        finally:
            if feeder is not None and not feeder.done():
                feeder.cancel()
            if feeder is not None:
                await asyncio.gather(feeder, return_exceptions=True)
            await asyncio.shield(connection.close())
            await asyncio.shield(agent.shutdown())

    async with serve(
        handle,
        host,
        port,
        max_size=ACP_STDIO_BUFFER_LIMIT_BYTES,
        max_queue=16,
    ):
        await asyncio.Future()


async def serve_stdio(
    agent: _TransportAgent,
    *,
    streams_factory: _StdioStreamsFactory = stdio_streams,
    connection_factory: _ConnectionFactory = _AcpSdkConnection,
) -> None:
    """Serve an injected ACP Agent on stdio through the official SDK framing."""

    connection: _AcpSdkConnection | None = None
    try:
        reader, writer = await streams_factory(limit=ACP_STDIO_BUFFER_LIMIT_BYTES)
        connection = connection_factory(agent, writer, reader)
        await connection.listen()
    finally:
        if connection is not None:
            await asyncio.shield(connection.close())
        await asyncio.shield(agent.shutdown())


__all__ = [
    "ACP_STDIO_BUFFER_LIMIT_BYTES",
    "serve_stdio",
    "serve_websocket",
    "stdio_streams",
]
