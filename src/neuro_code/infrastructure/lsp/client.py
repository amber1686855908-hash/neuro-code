"""One bounded JSON-RPC/LSP session over an owned local process."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.ports.lsp import LspError, LspFailureKind, LspFailurePhase
from neuro_code.application.ports.sandbox import OwnedLocalProcess
from neuro_code.infrastructure.lsp.positions import PositionEncoding
from neuro_code.infrastructure.lsp.protocol import (
    LspFrameReader,
    LspProtocolError,
    write_message,
)
from neuro_code.infrastructure.lsp.uri import file_uri_from_path

LSP_STARTUP_TIMEOUT_SECONDS = 10.0
LSP_INITIALIZE_TIMEOUT_SECONDS = 20.0
LSP_REQUEST_TIMEOUT_SECONDS = 30.0
LSP_SHUTDOWN_TIMEOUT_SECONDS = 2.0
LSP_DIAGNOSTIC_WAIT_SECONDS = 1.0
MAX_LSP_PENDING_REQUESTS = 64
MAX_LSP_STDERR_BYTES = 16 * 1024
MAX_LSP_DIAGNOSTICS = 200
MAX_LSP_CONFIG_ITEMS = 64

NotificationHandler = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


class LspRemoteError(LspError):
    """A server returned a JSON-RPC error response."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            kind=LspFailureKind.PROTOCOL_ERROR,
            phase=LspFailurePhase.REQUEST,
        )


