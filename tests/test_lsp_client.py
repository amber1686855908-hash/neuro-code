from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from neuro_code.application.ports.lsp import LspError, LspFailureKind, LspFailurePhase
from neuro_code.infrastructure.lsp.client import LspClient, LspRemoteError
from neuro_code.infrastructure.lsp.positions import PositionEncoding
from neuro_code.infrastructure.lsp.protocol import LspProtocolError


class _MemoryStream:
    def __init__(self, data: bytes = b"", error: BaseException | None = None) -> None:
        self.data = bytearray(data)
        self.error = error

    async def read(self, n: int = -1, /) -> bytes:
        if self.error is not None:
            raise self.error
        if not self.data:
            await asyncio.sleep(0)
            return b""
        length = len(self.data) if n < 0 else min(n, len(self.data))
        result = bytes(self.data[:length])
        del self.data[:length]
        return result


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: _MemoryStream | None = None,
        stderr: _MemoryStream | None = None,
        wait_error: BaseException | None = None,
        write_error: BaseException | None = None,
    ) -> None:
        self.stdout = stdout or _MemoryStream()
        self.stderr = stderr
        self.wait_error = wait_error
        self.write_error = write_error
        self.writes: list[bytes] = []
        self.closed_stdin = False
        self.terminated = False

    async def write_stdin(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)

    async def close_stdin(self) -> None:
        self.closed_stdin = True

    async def wait(self) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        return 0

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        self.terminated = True


def _error(kind: LspFailureKind = LspFailureKind.PROTOCOL_ERROR) -> LspError:
    return LspError("fixture failure", kind=kind, phase=LspFailurePhase.REQUEST)


class LspClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_dispatch_and_server_request_paths(self) -> None:
        root = Path(tempfile.mkdtemp())
        process = _FakeProcess()
        client = LspClient(process, workspace_root=root)
        self.assertEqual(client.position_encoding, PositionEncoding.UTF16)
        self.assertTrue(client.alive)

        loop = asyncio.get_running_loop()
        remote = loop.create_future()
        client._pending[1] = remote
        await client._dispatch({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}})
        with self.assertRaises(LspRemoteError):
            await remote

        done = loop.create_future()
        done.set_result({})
        client._pending[2] = done
        await client._dispatch({"jsonrpc": "2.0", "id": 2, "result": {}})
        await client._dispatch({"jsonrpc": "2.0", "id": True, "result": {}})
        await client._dispatch({"jsonrpc": "2.0", "id": 99, "result": {}})
        await client._dispatch({"jsonrpc": "2.0", "method": 7})

        seen: list[str] = []

        async def handler(method: str, params: dict[str, object]) -> None:
            seen.append(method)

        notified = LspClient(_FakeProcess(), workspace_root=root, notification_handler=handler)
        await notified._dispatch(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": "file:///x", "diagnostics": [{"message": "ok"}]},
            }
        )
        await notified._dispatch({"jsonrpc": "2.0", "method": "telemetry/event", "params": 1})
        self.assertEqual(seen, ["textDocument/publishDiagnostics", "telemetry/event"])
        self.assertEqual(notified.diagnostics("file:///x"), [{"message": "ok"}])

        for method, params in (
            ("workspace/configuration", {"items": [{}, {}]}),
            ("client/registerCapability", {}),
            ("client/unregisterCapability", {}),
            ("workspace/applyEdit", {"edit": {}}),
            ("window/showMessageRequest", {}),
            ("workspace/workspaceFolders", {}),
            ("unknown/clientRequest", {}),
        ):
            await client._handle_server_request(10, method, params)
        await client._handle_server_request(True, "workspace/configuration", {})
        self.assertGreaterEqual(len(process.writes), 7)

    async def test_requests_cancel_and_transport_failures_are_typed(self) -> None:
        root = Path(tempfile.mkdtemp())
        process = _FakeProcess()
        client = LspClient(process, workspace_root=root)
        with self.assertRaises(LspError) as timed_out:
            await client.request("slow", budget_seconds=0.001)
        self.assertEqual(timed_out.exception.kind, LspFailureKind.REQUEST_TIMEOUT)
        self.assertTrue(any(b"cancelRequest" in frame for frame in process.writes))

        task = asyncio.create_task(client.request("cancelled", budget_seconds=10.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(sum(b"cancelRequest" in frame for frame in process.writes), 2)

        client._closed = True
        with self.assertRaises(LspError):
            await client.request("closed")

        failed_write = LspClient(
            _FakeProcess(write_error=OSError("write failed")),
            workspace_root=root,
        )
        with self.assertRaises(LspError) as write_error:
            await failed_write.notify("notification")
        self.assertEqual(write_error.exception.kind, LspFailureKind.PROTOCOL_ERROR)

        protocol_failure = LspClient(
            _FakeProcess(stdout=_MemoryStream(error=LspProtocolError("bad frame"))),
            workspace_root=root,
        )
        await protocol_failure._reader_loop()
        self.assertEqual(protocol_failure.terminal_error.kind, LspFailureKind.PROTOCOL_ERROR)  # type: ignore[union-attr]

        transport_failure = LspClient(
            _FakeProcess(stdout=_MemoryStream(error=OSError("read failed"))),
            workspace_root=root,
        )
        await transport_failure._reader_loop()
        self.assertEqual(transport_failure.terminal_error.kind, LspFailureKind.SERVER_CRASH)  # type: ignore[union-attr]

    async def test_initialize_validation_and_close_fallbacks(self) -> None:
        root = Path(tempfile.mkdtemp())
        client = LspClient(_FakeProcess(), workspace_root=root)
        client.request = AsyncMock(  # type: ignore[method-assign]
            return_value={"capabilities": {"offsetEncoding": ["utf-8"]}}
        )
        await client.initialize()
        self.assertEqual(client.position_encoding, PositionEncoding.UTF8)
        await client.close()
        self.assertTrue(client._closed)
        await client.close()

        invalid_result = LspClient(_FakeProcess(), workspace_root=root)
        invalid_result.request = AsyncMock(return_value=[])
        with self.assertRaises(LspError):
            await invalid_result.initialize()
        await invalid_result.close()

        invalid_capabilities = LspClient(_FakeProcess(), workspace_root=root)
        invalid_capabilities.request = AsyncMock(return_value={"capabilities": []})
        with self.assertRaises(LspError):
            await invalid_capabilities.initialize()
        await invalid_capabilities.close()

        init_timeout = LspClient(_FakeProcess(), workspace_root=root)
        init_timeout.request = AsyncMock(side_effect=_error(LspFailureKind.REQUEST_TIMEOUT))
        with self.assertRaises(LspError) as timed_out:
            await init_timeout.initialize()
        self.assertEqual(timed_out.exception.kind, LspFailureKind.INITIALIZATION_TIMEOUT)
        await init_timeout.close()

        terminating = _FakeProcess(wait_error=TimeoutError("still alive"))
        closing = LspClient(terminating, workspace_root=root)
        closing._closed = True
        closing._terminal_error = _error(LspFailureKind.SERVER_CRASH)
        await closing.close()
        self.assertTrue(terminating.terminated)
