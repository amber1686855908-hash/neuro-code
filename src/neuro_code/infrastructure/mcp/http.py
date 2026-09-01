"""Canonical HTTP and SSE MCP infrastructure owner.

定义规范的 HTTP 和 SSE MCP 基础设施所有者."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, cast

import httpx
import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from neuro_code.application.ports.mcp import (
    McpElicitationHandler,
    McpPrompt,
    McpPromptMessage,
    McpResource,
    McpResourceContent,
    McpResourceTemplate,
    McpSamplingHandler,
)
from neuro_code.infrastructure.mcp.stdio import (
    MAX_MCP_LIST_PAGES,
    MAX_MCP_SERVER_TOOLS,
    MAX_MCP_SERVERS,
    MAX_MCP_TOTAL_TOOLS,
    MCP_CALL_TIMEOUT_SECONDS,
    MCP_CLOSE_TIMEOUT_SECONDS,
    MCP_INITIALIZE_TIMEOUT_SECONDS,
    McpStdioError,
    McpTool,
    _AuxRequest,
    _McpSnapshot,
    _prompt_descriptors,
    _prompt_messages,
    _resource_contents,
    _resource_descriptors,
    _resource_template_descriptors,
    _tool_definition,
)

MAX_MCP_HTTP_RESPONSE_BYTES = 1024 * 1024


class McpHttpError(McpStdioError):
    """Bounded, stable failure raised by a remote session-owned MCP adapter.

    表示远程会话拥有 MCP 适配器抛出的有界稳定失败."""


@dataclass(frozen=True, slots=True)
class McpHttpServerConfig:
    """Interface-neutral configuration for one HTTP or SSE MCP server.

    表示一个 HTTP 或 SSE MCP 服务器的接口无关配置."""

    name: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    transport: Literal["http", "sse"] = "http"


@dataclass(slots=True)
class _CallRequest:
    name: str
    arguments: dict[str, Any]
    timeout_seconds: float
    result: asyncio.Future[mcp_types.CallToolResult]
    cancelled: asyncio.Event


class McpHttpToolCollection:
    """Own all remote MCP connections and model tools for one ACP session.

    管理一个 ACP 会话的所有远程 MCP 连接和模型工具."""

    def __init__(
        self,
        connections: tuple[_McpHttpServerConnection, ...],
        tools: tuple[McpTool, ...],
    ) -> None:
        self._connections = connections
        self.tools = tools
        self.resources: tuple[McpResource, ...] = tuple(
            resource
            for connection in connections
            for resource in _resource_descriptors(
                connection.configuration.name,
                connection.resources,
            )
        )
        self.resource_templates: tuple[McpResourceTemplate, ...] = tuple(
            resource
            for connection in connections
            for resource in _resource_template_descriptors(
                connection.configuration.name,
                connection.resource_templates,
            )
        )
        self.prompts: tuple[McpPrompt, ...] = tuple(
            prompt
            for connection in connections
            for prompt in _prompt_descriptors(connection.configuration.name, connection.prompts)
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        configurations: Sequence[McpHttpServerConfig],
        *,
        explicit_redactions: Sequence[str] = (),
        sampling_handler: McpSamplingHandler | None = None,
        elicitation_handler: McpElicitationHandler | None = None,
    ) -> McpHttpToolCollection:
        if len(configurations) > MAX_MCP_SERVERS:
            raise McpHttpError("too_many_mcp_servers")
        connections: list[_McpHttpServerConnection] = []
        tools: list[McpTool] = []
        names: set[str] = set()
        try:
            for configuration in configurations:
                connection = _McpHttpServerConnection(
                    configuration,
                    explicit_redactions=explicit_redactions,
                    sampling_handler=sampling_handler,
                    elicitation_handler=elicitation_handler,
                )
                connections.append(connection)
                snapshot = await connection.start()
                for remote_tool in snapshot.tools:
                    definition = _tool_definition(
                        configuration.name,
                        remote_tool,
                        explicit_redactions=connection.explicit_redactions,
                    )
                    if definition.name in names:
                        raise McpHttpError("mcp_tool_name_collision")
                    names.add(definition.name)
                    tools.append(
                        McpTool(
                            connection=connection,
                            definition=definition,
                            remote_name=remote_tool.name,
                        )
                    )
                    if len(tools) > MAX_MCP_TOTAL_TOOLS:
                        raise McpHttpError("too_many_mcp_tools")
            return cls(tuple(connections), tuple(tools))
        except BaseException:
            if connections:
                await asyncio.gather(
                    *(connection.close() for connection in reversed(connections)),
                    return_exceptions=True,
                )
            raise

    async def refresh(self) -> None:
        snapshots = await asyncio.gather(
            *(connection.refresh() for connection in self._connections)
        )
        tools: list[McpTool] = []
        resources: list[McpResource] = []
        templates: list[McpResourceTemplate] = []
        prompts: list[McpPrompt] = []
        names: set[str] = set()
        for connection, snapshot in zip(self._connections, snapshots, strict=True):
            for remote_tool in snapshot.tools:
                definition = _tool_definition(
                    connection.configuration.name,
                    remote_tool,
                    explicit_redactions=connection.explicit_redactions,
                )
                if definition.name in names:
                    raise McpHttpError("mcp_tool_name_collision")
                names.add(definition.name)
                tools.append(
                    McpTool(
                        connection=connection,
                        definition=definition,
                        remote_name=remote_tool.name,
                    )
                )
            resources.extend(
                _resource_descriptors(connection.configuration.name, snapshot.resources)
            )
            templates.extend(
                _resource_template_descriptors(
                    connection.configuration.name,
                    snapshot.resource_templates,
                )
            )
            prompts.extend(_prompt_descriptors(connection.configuration.name, snapshot.prompts))
        if len(tools) > MAX_MCP_TOTAL_TOOLS:
            raise McpHttpError("too_many_mcp_tools")
        self.tools = tuple(tools)
        self.resources = tuple(resources)
        self.resource_templates = tuple(templates)
        self.prompts = tuple(prompts)

    async def read_resource(self, uri: str) -> tuple[McpResourceContent, ...]:
        for connection in self._connections:
            if any(str(resource.uri) == uri for resource in connection.resources):
                return _resource_contents(await connection.read_resource(uri))
        raise McpHttpError("mcp_resource_not_found")

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> tuple[McpPromptMessage, ...]:
        for connection in self._connections:
            if any(prompt.name == name for prompt in connection.prompts):
                return _prompt_messages(await connection.get_prompt(name, dict(arguments or {})))
        raise McpHttpError("mcp_prompt_not_found")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.gather(
                *(connection.close() for connection in reversed(self._connections)),
                return_exceptions=True,
            )


class _McpHttpServerConnection:
    def __init__(
        self,
        configuration: McpHttpServerConfig,
        *,
        explicit_redactions: Sequence[str],
        sampling_handler: McpSamplingHandler | None,
        elicitation_handler: McpElicitationHandler | None,
    ) -> None:
        self._configuration = configuration
        self._sampling_handler = sampling_handler
        self._elicitation_handler = elicitation_handler
        self.explicit_redactions = tuple(
            dict.fromkeys(
                (
                    *(value for value in explicit_redactions if value),
                    *(value for _, value in configuration.headers if value),
                )
            )
        )
        self._commands: asyncio.Queue[_CallRequest | _AuxRequest | None] = asyncio.Queue()
        self._ready: asyncio.Future[_McpSnapshot] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._current: _CallRequest | _AuxRequest | None = None
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.resources: tuple[mcp_types.Resource, ...] = ()
        self.resource_templates: tuple[mcp_types.ResourceTemplate, ...] = ()
        self.prompts: tuple[mcp_types.Prompt, ...] = ()

    @property
    def configuration(self) -> McpHttpServerConfig:
        return self._configuration

    async def start(self) -> _McpSnapshot:
        if self._owner is not None:
            raise McpHttpError("mcp_server_already_started")
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._owner = asyncio.create_task(
            self._run(),
            name=f"neuro-code-mcp-{self._configuration.transport}-{self._configuration.name}",
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._ready),
                timeout=MCP_INITIALIZE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            await asyncio.shield(self.close())
            raise
        except TimeoutError:
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            await self.close()
            raise McpHttpError("mcp_server_initialization_timeout") from None
        except McpStdioError as error:
            await self.close()
            raise McpHttpError(error.reason) from None
        except Exception:
            await self.close()
            raise McpHttpError("mcp_server_initialization_failed") from None

    async def refresh(self) -> _McpSnapshot:
        return cast(_McpSnapshot, await self._auxiliary("refresh", {}))

    async def read_resource(self, uri: str) -> mcp_types.ReadResourceResult:
        return cast(
            mcp_types.ReadResourceResult,
            await self._auxiliary("read_resource", {"uri": uri}),
        )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> mcp_types.GetPromptResult:
        return cast(
            mcp_types.GetPromptResult,
            await self._auxiliary("get_prompt", {"name": name, "arguments": arguments}),
        )

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> mcp_types.CallToolResult:
        if self._closing or self._closed or self._owner is None or self._owner.done():
            raise McpHttpError("mcp_server_not_active")
        loop = asyncio.get_running_loop()
        request = _CallRequest(
            name,
            arguments,
            timeout_seconds,
            loop.create_future(),
            asyncio.Event(),
        )
        await self._commands.put(request)
        try:
            return await asyncio.shield(request.result)
        except asyncio.CancelledError:
            request.cancelled.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(request.result)
            raise

    async def _auxiliary(self, operation: str, arguments: dict[str, Any]) -> Any:
        if self._closing or self._closed or self._owner is None or self._owner.done():
            raise McpHttpError("mcp_server_not_active")
        loop = asyncio.get_running_loop()
        request = _AuxRequest(
            operation,
            arguments,
            MCP_CALL_TIMEOUT_SECONDS,
            loop.create_future(),
            asyncio.Event(),
        )
        await self._commands.put(request)
        try:
            return await asyncio.shield(request.result)
        except asyncio.CancelledError:
            request.cancelled.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(request.result)
            raise

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            current = self._current
            if current is not None:
                current.cancelled.set()
            await self._commands.put(None)
            owner = self._owner
            if owner is not None and owner is not asyncio.current_task():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(owner),
                        timeout=MCP_CLOSE_TIMEOUT_SECONDS * 2,
                    )
                except TimeoutError:
                    owner.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await owner
            self._closed = True

    async def _run(self) -> None:
        failure_reason = "mcp_server_connection_closed"
        try:
            async with (
                _remote_transport(self._configuration) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=MCP_INITIALIZE_TIMEOUT_SECONDS),
                    sampling_callback=cast(
                        Any,
                        self._sampling_callback if self._sampling_handler is not None else None,
                    ),
                    elicitation_callback=cast(
                        Any,
                        self._elicitation_callback
                        if self._elicitation_handler is not None
                        else None,
                    ),
                ) as session,
            ):
                await session.initialize()
                snapshot = await self._list_snapshot(session)
                self._set_ready_result(snapshot)
                while not self._closing:
                    command = await self._commands.get()
                    if command is None:
                        break
                    self._current = command
                    if isinstance(command, _CallRequest):
                        should_continue = await self._execute_call(session, command)
                    else:
                        should_continue = await self._execute_auxiliary(session, command)
                    if not should_continue:
                        failure_reason = "mcp_tool_call_aborted"
                        break
                    self._current = None
        except asyncio.CancelledError:
            failure_reason = "mcp_server_cancelled"
            self._set_ready_exception(McpHttpError(failure_reason))
            raise
        except McpStdioError as error:
            failure_reason = error.reason
            self._set_ready_exception(McpHttpError(failure_reason))
        except Exception:
            failure_reason = "mcp_server_failure"
            self._set_ready_exception(McpHttpError(failure_reason))
        finally:
            self._closing = True
            current = self._current
            self._reject_queued_calls(failure_reason)
            if current is not None and not current.result.done():
                current.result.set_exception(McpHttpError(failure_reason))
            self._closed = True

    async def _execute_auxiliary(
        self,
        session: ClientSession,
        request: _AuxRequest,
    ) -> bool:
        try:
            if request.operation == "refresh":
                result: Any = await self._list_snapshot(session)
            elif request.operation == "read_resource":
                result = await asyncio.wait_for(
                    session.read_resource(request.arguments["uri"]),
                    timeout=request.timeout_seconds,
                )
            elif request.operation == "get_prompt":
                result = await asyncio.wait_for(
                    session.get_prompt(
                        request.arguments["name"],
                        request.arguments["arguments"],
                    ),
                    timeout=request.timeout_seconds,
                )
            else:
                raise McpHttpError("mcp_auxiliary_operation_unsupported")
            if not request.result.done():
                request.result.set_result(result)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            if not request.result.done():
                request.result.set_exception(McpHttpError("mcp_auxiliary_request_failed"))
            return True

    async def _sampling_callback(self, _context: Any, params: Any) -> Any:
        handler = self._sampling_handler
        if handler is None:
            raise McpHttpError("mcp_sampling_unavailable")
        payload = params.model_dump(by_alias=True, exclude_none=True)
        result = await handler(
            payload.get("messages", []),
            model_preferences=payload.get("modelPreferences"),
            system_prompt=payload.get("systemPrompt"),
            max_tokens=payload.get("maxTokens"),
        )
        return mcp_types.CreateMessageResult.model_validate(result)

    async def _elicitation_callback(self, _context: Any, params: Any) -> Any:
        handler = self._elicitation_handler
        if handler is None:
            raise McpHttpError("mcp_elicitation_unavailable")
        payload = params.model_dump(by_alias=True, exclude_none=True)
        result = await handler(
            str(payload.get("message", "")),
            payload.get("requestedSchema"),
            url=payload.get("url"),
        )
        return mcp_types.ElicitResult.model_validate(result)

    async def _execute_call(
        self,
        session: ClientSession,
        request: _CallRequest,
    ) -> bool:
        call_task = asyncio.create_task(
            session.call_tool(
                request.name,
                request.arguments,
                read_timeout_seconds=timedelta(seconds=request.timeout_seconds),
            )
        )
        cancel_task = asyncio.create_task(request.cancelled.wait())
        done, pending = await asyncio.wait(
            (call_task, cancel_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if call_task in done:
            try:
                result = call_task.result()
            except Exception:
                await asyncio.gather(cancel_task, return_exceptions=True)
                return False
            else:
                if not request.result.done():
                    request.result.set_result(result)
            await asyncio.gather(cancel_task, return_exceptions=True)
            return True

        call_task.cancel()
        await asyncio.gather(call_task, cancel_task, return_exceptions=True)
        # A remote server is outside this process tree. Closing the SDK transport
        # fails this connection closed rather than pretending a cancelled HTTP
        # request stopped a potentially side-effecting remote operation.
        return False

    async def _list_tools(self, session: ClientSession) -> tuple[mcp_types.Tool, ...]:
        tools: list[mcp_types.Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_MCP_LIST_PAGES):
            result = await session.list_tools(cursor)
            tools.extend(result.tools)
            if len(tools) > MAX_MCP_SERVER_TOOLS:
                raise McpHttpError("too_many_mcp_server_tools")
            cursor = result.nextCursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen_cursors:
                raise McpHttpError("mcp_tool_cursor_cycle")
            seen_cursors.add(cursor)
        raise McpHttpError("too_many_mcp_tool_pages")

    async def _list_snapshot(self, session: ClientSession) -> _McpSnapshot:
        tools = await self._list_tools(session)
        capabilities = session.get_server_capabilities()
        resources = (
            await self._list_resources(session)
            if capabilities is not None and capabilities.resources is not None
            else ()
        )
        resource_templates = (
            await self._list_resource_templates(session)
            if capabilities is not None and capabilities.resources is not None
            else ()
        )
        prompts = (
            await self._list_prompts(session)
            if capabilities is not None and capabilities.prompts is not None
            else ()
        )
        self.resources = resources
        self.resource_templates = resource_templates
        self.prompts = prompts
        return _McpSnapshot(tools, resources, resource_templates, prompts)

    async def _list_resources(
        self,
        session: ClientSession,
    ) -> tuple[mcp_types.Resource, ...]:
        resources: list[mcp_types.Resource] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_MCP_LIST_PAGES):
            result = await session.list_resources(cursor)
            resources.extend(result.resources)
            if len(resources) > 512:
                raise McpHttpError("too_many_mcp_resources")
            cursor = result.nextCursor
            if cursor is None:
                return tuple(resources)
            if cursor in seen:
                raise McpHttpError("mcp_resource_cursor_cycle")
            seen.add(cursor)
        raise McpHttpError("too_many_mcp_resource_pages")

    async def _list_resource_templates(
        self,
        session: ClientSession,
    ) -> tuple[mcp_types.ResourceTemplate, ...]:
        templates: list[mcp_types.ResourceTemplate] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_MCP_LIST_PAGES):
            result = await session.list_resource_templates(cursor)
            templates.extend(result.resourceTemplates)
            if len(templates) > 512:
                raise McpHttpError("too_many_mcp_resource_templates")
            cursor = result.nextCursor
            if cursor is None:
                return tuple(templates)
            if cursor in seen:
                raise McpHttpError("mcp_resource_template_cursor_cycle")
            seen.add(cursor)
        raise McpHttpError("too_many_mcp_resource_template_pages")

    async def _list_prompts(self, session: ClientSession) -> tuple[mcp_types.Prompt, ...]:
        prompts: list[mcp_types.Prompt] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_MCP_LIST_PAGES):
            result = await session.list_prompts(cursor)
            prompts.extend(result.prompts)
            if len(prompts) > 128:
                raise McpHttpError("too_many_mcp_prompts")
            cursor = result.nextCursor
            if cursor is None:
                return tuple(prompts)
            if cursor in seen:
                raise McpHttpError("mcp_prompt_cursor_cycle")
            seen.add(cursor)
        raise McpHttpError("too_many_mcp_prompt_pages")

    def _set_ready_result(self, snapshot: _McpSnapshot) -> None:
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_result(snapshot)

    def _set_ready_exception(self, error: Exception) -> None:
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_exception(error)

    def _reject_queued_calls(self, reason: str) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            if request is not None and not request.result.done():
                request.result.set_exception(McpHttpError(reason))


@asynccontextmanager
async def _remote_transport(
    configuration: McpHttpServerConfig,
) -> AsyncIterator[tuple[Any, Any]]:
    headers = dict(configuration.headers)
    if configuration.transport == "http":
        async with (
            _mcp_http_client(headers) as client,
            streamable_http_client(
                configuration.url,
                http_client=client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _get_session_id),
        ):
            yield read_stream, write_stream
        return
    if configuration.transport == "sse":
        async with sse_client(
            configuration.url,
            headers=headers,
            timeout=MCP_INITIALIZE_TIMEOUT_SECONDS,
            sse_read_timeout=MCP_CALL_TIMEOUT_SECONDS,
            httpx_client_factory=_mcp_http_client,
        ) as (read_stream, write_stream):
            yield read_stream, write_stream
        return
    raise McpHttpError("mcp_transport_unsupported")


def _mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    if auth is not None:
        raise McpHttpError("mcp_http_auth_unsupported")
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout
        or httpx.Timeout(
            MCP_CALL_TIMEOUT_SECONDS,
            connect=MCP_INITIALIZE_TIMEOUT_SECONDS,
            pool=MCP_INITIALIZE_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
        trust_env=False,
        transport=_BoundedMcpHttpTransport(),
    )


class _BoundedMcpHttpResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream
        self._bytes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._bytes += len(chunk)
            if self._bytes > MAX_MCP_HTTP_RESPONSE_BYTES:
                raise McpHttpError("mcp_http_response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _BoundedMcpHttpTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_MCP_HTTP_RESPONSE_BYTES:
                await response.aclose()
                raise McpHttpError("mcp_http_response_too_large")
        response.stream = _BoundedMcpHttpResponseStream(
            cast(httpx.AsyncByteStream, response.stream)
        )
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()
