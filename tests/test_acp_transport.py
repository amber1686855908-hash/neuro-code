from __future__ import annotations

import asyncio
import builtins
import sys
import unittest
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import neuro_code.interfaces.acp.transport as transport
from neuro_code.shared.errors import ConfigurationError


class _FakeAgent:
    def __init__(self) -> None:
        self.connections: list[Any] = []
        self.shutdown_calls = 0

    def on_connect(self, connection: Any) -> None:
        self.connections.append(connection)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeReader:
    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self.data: list[bytes] = []
        self.eof = False
        self.eof_event = asyncio.Event()

    def feed_data(self, data: bytes) -> None:
        self.data.append(data)

    def feed_eof(self) -> None:
        self.eof = True
        self.eof_event.set()


class _FakeConnection:
    def __init__(
        self,
        reader: _FakeReader,
        *,
        wait_for_eof: bool = True,
        listen_ready: asyncio.Event | None = None,
    ) -> None:
        self.reader = reader
        self.wait_for_eof = wait_for_eof
        self.listen_ready = listen_ready
        self.listen_calls = 0
        self.close_calls = 0

    async def listen(self) -> None:
        self.listen_calls += 1
        if self.listen_ready is not None:
            await self.listen_ready.wait()
        elif self.wait_for_eof:
            await self.reader.eof_event.wait()

    async def close(self) -> None:
        self.close_calls += 1


class _FakeWebSocket:
    def __init__(self, messages: list[object] | None = None, *, block: bool = False) -> None:
        self.messages = messages or []
        self.index = 0
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.sent: list[Any] = []

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> object:
        if self.index < len(self.messages):
            message = self.messages[self.index]
            self.index += 1
            return message
        if not self.block:
            raise StopAsyncIteration
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise StopAsyncIteration

    async def send(self, value: Any) -> None:
        self.sent.append(value)


class _StopServer(Exception):
    pass


class _ServerContext:
    def __init__(self, handler: Any, websockets: list[_FakeWebSocket]) -> None:
        self.handler = handler
        self.websockets = websockets

    async def __aenter__(self) -> _ServerContext:
        for websocket in self.websockets:
            await self.handler(websocket)
        raise _StopServer

    async def __aexit__(self, *_args: Any) -> bool:
        return False


def _fake_websockets_module(
    websockets: list[_FakeWebSocket], calls: list[tuple[Any, str, int, dict[str, Any]]]
) -> ModuleType:
    fake_server = ModuleType("websockets.asyncio.server")

    def serve(handler: Any, host: str, port: int, **kwargs: Any) -> _ServerContext:
        calls.append((handler, host, port, kwargs))
        return _ServerContext(handler, websockets)

    fake_server.__dict__["serve"] = serve
    return fake_server


class AcpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_uses_canonical_bound_and_closes_connection_and_agent_once(self) -> None:
        agent = _FakeAgent()
        reader = object()
        writer = object()
        streams = AsyncMock(return_value=(reader, writer))
        connection = _FakeConnection(
            _FakeReader(limit=transport.ACP_STDIO_BUFFER_LIMIT_BYTES),
            wait_for_eof=False,
        )
        created: list[tuple[Any, Any, Any]] = []

        def create_connection(candidate: Any, candidate_writer: Any, candidate_reader: Any) -> Any:
            created.append((candidate, candidate_writer, candidate_reader))
            return connection

        await transport.serve_stdio(
            agent,
            streams_factory=streams,
            connection_factory=create_connection,
        )

        streams.assert_awaited_once_with(limit=transport.ACP_STDIO_BUFFER_LIMIT_BYTES)
        self.assertEqual(created, [(agent, writer, reader)])
        self.assertEqual(connection.listen_calls, 1)
        self.assertEqual(connection.close_calls, 1)
        self.assertEqual(agent.shutdown_calls, 1)

    async def test_stdio_stream_setup_failure_still_shuts_down_injected_agent(self) -> None:
        agent = _FakeAgent()

        async def fail_streams(**_kwargs: Any) -> tuple[Any, Any]:
            raise RuntimeError("stream setup failed")

        with self.assertRaisesRegex(RuntimeError, "stream setup failed"):
            await transport.serve_stdio(agent, streams_factory=fail_streams)

        self.assertEqual(agent.shutdown_calls, 1)

    async def test_websocket_text_binary_utf8_newline_bound_and_per_connection_agents(self) -> None:
        agents: list[_FakeAgent] = []
        connections: list[_FakeConnection] = []
        readers: list[_FakeReader] = []
        calls: list[tuple[Any, str, int, dict[str, Any]]] = []
        websockets = [
            _FakeWebSocket(["你好", b'{"jsonrpc":"2.0"}\n']),
            _FakeWebSocket([b"second"]),
        ]

        def create_agent() -> _FakeAgent:
            agent = _FakeAgent()
            agents.append(agent)
            return agent

        def create_reader(*, limit: int) -> _FakeReader:
            reader = _FakeReader(limit=limit)
            readers.append(reader)
            return reader

        def create_connection(agent: Any, _writer: Any, reader: _FakeReader) -> _FakeConnection:
            self.assertIs(agent, agents[len(connections)])
            connection = _FakeConnection(reader)
            connections.append(connection)
            return connection

        fake_server = _fake_websockets_module(websockets, calls)
        with (
            patch.dict(sys.modules, {"websockets.asyncio.server": fake_server}),
            patch.object(transport.asyncio, "StreamReader", create_reader),
            self.assertRaises(_StopServer),
        ):
            await transport.serve_websocket(
                create_agent,
                host="127.0.0.1",
                port=8765,
                connection_factory=create_connection,
                writer_factory=lambda websocket: ("writer", websocket),
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1:],
            (
                "127.0.0.1",
                8765,
                {
                    "max_size": transport.ACP_STDIO_BUFFER_LIMIT_BYTES,
                    "max_queue": 16,
                },
            ),
        )
        self.assertEqual(
            readers[0].data,
            ["你好".encode() + b"\n", b'{"jsonrpc":"2.0"}\n'],
        )
        self.assertEqual(readers[1].data, [b"second\n"])
        self.assertTrue(
            all(reader.limit == transport.ACP_STDIO_BUFFER_LIMIT_BYTES for reader in readers)
        )
        self.assertTrue(all(reader.eof for reader in readers))
        self.assertEqual([connection.close_calls for connection in connections], [1, 1])
        self.assertEqual([agent.shutdown_calls for agent in agents], [1, 1])
        self.assertIsNot(agents[0], agents[1])
        self.assertFalse(
            any(
                task.get_name() == "neuro-code-acp-websocket-reader" for task in asyncio.all_tasks()
            )
        )

    async def test_websocket_unsupported_and_oversized_messages_close_without_leaking_feeder(
        self,
    ) -> None:
        for message in (object(), b"x" * (transport.ACP_STDIO_BUFFER_LIMIT_BYTES + 1)):
            with self.subTest(message_type=type(message).__name__):
                agent = _FakeAgent()
                websocket = _FakeWebSocket([message])
                reader: _FakeReader | None = None
                connection: _FakeConnection | None = None

                def create_reader(*, limit: int) -> _FakeReader:
                    nonlocal reader
                    reader = _FakeReader(limit=limit)
                    return reader

                def create_connection(
                    _agent: Any, _writer: Any, candidate: _FakeReader
                ) -> _FakeConnection:
                    nonlocal connection
                    connection = _FakeConnection(candidate)
                    return connection

                calls: list[tuple[Any, str, int, dict[str, Any]]] = []
                fake_server = _fake_websockets_module([websocket], calls)
                with (
                    patch.dict(sys.modules, {"websockets.asyncio.server": fake_server}),
                    patch.object(transport.asyncio, "StreamReader", create_reader),
                    self.assertRaises(_StopServer),
                ):
                    await transport.serve_websocket(
                        lambda candidate=agent: candidate,
                        connection_factory=create_connection,
                        writer_factory=lambda _websocket: object(),
                    )

                assert reader is not None
                assert connection is not None
                self.assertEqual(reader.data, [])
                self.assertTrue(reader.eof)
                self.assertEqual(connection.close_calls, 1)
                self.assertEqual(agent.shutdown_calls, 1)
                self.assertFalse(
                    any(
                        task.get_name() == "neuro-code-acp-websocket-reader"
                        for task in asyncio.all_tasks()
                    )
                )

    async def test_websocket_feeder_cancellation_is_joined(self) -> None:
        agent = _FakeAgent()
        websocket = _FakeWebSocket(block=True)
        reader: _FakeReader | None = None
        connection: _FakeConnection | None = None

        def create_reader(*, limit: int) -> _FakeReader:
            nonlocal reader
            reader = _FakeReader(limit=limit)
            return reader

        def create_connection(_agent: Any, _writer: Any, candidate: _FakeReader) -> _FakeConnection:
            nonlocal connection
            connection = _FakeConnection(
                candidate,
                wait_for_eof=False,
                listen_ready=websocket.started,
            )
            return connection

        calls: list[tuple[Any, str, int, dict[str, Any]]] = []
        fake_server = _fake_websockets_module([websocket], calls)
        with (
            patch.dict(sys.modules, {"websockets.asyncio.server": fake_server}),
            patch.object(transport.asyncio, "StreamReader", create_reader),
            self.assertRaises(_StopServer),
        ):
            await transport.serve_websocket(
                lambda: agent,
                connection_factory=create_connection,
                writer_factory=lambda _websocket: object(),
            )

        assert reader is not None
        assert connection is not None
        self.assertTrue(websocket.started.is_set())
        self.assertTrue(websocket.cancelled)
        self.assertTrue(reader.eof)
        self.assertEqual(connection.close_calls, 1)
        self.assertEqual(agent.shutdown_calls, 1)
        self.assertFalse(
            any(
                task.get_name() == "neuro-code-acp-websocket-reader" for task in asyncio.all_tasks()
            )
        )

    async def test_missing_websockets_dependency_remains_fail_closed(self) -> None:
        agent_factory = Mock()
        original_import = builtins.__import__

        def reject_websockets(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "websockets.asyncio.server":
                raise ImportError("fixture dependency missing")
            return original_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=reject_websockets),
            self.assertRaisesRegex(ConfigurationError, "requires the websockets dependency"),
        ):
            await transport.serve_websocket(agent_factory)

        agent_factory.assert_not_called()

    async def test_canonical_symbols_are_identity_stable_for_legacy_imports(self) -> None:
        import neuro_code.acp as legacy

        self.assertIs(legacy._build_acp_router, transport._build_acp_router)
        self.assertIs(legacy._AcpSdkConnection, transport._AcpSdkConnection)
        self.assertIs(legacy._WebSocketWriter, transport._WebSocketWriter)
        self.assertIs(legacy.stdio_streams, transport.stdio_streams)
        self.assertIs(legacy.ACP_STDIO_BUFFER_LIMIT_BYTES, transport.ACP_STDIO_BUFFER_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
