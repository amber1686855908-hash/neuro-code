from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from typing import Literal, cast
from unittest.mock import patch

import httpx

import neuro_code.adapters.mcp_http as mcp_http
from neuro_code.adapters.mcp_http import McpHttpError, McpHttpServerConfig, McpHttpToolCollection
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.shared.errors import ToolError


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False

    async def send(self, frame: bytes) -> None:
        await self._frames.put(frame)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._frames.put(None)


class _RemoteMcpTransportFixture:
    def __init__(self, transport: str) -> None:
        self.transport = transport
        self.delete_requests = 0
        self.request_headers: list[dict[str, str]] = []
        self.block_tool_calls = False
        self.tool_call_started = asyncio.Event()
        self._sse_stream = _QueueStream()

    def client(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        del auth
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            transport=httpx.MockTransport(self.handle),
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.request_headers.append(dict(request.headers))
        if request.method == "DELETE":
            self.delete_requests += 1
            return httpx.Response(204, request=request)
        if request.method == "GET":
            if self.transport == "sse":
                await self._sse_stream.send(
                    b"event: endpoint\ndata: /messages?sessionId=fixture-session\n\n"
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=self._sse_stream,
                    request=request,
                )
            return httpx.Response(405, request=request)
        if request.method != "POST":
            return httpx.Response(405, request=request)

        payload = json.loads(request.content)
        response = await self._response(payload)
        if response is None:
            return httpx.Response(202, request=request)
        if self.transport == "http":
            return httpx.Response(
                200,
                json=response,
                headers={"mcp-session-id": "fixture-session"},
                request=request,
            )
        frame = json.dumps(response, separators=(",", ":"))
        await self._sse_stream.send(f"event: message\ndata: {frame}\n\n".encode())
        return httpx.Response(202, request=request)

    async def _response(self, request: dict[str, object]) -> dict[str, object] | None:
        request_id = request.get("id")
        if request_id is None:
            return None
        method = request["method"]
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture-http-mcp", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return supplied test text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            }
        elif method == "tools/call":
            if self.block_tool_calls:
                self.tool_call_started.set()
                await asyncio.Event().wait()
            result = {
                "content": [{"type": "text", "text": "Bearer fixture-token-value"}],
                "isError": False,
            }
        else:
            result = {"tools": []}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


class McpHttpToolCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamable_http_and_sse_use_official_sdk_and_redact_headers(self) -> None:
        for transport in ("http", "sse"):
            with self.subTest(transport=transport):
                fixture = _RemoteMcpTransportFixture(transport)
                with patch("neuro_code.adapters.mcp_http._mcp_http_client", fixture.client):
                    collection = await McpHttpToolCollection.open(
                        (
                            McpHttpServerConfig(
                                name=transport,
                                url="https://mcp.fixture.test/mcp",
                                headers=(("Authorization", "Bearer fixture-token-value"),),
                                transport=cast(Literal["http", "sse"], transport),
                            ),
                        )
                    )
                    self.addAsyncCleanup(collection.close)
                    result = await collection.tools[0].execute(
                        {"text": "hello"},
                        ToolContext(__file__, sandbox_profile=SandboxProfile.OFF),
                    )
                    await collection.close()

                self.assertFalse(result.is_error)
                self.assertIn("[REDACTED]", result.content)
                self.assertNotIn("fixture-token-value", result.content)
                self.assertTrue(
                    any(
                        headers.get("authorization") == "Bearer fixture-token-value"
                        for headers in fixture.request_headers
                    )
                )
                if transport == "http":
                    self.assertEqual(fixture.delete_requests, 1)

    async def test_cancelling_remote_call_closes_connection_and_fails_closed(self) -> None:
        fixture = _RemoteMcpTransportFixture("http")
        fixture.block_tool_calls = True
        with patch("neuro_code.adapters.mcp_http._mcp_http_client", fixture.client):
            collection = await McpHttpToolCollection.open(
                (
                    McpHttpServerConfig(
                        name="http",
                        url="https://mcp.fixture.test/mcp",
                    ),
                )
            )
            self.addAsyncCleanup(collection.close)
            tool = collection.tools[0]
            call = asyncio.create_task(
                tool.execute(
                    {"text": "wait"},
                    ToolContext(__file__, sandbox_profile=SandboxProfile.OFF),
                )
            )
            await fixture.tool_call_started.wait()
            call.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await call
            with self.assertRaisesRegex(ToolError, "not_active"):
                await tool.execute(
                    {"text": "must not run"},
                    ToolContext(__file__, sandbox_profile=SandboxProfile.OFF),
                )

    async def test_http_response_limits_fail_before_unbounded_consumption(self) -> None:
        stream = _QueueStream()
        await stream.send(b"x" * (mcp_http.MAX_MCP_HTTP_RESPONSE_BYTES + 1))
        bounded = mcp_http._BoundedMcpHttpResponseStream(stream)

        with self.assertRaisesRegex(McpHttpError, "response_too_large"):
            async for _chunk in bounded:
                self.fail("oversized response must not yield a chunk")

        transport = mcp_http._BoundedMcpHttpTransport()
        transport._transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-length": str(mcp_http.MAX_MCP_HTTP_RESPONSE_BYTES + 1)},
                request=request,
            )
        )
        with self.assertRaisesRegex(McpHttpError, "response_too_large"):
            await transport.handle_async_request(httpx.Request("GET", "https://mcp.fixture.test"))
        await transport.aclose()


if __name__ == "__main__":
    unittest.main()
