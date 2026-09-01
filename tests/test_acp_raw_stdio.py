from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any

from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import SessionError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RawAcpProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._readline_task: asyncio.Task[bytes] | None = None

    @classmethod
    async def start(cls, root: Path) -> RawAcpProcess:
        state = root / "state"
        state.mkdir()
        (state / "config.toml").write_text(
            """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "http://127.0.0.1:9/v1"
auth = "proxy-managed"
proxy_mode = "direct"
""",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "neuro_code",
            "acp",
            "--cwd",
            str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=REPOSITORY_ROOT,
            env=environment,
        )
        return cls(process)

    async def send(self, payload: bytes) -> None:
        self.stdin.write(payload)
        await self.stdin.drain()

    def _next_line_task(self) -> asyncio.Task[bytes]:
        if self._readline_task is None:
            self._readline_task = asyncio.create_task(self.stdout.readline())
        return self._readline_task

    async def _next_line(self, *, wait_seconds: float) -> bytes:
        task = self._next_line_task()
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        finally:
            if task.done() and self._readline_task is task:
                self._readline_task = None

    async def response(self, *, wait_seconds: float = 5.0) -> dict[str, Any]:
        line = await self._next_line(wait_seconds=wait_seconds)
        if not line:
            raise AssertionError("ACP process closed stdout before returning a response")
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise AssertionError("ACP stdout frame was not a JSON object")
        return parsed

    async def expect_no_response(self, *, wait_seconds: float = 0.15) -> None:
        try:
            line = await self._next_line(wait_seconds=wait_seconds)
        except TimeoutError:
            return
        if not line:
            raise AssertionError("ACP process closed stdout before returning a response")
        raise AssertionError("ACP process unexpectedly wrote a stdout frame")

    async def initialize(self, request_id: int = 1) -> dict[str, Any]:
        await self.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        return await self.response()

    async def close(self) -> str:
        self.stdin.close()
        await self.stdin.wait_closed()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except TimeoutError:
            self.process.terminate()
            await self.process.wait()
        if self._readline_task is not None and not self._readline_task.done():
            self._readline_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._readline_task
        self._readline_task = None
        return (await self.stderr.read()).decode("utf-8", "replace")


class RawStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_json_and_physical_newline_do_not_pollute_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = await RawAcpProcess.start(Path(directory))
            try:
                await process.send(b'{"jsonrpc":"2.0","id":1,\n')
                await process.expect_no_response()
                await process.send(b'"method":"initialize"}\n')
                await process.expect_no_response()
                await process.send(b"not-json\n")
                await process.expect_no_response()
                response = await process.initialize(2)
            finally:
                stderr = await process.close()

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 2)
        self.assertNotIn("not-json", json.dumps(response))
        self.assertIn("Error parsing JSON-RPC message", stderr)

    async def test_missing_newline_is_not_dispatched_until_frame_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = await RawAcpProcess.start(Path(directory))
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                await process.send(payload)
                await process.expect_no_response()
                await process.send(b"\n")
                response = await process.response()
            finally:
                await process.close()

        self.assertEqual(response["id"], 1)
        self.assertIn("result", response)

    async def test_unknown_method_invalid_params_and_cancel_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = await RawAcpProcess.start(Path(directory))
            try:
                await process.initialize()
                await process.send(
                    b'{"jsonrpc":"2.0","id":2,"method":"unknown/method","params":{}}\n'
                )
                unknown = await process.response()
                await process.send(b'{"jsonrpc":"2.0","id":3,"method":"session/new","params":{}}\n')
                invalid = await process.response()
                await process.send(
                    b'{"jsonrpc":"2.0","method":"session/cancel",'
                    b'"params":{"sessionId":"unknown"}}\n'
                )
                await process.expect_no_response()
                await process.send(
                    b'{"jsonrpc":"2.0","id":4,"method":"unknown/after-cancel","params":{}}\n'
                )
                after_cancel = await process.response()
            finally:
                await process.close()

        self.assertEqual(unknown["error"]["code"], -32601)
        self.assertEqual(invalid["error"]["code"], -32602)
        self.assertEqual(after_cancel["id"], 4)
        self.assertEqual(after_cancel["error"]["code"], -32601)

    async def test_official_sdk_normalizes_wrong_jsonrpc_version_on_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = await RawAcpProcess.start(Path(directory))
            try:
                await process.send(
                    b'{"jsonrpc":"1.0","id":7,"method":"initialize",'
                    b'"params":{"protocolVersion":1}}\n'
                )
                response = await process.response()
            finally:
                await process.close()

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 7)
        self.assertIn("result", response)

    async def test_session_delete_route_removes_durable_workspace_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = await RawAcpProcess.start(root)
            try:
                await process.initialize()
                store = SqliteSessionStore(root / "state" / "sessions.db")
                await store.initialize()
                internal_session_id = await store.create_session(
                    str(root),
                    "fixture",
                    "fixture-model",
                )
                await store.bind_session_alias(
                    "acp-v1",
                    "acp-delete",
                    internal_session_id,
                )
                await process.send(
                    b'{"jsonrpc":"2.0","id":2,"method":"session/delete",'
                    b'"params":{"sessionId":"acp-delete"}}\n'
                )
                response = await process.response()
            finally:
                await process.close()

            self.assertEqual(response["result"], {})
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.get_session(internal_session_id)


if __name__ == "__main__":
    unittest.main()
