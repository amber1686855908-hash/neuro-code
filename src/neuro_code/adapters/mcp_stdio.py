from __future__ import annotations

import asyncio
import contextlib
import json
import math
import ntpath
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mcp_types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage

from neuro_code.adapters.process_tree import ProcessTree
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_MCP_SERVERS = 8
MAX_MCP_SERVER_TOOLS = 128
MAX_MCP_TOTAL_TOOLS = 256
MAX_MCP_TOOL_NAME_BYTES = 128
MAX_MCP_TOOL_DESCRIPTION_BYTES = 8 * 1024
MAX_MCP_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_MCP_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_MCP_TOOL_RESULT_BYTES = 128 * 1024
MAX_MCP_STDIO_FRAME_BYTES = 1024 * 1024
MAX_MCP_LIST_PAGES = 16
MAX_MCP_JSON_DEPTH = 32
MAX_MCP_JSON_NODES = 8_192
MCP_INITIALIZE_TIMEOUT_SECONDS = 30.0
MCP_CLOSE_TIMEOUT_SECONDS = 2.0
MCP_CALL_TIMEOUT_SECONDS = 120.0

_MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")


class McpStdioError(RuntimeError):
    """Bounded, stable failure raised by the session-scoped MCP adapter."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class McpStdioServerConfig:
    """Interface-neutral configuration for one session-owned stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class _CallRequest:
    name: str
    arguments: dict[str, Any]
    timeout_seconds: float
    result: asyncio.Future[mcp_types.CallToolResult]
    cancelled: asyncio.Event


class McpStdioTool:
    """Model tool projection of one official-SDK MCP tool."""

    side_effecting = True

    def __init__(
        self,
        *,
        connection: _McpServerConnection,
        definition: ToolDefinition,
        remote_name: str,
    ) -> None:
        self._connection = connection
        self.definition = definition
        self._remote_name = remote_name

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            copied_arguments = _validated_arguments(arguments)
        except McpStdioError as error:
            raise ToolError(f"invalid MCP tool arguments: {error.reason}") from None
        timeout = context.command_timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            raise ToolError("MCP tool timeout must be positive")
        timeout = min(timeout, MCP_CALL_TIMEOUT_SECONDS)
        try:
            result = await self._connection.call(
                self._remote_name,
                copied_arguments,
                timeout_seconds=timeout,
            )
        except asyncio.CancelledError:
            raise
        except McpStdioError as error:
            raise ToolError(f"MCP tool failed: {error.reason}") from None
        return _render_tool_result(
            result,
            explicit_redactions=self._connection.explicit_redactions,
        )


class McpStdioToolCollection:
    """Own all stdio MCP servers and model tools for one ACP session."""

    def __init__(
        self,
        connections: tuple[_McpServerConnection, ...],
        tools: tuple[McpStdioTool, ...],
    ) -> None:
        self._connections = connections
        self.tools = tools
        self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        configurations: Sequence[McpStdioServerConfig],
        *,
        cwd: Path,
        explicit_redactions: Sequence[str] = (),
    ) -> McpStdioToolCollection:
        if len(configurations) > MAX_MCP_SERVERS:
            raise McpStdioError("too_many_mcp_servers")
        connections: list[_McpServerConnection] = []
        tools: list[McpStdioTool] = []
        names: set[str] = set()
        try:
            for configuration in configurations:
                connection = _McpServerConnection(
                    configuration,
                    cwd=cwd,
                    explicit_redactions=explicit_redactions,
                )
                remote_tools = await connection.start()
                connections.append(connection)
                for remote_tool in remote_tools:
                    definition = _tool_definition(
                        configuration.name,
                        remote_tool,
                        explicit_redactions=connection.explicit_redactions,
                    )
                    if definition.name in names:
                        raise McpStdioError("mcp_tool_name_collision")
                    names.add(definition.name)
                    tools.append(
                        McpStdioTool(
                            connection=connection,
                            definition=definition,
                            remote_name=remote_tool.name,
                        )
                    )
                    if len(tools) > MAX_MCP_TOTAL_TOOLS:
                        raise McpStdioError("too_many_mcp_tools")
            return cls(tuple(connections), tuple(tools))
        except BaseException:
            if connections:
                await asyncio.gather(
                    *(connection.close() for connection in reversed(connections)),
                    return_exceptions=True,
                )
            raise

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.gather(
                *(connection.close() for connection in reversed(self._connections)),
                return_exceptions=True,
            )