class LspClient:
    """Own protocol tasks and pending correlation for one language server."""

    def __init__(
        self,
        process: OwnedLocalProcess,
        *,
        workspace_root: Path,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self._process = process
        self._workspace_root = workspace_root
        self._notification_handler = notification_handler
        stdout = process.stdout
        if stdout is None:
            raise LspError(
                "LSP server did not expose protocol stdout",
                kind=LspFailureKind.PROTOCOL_ERROR,
                phase=LspFailurePhase.STARTUP,
            )
        self._reader = LspFrameReader(stdout)
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._next_request_id = 1
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_requested = False
        self._terminal_error: LspError | None = None
        self._stderr = bytearray()
        self._capabilities: dict[str, Any] = {}
        self._position_encoding = PositionEncoding.UTF16
        self._published_diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostic_events: dict[str, asyncio.Event] = {}

    @property
    def capabilities(self) -> Mapping[str, Any]:
        return self._capabilities

    @property
    def position_encoding(self) -> PositionEncoding:
        return self._position_encoding

    @property
    def stderr_text(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace")

    @property
    def terminal_error(self) -> LspError | None:
        return self._terminal_error

    @property
    def alive(self) -> bool:
        return not self._closed

    async def initialize(self) -> Mapping[str, Any]:
        self._reader_task = asyncio.create_task(self._reader_loop(), name="neuro-code-lsp-reader")
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(),
                name="neuro-code-lsp-stderr",
            )
        self._wait_task = asyncio.create_task(self._wait_loop(), name="neuro-code-lsp-wait")
        root_uri = file_uri_from_path(self._workspace_root)
        if root_uri is None:
            raise LspError(
                "LSP workspace root cannot be represented as a file URI",
                kind=LspFailureKind.INVALID_URI,
                phase=LspFailurePhase.INITIALIZATION,
            )
        params = {
            "processId": None,
            "clientInfo": {"name": "neuro-code", "version": "0.1"},
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                    "workspaceSymbol": {"resolveSupport": {"properties": []}},
                },
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "publishDiagnostics": {},
                    "diagnostic": {"dynamicRegistration": False},
                },
            },
            "trace": "off",
        }
        try:
            raw_result = await self.request(
                "initialize",
                params,
                budget_seconds=LSP_INITIALIZE_TIMEOUT_SECONDS,
            )
        except LspError as error:
            if error.kind is LspFailureKind.REQUEST_TIMEOUT:
                raise LspError(
                    "LSP initialize request timed out",
                    kind=LspFailureKind.INITIALIZATION_TIMEOUT,
                    phase=LspFailurePhase.INITIALIZATION,
                    retryable=True,
                ) from error
            raise
        if not isinstance(raw_result, dict):
            raise LspError(
                "LSP initialize response must be an object",
                kind=LspFailureKind.PROTOCOL_ERROR,
                phase=LspFailurePhase.INITIALIZATION,
            )
        result = raw_result
        capabilities = result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise LspError(
                "LSP initialize response omitted valid capabilities",
                kind=LspFailureKind.PROTOCOL_ERROR,
                phase=LspFailurePhase.INITIALIZATION,
            )
        self._capabilities = capabilities
        encoding = capabilities.get("positionEncoding")
        if encoding is None:
            legacy = capabilities.get("offsetEncoding")
            encoding = legacy[0] if isinstance(legacy, list) and legacy else legacy
        self._position_encoding = PositionEncoding.from_server_value(encoding)
        await self.notify("initialized", {})
        return result

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        budget_seconds: float = LSP_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        if self._closed:
            raise self._terminal_error or LspError(
                "LSP server session is closed",
                kind=LspFailureKind.SERVER_CRASH,
                phase=LspFailurePhase.REQUEST,
                retryable=True,
            )
        if len(self._pending) >= MAX_LSP_PENDING_REQUESTS:
            raise LspError(
                "LSP pending request limit exceeded",
                kind=LspFailureKind.REQUEST_TIMEOUT,
                phase=LspFailurePhase.REQUEST,
            )
        request_id = self._next_request_id
        self._next_request_id += 1
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = result
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            try:
                return await asyncio.wait_for(asyncio.shield(result), timeout=budget_seconds)
            except TimeoutError as error:
                await self._cancel_request(request_id)
                raise LspError(
                    f"LSP request timed out: {method}",
                    kind=LspFailureKind.REQUEST_TIMEOUT,
                    phase=LspFailurePhase.REQUEST,
                    retryable=True,
                ) from error
            except asyncio.CancelledError:
                await self._cancel_request(request_id)
                raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if self._closed and method != "exit":
            return
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params or {}),
            }
        )

    async def wait_for_diagnostics(self, uri: str, *, budget_seconds: float) -> None:
        event = self._diagnostic_events.setdefault(uri, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=budget_seconds)

    def diagnostics(self, uri: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._published_diagnostics.get(uri, ())]

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_requested:
                return
            self._close_requested = True
            if not self._closed and self._terminal_error is None:
                with contextlib.suppress(BaseException):
                    await self.request(
                        "shutdown",
                        {},
                        budget_seconds=LSP_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                with contextlib.suppress(BaseException):
                    await self.notify("exit", {})
            with contextlib.suppress(BaseException):
                await self._process.close_stdin()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._process.wait()),
                    timeout=LSP_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except BaseException:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(
                        self._process.terminate(grace_seconds=0.5),
                    )
            self._closed = True
            self._fail_pending(
                self._terminal_error
                or LspError(
                    "LSP server session closed",
                    kind=LspFailureKind.SHUTDOWN_TIMEOUT,
                    phase=LspFailurePhase.SHUTDOWN,
                )
            )
            for task in (self._reader_task, self._stderr_task, self._wait_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self._reader_task, self._stderr_task, self._wait_task) if task),
                return_exceptions=True,
            )

    async def _send(self, message: Mapping[str, Any]) -> None:
        async with self._write_lock:
            if self._closed and message.get("method") != "exit":
                raise self._terminal_error or LspError(
                    "LSP server session is closed",
                    kind=LspFailureKind.SERVER_CRASH,
                    phase=LspFailurePhase.NOTIFICATION,
                )
            try:
                await write_message(self._process, message)
            except (LspProtocolError, OSError) as error:
                failure = LspError(
                    f"LSP frame write failed: {type(error).__name__}",
                    kind=LspFailureKind.PROTOCOL_ERROR,
                    phase=LspFailurePhase.NOTIFICATION,
                )
                self._mark_failed(failure)
                raise failure from error

    async def _cancel_request(self, request_id: int | str) -> None:
        with contextlib.suppress(BaseException):
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": request_id},
                }
            )

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                message = await self._reader.read_message()
                if message is None:
                    if not self._close_requested:
                        self._mark_failed(
                            LspError(
                                "LSP server exited without a close handshake",
                                kind=LspFailureKind.SERVER_CRASH,
                                phase=LspFailurePhase.REQUEST,
                                retryable=True,
                            )
                        )
                    return
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except LspProtocolError as error:
            self._mark_failed(
                LspError(
                    f"LSP protocol framing failed: {error}",
                    kind=LspFailureKind.PROTOCOL_ERROR,
                    phase=LspFailurePhase.REQUEST,
                )
            )
        except (OSError, UnicodeError) as error:
            self._mark_failed(
                LspError(
                    f"LSP transport failed: {type(error).__name__}",
                    kind=LspFailureKind.SERVER_CRASH,
                    phase=LspFailurePhase.REQUEST,
                    retryable=True,
                )
            )

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message.get("id")
            if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                return
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                detail = error.get("message", "server error")
                future.set_exception(
                    LspRemoteError(f"LSP server error {code}: {str(detail)[:500]}")
                )
            else:
                result = message.get("result")
                future.set_result(result)
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            await self._handle_server_request(message.get("id"), method, message.get("params"))
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri")
            diagnostics = params.get("diagnostics")
            if isinstance(uri, str) and isinstance(diagnostics, list):
                bounded = [
                    item for item in diagnostics[:MAX_LSP_DIAGNOSTICS] if isinstance(item, dict)
                ]
                self._published_diagnostics[uri] = bounded
                self._diagnostic_events.setdefault(uri, asyncio.Event()).set()
        if self._notification_handler is not None:
            result = self._notification_handler(method, params)
            if result is not None:
                await result

    async def _handle_server_request(
        self,
        request_id: object,
        method: str,
        params: object,
    ) -> None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            return
        result: object
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            count = min(len(items), MAX_LSP_CONFIG_ITEMS) if isinstance(items, list) else 0
            result = [{} for _ in range(count)]
        elif method in {"client/registerCapability", "client/unregisterCapability"}:
            result = None
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "Neuro Code LSP mode is read-only"}
        elif method == "window/showMessageRequest":
            result = None
        elif method == "workspace/workspaceFolders":
            uri = file_uri_from_path(self._workspace_root)
            result = [] if uri is None else [{"uri": uri, "name": "workspace"}]
        else:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not supported by client"},
                }
            )
            return
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _stderr_loop(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > MAX_LSP_STDERR_BYTES:
                del self._stderr[: len(self._stderr) - MAX_LSP_STDERR_BYTES]

    async def _wait_loop(self) -> None:
        with contextlib.suppress(BaseException):
            await self._process.wait()
        if not self._close_requested and not self._closed:
            self._mark_failed(
                LspError(
                    "LSP server process exited unexpectedly",
                    kind=LspFailureKind.SERVER_CRASH,
                    phase=LspFailurePhase.REQUEST,
                    retryable=True,
                )
            )

    def _mark_failed(self, error: LspError) -> None:
        if self._terminal_error is None:
            self._terminal_error = error
        self._closed = True
        self._fail_pending(error)

    def _fail_pending(self, error: LspError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)


__all__ = [
    "LSP_DIAGNOSTIC_WAIT_SECONDS",
    "LSP_INITIALIZE_TIMEOUT_SECONDS",
    "LSP_REQUEST_TIMEOUT_SECONDS",
    "LSP_SHUTDOWN_TIMEOUT_SECONDS",
    "LSP_STARTUP_TIMEOUT_SECONDS",
    "MAX_LSP_DIAGNOSTICS",
    "MAX_LSP_PENDING_REQUESTS",
    "MAX_LSP_STDERR_BYTES",
    "LspClient",
    "LspRemoteError",
]
