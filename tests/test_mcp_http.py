from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, patch

import httpx

import neuro_code.infrastructure.mcp.http as mcp_http
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.infrastructure.mcp.http import (
    McpHttpError,
    McpHttpServerConfig,
    McpHttpToolCollection,
)
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
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
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
        elif method == "resources/list":
            result = {
                "resources": [
                    {
                        "uri": "fixture://http-resource",
                        "name": "http-resource",
                        "mimeType": "text/plain",
                    }
                ]
            }
        elif method == "resources/templates/list":
            result = {
                "resourceTemplates": [
                    {
                        "uriTemplate": "fixture://http-resource/{name}",
                        "name": "http-template",
                    }
                ]
            }
        elif method == "prompts/list":
            result = {
                "prompts": [
                    {
                        "name": "http-prompt",
                        "arguments": [{"name": "topic", "required": True}],
                    }
                ]
            }
        elif method == "resources/read":
            result = {
                "contents": [
                    {
                        "uri": "fixture://http-resource",
                        "mimeType": "text/plain",
                        "text": "http resource text",
                    }
                ]
            }
        elif method == "prompts/get":
            result = {
                "description": "HTTP prompt",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": "http prompt text"},
                    }
                ],
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
    async def test_collection_open_rejects_server_tool_limits_and_closes_partial_state(
        self,
    ) -> None:
        configuration = McpHttpServerConfig(
            name="fixture",
            url="https://mcp.fixture.test/mcp",
        )
        with self.assertRaisesRegex(McpHttpError, "too_many_mcp_servers"):
            await McpHttpToolCollection.open(
                tuple(configuration for _ in range(mcp_http.MAX_MCP_SERVERS + 1))
            )

        snapshot = SimpleNamespace(
            tools=(SimpleNamespace(name="echo", description="echo", inputSchema={}),),
            resources=(),
            resource_templates=(),
            prompts=(),
        )
        close = AsyncMock()
        with (
            patch.object(
                mcp_http._McpHttpServerConnection,
                "start",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(mcp_http._McpHttpServerConnection, "close", new=close),
            self.assertRaisesRegex(McpHttpError, "tool_name_collision"),
        ):
            await McpHttpToolCollection.open((configuration, configuration))
        self.assertEqual(close.await_count, 2)

        close.reset_mock()
        with (
            patch.object(
                mcp_http._McpHttpServerConnection,
                "start",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(mcp_http._McpHttpServerConnection, "close", new=close),
            patch.object(mcp_http, "MAX_MCP_TOTAL_TOOLS", 0),
            self.assertRaisesRegex(McpHttpError, "too_many_mcp_tools"),
        ):
            await McpHttpToolCollection.open((configuration,))
        self.assertEqual(close.await_count, 1)

    async def test_paginated_listing_and_cursor_guards_are_fail_closed(self) -> None:
        connection = mcp_http._McpHttpServerConnection(
            McpHttpServerConfig(name="fixture", url="https://mcp.fixture.test/mcp"),
            explicit_redactions=(),
            sampling_handler=None,
            elicitation_handler=None,
        )
        tool = SimpleNamespace(name="echo", description="echo", inputSchema={})
        resource = SimpleNamespace(
            uri="fixture://resource",
            name="resource",
            title=None,
            description=None,
            mimeType="text/plain",
            size=None,
        )
        template = SimpleNamespace(name="template", uriTemplate="fixture://{name}")
        prompt = SimpleNamespace(name="prompt", title=None, description=None, arguments=[])

        class PagedSession:
            async def list_tools(self, cursor: str | None) -> object:
                return SimpleNamespace(
                    tools=[tool],
                    nextCursor=None if cursor == "second" else ("second" if cursor else "first"),
                )

            async def list_resources(self, cursor: str | None) -> object:
                return SimpleNamespace(
                    resources=[resource],
                    nextCursor=None if cursor == "second" else ("second" if cursor else "first"),
                )

            async def list_resource_templates(self, cursor: str | None) -> object:
                return SimpleNamespace(
                    resourceTemplates=[template],
                    nextCursor=None if cursor == "second" else ("second" if cursor else "first"),
                )

            async def list_prompts(self, cursor: str | None) -> object:
                return SimpleNamespace(
                    prompts=[prompt],
                    nextCursor=None if cursor == "second" else ("second" if cursor else "first"),
                )

        session = PagedSession()
        self.assertEqual(len(await connection._list_tools(session)), 3)
        self.assertEqual(len(await connection._list_resources(session)), 3)
        self.assertEqual(len(await connection._list_resource_templates(session)), 3)
        self.assertEqual(len(await connection._list_prompts(session)), 3)

        class CyclingSession:
            async def list_tools(self, _cursor: str | None) -> object:
                return SimpleNamespace(tools=[], nextCursor="cycle")

            async def list_resources(self, _cursor: str | None) -> object:
                return SimpleNamespace(resources=[], nextCursor="cycle")

            async def list_resource_templates(self, _cursor: str | None) -> object:
                return SimpleNamespace(resourceTemplates=[], nextCursor="cycle")

            async def list_prompts(self, _cursor: str | None) -> object:
                return SimpleNamespace(prompts=[], nextCursor="cycle")

        cycling = CyclingSession()
        for method, reason in (
            (connection._list_tools, "tool_cursor_cycle"),
            (connection._list_resources, "resource_cursor_cycle"),
            (connection._list_resource_templates, "resource_template_cursor_cycle"),
            (connection._list_prompts, "prompt_cursor_cycle"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(McpHttpError, reason):
                await method(cycling)

    async def test_auxiliary_requests_and_mcp_callbacks_are_bounded(self) -> None:
        sampling_calls: list[tuple[object, object, object, object]] = []
        elicitation_calls: list[tuple[object, object, object]] = []

        async def sampling(
            messages: object,
            *,
            model_preferences: object = None,
            system_prompt: object = None,
            max_tokens: object = None,
        ) -> dict[str, object]:
            sampling_calls.append((messages, model_preferences, system_prompt, max_tokens))
            return {
                "role": "assistant",
                "content": {"type": "text", "text": "sampled"},
                "model": "fixture",
            }

        async def elicitation(
            message: str,
            requested_schema: object,
            *,
            url: str | None = None,
        ) -> dict[str, object]:
            elicitation_calls.append((message, requested_schema, url))
            return {"action": "accept", "content": {"answer": "yes"}}

        connection = mcp_http._McpHttpServerConnection(
            McpHttpServerConfig(name="fixture", url="https://mcp.fixture.test/mcp"),
            explicit_redactions=(),
            sampling_handler=sampling,
            elicitation_handler=elicitation,
        )

        class Session:
            async def read_resource(self, uri: str) -> str:
                return uri

            async def get_prompt(self, name: str, arguments: dict[str, str]) -> dict[str, object]:
                return {"name": name, "arguments": arguments}

        loop = asyncio.get_running_loop()
        read_request = mcp_http._AuxRequest(
            "read_resource", {"uri": "fixture://resource"}, 1, loop.create_future(), asyncio.Event()
        )
        prompt_request = mcp_http._AuxRequest(
            "get_prompt",
            {"name": "prompt", "arguments": {"topic": "test"}},
            1,
            loop.create_future(),
            asyncio.Event(),
        )
        refresh_request = mcp_http._AuxRequest(
            "refresh", {}, 1, loop.create_future(), asyncio.Event()
        )
        unsupported_request = mcp_http._AuxRequest(
            "unknown", {}, 1, loop.create_future(), asyncio.Event()
        )
        for request in (read_request, prompt_request, refresh_request, unsupported_request):
            connection._list_snapshot = lambda _session: asyncio.sleep(0, result="refreshed")  # type: ignore[method-assign]
            await connection._execute_auxiliary(Session(), request)
        self.assertEqual(read_request.result.result(), "fixture://resource")
        self.assertEqual(prompt_request.result.result()["name"], "prompt")
        self.assertEqual(refresh_request.result.result(), "refreshed")
        with self.assertRaisesRegex(McpHttpError, "auxiliary_request_failed"):
            unsupported_request.result.result()

        params = SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "messages": [{"role": "user"}],
                "modelPreferences": {"hints": []},
                "systemPrompt": "system",
                "maxTokens": 8,
            }
        )
        sampled = await connection._sampling_callback(None, params)
        elicited = await connection._elicitation_callback(
            None,
            SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "message": "Choose",
                    "requestedSchema": {"type": "object"},
                    "url": "https://example.invalid/form",
                }
            ),
        )
        self.assertEqual(sampled.model, "fixture")
        self.assertEqual(elicited.action, "accept")
        self.assertEqual(sampling_calls[0][2], "system")
        self.assertEqual(elicitation_calls[0][0], "Choose")

    async def test_inactive_connection_and_transport_configuration_fail_closed(self) -> None:
        connection = mcp_http._McpHttpServerConnection(
            McpHttpServerConfig(name="fixture", url="https://mcp.fixture.test/mcp"),
            explicit_redactions=(),
            sampling_handler=None,
            elicitation_handler=None,
        )
        with self.assertRaisesRegex(McpHttpError, "not_active"):
            await connection.call("echo", {}, timeout_seconds=1)
        with self.assertRaisesRegex(McpHttpError, "not_active"):
            await connection.refresh()
        with self.assertRaisesRegex(McpHttpError, "not_active"):
            await connection.read_resource("fixture://missing")
        with self.assertRaisesRegex(McpHttpError, "not_active"):
            await connection.get_prompt("missing", {})
        await connection.close()
        await connection.close()
        with self.assertRaisesRegex(McpHttpError, "auth"):
            mcp_http._mcp_http_client(auth=object())

    async def test_remote_transport_rejects_unknown_transport(self) -> None:
        configuration = McpHttpServerConfig(
            name="fixture",
            url="https://mcp.fixture.test/mcp",
            transport="invalid",  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(McpHttpError, "unsupported"):
            async with mcp_http._remote_transport(configuration):
                pass

    async def test_streamable_http_and_sse_use_official_sdk_and_redact_headers(self) -> None:
        for transport in ("http", "sse"):
            with self.subTest(transport=transport):
                fixture = _RemoteMcpTransportFixture(transport)
                with patch("neuro_code.infrastructure.mcp.http._mcp_http_client", fixture.client):
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
                    self.assertEqual(collection.resources[0].uri, "fixture://http-resource")
                    self.assertEqual(collection.resource_templates[0].name, "http-template")
                    self.assertEqual(collection.prompts[0].name, "http-prompt")
                    contents = await collection.read_resource("fixture://http-resource")
                    self.assertEqual(contents[0].text, "http resource text")
                    messages = await collection.get_prompt("http-prompt", {"topic": "test"})
                    self.assertEqual(messages[0].content["text"], "http prompt text")
                    await collection.refresh()
                    with self.assertRaisesRegex(McpHttpError, "resource_not_found"):
                        await collection.read_resource("fixture://missing")
                    with self.assertRaisesRegex(McpHttpError, "prompt_not_found"):
                        await collection.get_prompt("missing", {})
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
        with patch("neuro_code.infrastructure.mcp.http._mcp_http_client", fixture.client):
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