class _McpServerConnection:
    def __init__(
        self,
        configuration: McpStdioServerConfig,
        *,
        cwd: Path,
        explicit_redactions: Sequence[str],
    ) -> None:
        self._configuration = configuration
        self._cwd = cwd
        self.explicit_redactions = tuple(
            dict.fromkeys(
                (
                    *(value for value in explicit_redactions if value),
                    *(value for _, value in configuration.env if value),
                )
            )
        )
        self._commands: asyncio.Queue[_CallRequest | None] = asyncio.Queue()
        self._ready: asyncio.Future[tuple[mcp_types.Tool, ...]] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._current: _CallRequest | None = None
        self._closing = False
        self._closed = False
        self._transport_ended = asyncio.Event()
        self._close_lock = asyncio.Lock()

    async def start(self) -> tuple[mcp_types.Tool, ...]:
        if self._owner is not None:
            raise McpStdioError("mcp_server_already_started")
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._owner = asyncio.create_task(
            self._run(),
            name=f"neuro-code-mcp-{self._configuration.name}",
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
            raise McpStdioError("mcp_server_initialization_timeout") from None
        except Exception:
            await self.close()
            raise McpStdioError("mcp_server_initialization_failed") from None

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> mcp_types.CallToolResult:
        if self._closing or self._closed or self._owner is None or self._owner.done():
            raise McpStdioError("mcp_server_not_active")
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
                # The owner resolves this only after the process tree has been
                # terminated, so the ACP prompt cannot complete while a
                # cancelled remote side effect is still running.
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
        tree: ProcessTree | None = None
        transport_tasks: list[asyncio.Task[None]] = []
        read_send: MemoryObjectSendStream[SessionMessage | Exception] | None = None
        write_send: MemoryObjectSendStream[SessionMessage] | None = None
        failure_reason = "mcp_server_connection_closed"
        try:
            environment = _server_environment(self._configuration)
            executable = await _resolved_executable(
                self._configuration.command,
                cwd=self._cwd,
                environment=environment,
            )
            arguments = self._configuration.args
            if os.name == "nt":
                executable, arguments = _windows_server_command(
                    executable,
                    arguments,
                    environment,
                )
            tree = await ProcessTree.spawn_exec(
                executable,
                arguments,
                cwd=self._cwd,
                env=environment,
                pipe_stdin=True,
            )
            read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](
                16
            )
            write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](16)
            transport_tasks = [
                asyncio.create_task(
                    self._stdout_reader(tree, read_send),
                    name=f"neuro-code-mcp-stdout-{self._configuration.name}",
                ),
                asyncio.create_task(
                    self._stdin_writer(tree, write_receive),
                    name=f"neuro-code-mcp-stdin-{self._configuration.name}",
                ),
                asyncio.create_task(
                    self._stderr_drainer(tree),
                    name=f"neuro-code-mcp-stderr-{self._configuration.name}",
                ),
                asyncio.create_task(
                    self._process_watcher(tree, read_send),
                    name=f"neuro-code-mcp-wait-{self._configuration.name}",
                ),
            ]
            async with ClientSession(
                read_receive,
                write_send,
                read_timeout_seconds=timedelta(seconds=MCP_INITIALIZE_TIMEOUT_SECONDS),
            ) as session:
                await session.initialize()
                tools = await self._list_tools(session)
                self._set_ready_result(tools)
                while True:
                    if self._closing or self._transport_ended.is_set():
                        break
                    command = await self._next_command()
                    if command is None:
                        break
                    self._current = command
                    if self._connection_closing():
                        failure_reason = "mcp_server_connection_closed"
                        break
                    should_continue = await self._execute_call(session, command)
                    if not should_continue:
                        failure_reason = "mcp_tool_call_aborted"
                        break
                    self._current = None
        except asyncio.CancelledError:
            failure_reason = "mcp_server_cancelled"
            self._set_ready_exception(McpStdioError(failure_reason))
            raise
        except Exception:
            failure_reason = "mcp_server_failure"
            self._set_ready_exception(McpStdioError(failure_reason))
        finally:
            self._closing = True
            current = self._current
            self._reject_queued_calls(failure_reason)
            if write_send is not None:
                with contextlib.suppress(BaseException):
                    await write_send.aclose()
            if read_send is not None:
                with contextlib.suppress(BaseException):
                    await read_send.aclose()
            for task in transport_tasks:
                task.cancel()
            if transport_tasks:
                await asyncio.gather(*transport_tasks, return_exceptions=True)
            if tree is not None:
                await self._close_tree(tree)
            if current is not None and not current.result.done():
                current.result.set_exception(McpStdioError(failure_reason))
            self._closed = True

    def _connection_closing(self) -> bool:
        """Re-read mutable close state after an await boundary."""

        return self._closing

    async def _next_command(self) -> _CallRequest | None:
        command_task = asyncio.create_task(self._commands.get())
        ended_task = asyncio.create_task(self._transport_ended.wait())
        done, pending = await asyncio.wait(
            (command_task, ended_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if command_task in done:
            command = command_task.result()
            await asyncio.gather(ended_task, return_exceptions=True)
            return command
        command_task.cancel()
        await asyncio.gather(command_task, ended_task, return_exceptions=True)
        return None

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
        transport_task = asyncio.create_task(self._transport_ended.wait())
        done, pending = await asyncio.wait(
            (call_task, cancel_task, transport_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if call_task in done:
            try:
                result = call_task.result()
            except Exception:
                await asyncio.gather(cancel_task, transport_task, return_exceptions=True)
                return False
            else:
                if not request.result.done():
                    request.result.set_result(result)
            await asyncio.gather(cancel_task, transport_task, return_exceptions=True)
            return True

        call_task.cancel()
        await asyncio.gather(call_task, cancel_task, transport_task, return_exceptions=True)
        # The official SDK owns request IDs and dispatch. Once a caller cancels,
        # terminate this server connection so no unacknowledged remote side
        # effect can continue after the ACP prompt has returned.
        return False

    async def _list_tools(
        self,
        session: ClientSession,
    ) -> tuple[mcp_types.Tool, ...]:
        tools: list[mcp_types.Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_MCP_LIST_PAGES):
            result = await session.list_tools(cursor)
            tools.extend(result.tools)
            if len(tools) > MAX_MCP_SERVER_TOOLS:
                raise McpStdioError("too_many_mcp_server_tools")
            cursor = result.nextCursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen_cursors:
                raise McpStdioError("mcp_tool_cursor_cycle")
            seen_cursors.add(cursor)
        raise McpStdioError("too_many_mcp_tool_pages")

    async def _stdout_reader(
        self,
        tree: ProcessTree,
        send_stream: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        stream = tree.process.stdout
        if stream is None:
            self._transport_ended.set()
            return
        buffer = bytearray()
        try:
            async with send_stream:
                while True:
                    chunk = await stream.read(64 * 1024)
                    if not chunk:
                        if buffer:
                            raise McpStdioError("mcp_frame_missing_newline")
                        return
                    buffer.extend(chunk)
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            if len(buffer) > MAX_MCP_STDIO_FRAME_BYTES:
                                raise McpStdioError("mcp_frame_too_large")
                            break
                        if newline > MAX_MCP_STDIO_FRAME_BYTES:
                            raise McpStdioError("mcp_frame_too_large")
                        frame = bytes(buffer[:newline])
                        del buffer[: newline + 1]
                        if not frame:
                            raise McpStdioError("mcp_frame_invalid")
                        message = mcp_types.JSONRPCMessage.model_validate_json(frame)
                        await send_stream.send(SessionMessage(message))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._transport_ended.set()

    async def _stdin_writer(
        self,
        tree: ProcessTree,
        receive_stream: MemoryObjectReceiveStream[SessionMessage],
    ) -> None:
        try:
            async with receive_stream:
                async for session_message in receive_stream:
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    ).encode("utf-8")
                    if len(payload) > MAX_MCP_STDIO_FRAME_BYTES:
                        raise McpStdioError("mcp_frame_too_large")
                    await tree.write_stdin(payload + b"\n")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._transport_ended.set()

    @staticmethod
    async def _stderr_drainer(tree: ProcessTree) -> None:
        stream = tree.process.stderr
        if stream is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            while await stream.read(64 * 1024):
                pass

    async def _process_watcher(
        self,
        tree: ProcessTree,
        read_send: MemoryObjectSendStream[SessionMessage | Exception],
    ) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tree.process.wait()
            await asyncio.sleep(0.05)
        self._transport_ended.set()
        with contextlib.suppress(BaseException):
            await read_send.aclose()

    async def _close_tree(self, tree: ProcessTree) -> None:
        with contextlib.suppress(BaseException):
            await tree.close_stdin()
        try:
            await asyncio.wait_for(
                tree.wait(),
                timeout=MCP_CLOSE_TIMEOUT_SECONDS,
            )
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(
                    tree.terminate(
                        grace_seconds=0.5,
                        force_wait_seconds=MCP_CLOSE_TIMEOUT_SECONDS,
                    )
                )

    def _set_ready_result(self, tools: tuple[mcp_types.Tool, ...]) -> None:
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_result(tools)

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
                request.result.set_exception(McpStdioError(reason))


async def _resolved_executable(
    command: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str:
    def resolve() -> str | None:
        located = shutil.which(command, path=environment.get("PATH"))
        if located is not None:
            return located
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.is_file():
            return str(candidate.resolve())
        return None

    located = await run_blocking(resolve)
    if located is None:
        raise McpStdioError("mcp_server_command_not_found")
    return located


def _windows_server_command(
    executable: str,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    suffix = ntpath.splitext(executable)[1].casefold()
    if suffix not in {".bat", ".cmd"}:
        if suffix == ".ps1":
            raise McpStdioError("mcp_windows_powershell_wrapper_unsupported")
        return executable, arguments

    values = (executable, *arguments)
    if any(any(character in value for character in '"%\r\n') for value in values):
        raise McpStdioError("mcp_windows_batch_argument_unsupported")
    folded_environment = {name.casefold(): value for name, value in environment.items()}
    command_processor = folded_environment.get("comspec")
    if command_processor is None:
        system_root = folded_environment.get("systemroot")
        if system_root:
            command_processor = ntpath.join(system_root, "System32", "cmd.exe")
    if command_processor is None or not ntpath.isabs(command_processor):
        raise McpStdioError("mcp_windows_command_processor_unavailable")

    # /S /C applies special first/last quote handling. Quote every token so cmd
    # metacharacters remain data, then wrap the complete command in the outer
    # pair expected for a quoted batch path.
    command = '"' + " ".join(f'"{value}"' for value in values) + '"'
    return command_processor, ("/d", "/s", "/v:off", "/c", command)


def _server_environment(configuration: McpStdioServerConfig) -> dict[str, str]:
    environment = get_default_environment()
    environment.update(configuration.env)
    return environment


def _validated_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(arguments)
    try:
        size = len(
            json.dumps(
                copied,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise ToolError("MCP tool arguments must be finite JSON") from None
    if size > MAX_MCP_TOOL_ARGUMENT_BYTES:
        raise ToolError("MCP tool arguments exceed the byte limit")
    _bounded_json(copied, explicit_redactions=(), redact=False)
    return copied


def _tool_definition(
    server_name: str,
    tool: mcp_types.Tool,
    *,
    explicit_redactions: Sequence[str],
) -> ToolDefinition:
    name = _bounded_tool_name(tool.name)
    description = _bounded_text(
        tool.description or tool.title or "No description provided.",
        MAX_MCP_TOOL_DESCRIPTION_BYTES,
        explicit_redactions=explicit_redactions,
    )
    source = _bounded_text(
        server_name,
        512,
        explicit_redactions=explicit_redactions,
    )
    schema = _bounded_json(
        tool.inputSchema,
        explicit_redactions=explicit_redactions,
        redact=True,
    )
    if not isinstance(schema, dict):
        raise McpStdioError("mcp_tool_schema_invalid")
    if _serialized_size(schema) > MAX_MCP_TOOL_SCHEMA_BYTES:
        raise McpStdioError("mcp_tool_schema_too_large")
    return ToolDefinition(
        name,
        f"MCP server {source}: {description}",
        schema,
    )


def _bounded_tool_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or not _MCP_TOOL_NAME.fullmatch(name)
        or len(name.encode("utf-8")) > MAX_MCP_TOOL_NAME_BYTES
    ):
        raise McpStdioError("mcp_tool_name_invalid")
    return name


def _render_tool_result(
    result: mcp_types.CallToolResult,
    *,
    explicit_redactions: Sequence[str],
) -> ToolResult:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            text = _bounded_text(
                block.text,
                MAX_MCP_TOOL_RESULT_BYTES,
                explicit_redactions=explicit_redactions,
            )
            if text:
                parts.append(text)
        elif isinstance(block, mcp_types.ResourceLink):
            resource: dict[str, object] = {
                "uri": str(block.uri),
                "name": block.name,
            }
            for field_name, wire_name in (
                ("title", "title"),
                ("description", "description"),
                ("mimeType", "mimeType"),
                ("size", "size"),
            ):
                value = getattr(block, field_name)
                if value is not None:
                    resource[wire_name] = value
            annotations = _annotations_payload(block.annotations)
            if annotations:
                resource["annotations"] = annotations
            safe_resource = _bounded_json(
                resource,
                explicit_redactions=explicit_redactions,
                redact=True,
            )
            parts.append(
                "<resource_link>"
                + json.dumps(
                    safe_resource,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "</resource_link>"
            )
        elif isinstance(block, mcp_types.ImageContent):
            parts.append("[MCP image content omitted]")
        elif isinstance(block, mcp_types.AudioContent):
            parts.append("[MCP audio content omitted]")
        else:
            parts.append("[MCP embedded resource content omitted]")
    if result.structuredContent is not None:
        structured = _bounded_json(
            result.structuredContent,
            explicit_redactions=explicit_redactions,
            redact=True,
        )
        parts.append(
            "<structured_content>"
            + json.dumps(
                structured,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "</structured_content>"
        )
    rendered = "\n\n".join(part for part in parts if part)
    if not rendered:
        rendered = "(MCP tool returned no model-visible content)"
    return ToolResult(
        _truncate_utf8(rendered, MAX_MCP_TOOL_RESULT_BYTES),
        is_error=result.isError,
    )


def _annotations_payload(annotations: mcp_types.Annotations | None) -> dict[str, object]:
    if annotations is None:
        return {}
    payload: dict[str, object] = {}
    if annotations.audience is not None:
        payload["audience"] = list(annotations.audience[:16])
    if annotations.priority is not None and math.isfinite(annotations.priority):
        payload["priority"] = annotations.priority
    return payload


def _bounded_json(
    value: object,
    *,
    explicit_redactions: Sequence[str],
    redact: bool,
) -> object:
    nodes = 0

    def visit(item: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_MCP_JSON_NODES or depth > MAX_MCP_JSON_DEPTH:
            raise McpStdioError("mcp_json_too_complex")
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise McpStdioError("mcp_json_not_finite")
            return item
        if isinstance(item, str):
            return _safe_text(item, explicit_redactions=explicit_redactions) if redact else item
        if isinstance(item, list | tuple):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, Mapping):
            rendered: dict[str, object] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise McpStdioError("mcp_json_key_invalid")
                if key == "_meta":
                    continue
                safe_key = (
                    _safe_text(key, explicit_redactions=explicit_redactions) if redact else key
                )
                rendered[safe_key] = visit(child, depth + 1)
            return rendered
        raise McpStdioError("mcp_json_type_invalid")

    return visit(value, 0)


def _serialized_size(value: object) -> int:
    try:
        return len(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise McpStdioError("mcp_json_invalid") from None


def _safe_text(text: str, *, explicit_redactions: Sequence[str]) -> str:
    sanitized = "".join(
        character
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
        else "\N{REPLACEMENT CHARACTER}"
        for character in text
    ).replace("\x7f", "\N{REPLACEMENT CHARACTER}")
    return redact_sensitive_text(sanitized, explicit_values=explicit_redactions)


def _bounded_text(
    text: str,
    limit: int,
    *,
    explicit_redactions: Sequence[str],
) -> str:
    return _truncate_utf8(
        _safe_text(text, explicit_redactions=explicit_redactions),
        limit,
    )


def _truncate_utf8(text: str, limit: int) -> str:
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    marker = "\n… [truncated]".encode()
    retained = payload[: max(0, limit - len(marker))]
    while retained:
        try:
            return retained.decode("utf-8") + marker.decode()
        except UnicodeDecodeError:
            retained = retained[:-1]
    return marker[:limit].decode("utf-8", "ignore")


__all__ = [
    "MAX_MCP_SERVERS",
    "McpStdioError",
    "McpStdioServerConfig",
    "McpStdioTool",
    "McpStdioToolCollection",
]
