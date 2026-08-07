from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from acp.exceptions import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    HttpMcpServer,
    Implementation,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)
from acp.stdio import spawn_agent_process

from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.workspace.paths import workspaces_match

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MCP_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "mcp_stdio_server.py"


class ProviderServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.cancel_request_received = threading.Event()
        self.release_cancel_request = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                fixture.requests.append(body)
                messages = body.get("messages", [])
                serialized = json.dumps(messages)
                if "WAIT_FOR_CANCEL" in serialized:
                    fixture.cancel_request_received.set()
                    fixture.release_cancel_request.wait(timeout=10)
                    self._send_sse("late response")
                    return
                if "USE_MCP_CANCEL" in serialized:
                    if not any(message.get("role") == "tool" for message in messages):
                        self._send_mcp_wait_call()
                    else:
                        self._send_sse("must not complete")
                    return
                if "USE_MCP" in serialized:
                    if not any(message.get("role") == "tool" for message in messages):
                        self._send_mcp_tool_call()
                    else:
                        self._send_sse("mcp completed")
                    return
                if not any(message.get("role") == "tool" for message in messages):
                    self._send_tool_calls()
                    return
                self._send_sse("tools completed")

            def _send_tool_calls(self) -> None:
                calls = [
                    (
                        "call-read",
                        "read_file",
                        {"path": "input.txt"},
                    ),
                    (
                        "call-edit",
                        "search_replace",
                        {
                            "path": "editable.txt",
                            "old": "before",
                            "new": "after",
                        },
                    ),
                    (
                        "call-command",
                        "bash",
                        {"command": "printf command-ok > command.txt"},
                    ),
                ]
                tool_calls = [
                    {
                        "index": index,
                        "id": identifier,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                    for index, (identifier, name, arguments) in enumerate(calls)
                ]
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {"tool_calls": tool_calls},
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    },
                ]
                self._write_sse(chunks)

            def _send_mcp_tool_call(self) -> None:
                self._write_sse(
                    [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call-mcp-echo",
                                                "type": "function",
                                                "function": {
                                                    "name": "echo",
                                                    "arguments": json.dumps(
                                                        {"text": "hello through MCP"}
                                                    ),
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        }
                    ]
                )

            def _send_mcp_wait_call(self) -> None:
                self._write_sse(
                    [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call-mcp-wait",
                                                "type": "function",
                                                "function": {
                                                    "name": "wait_forever",
                                                    "arguments": "{}",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        }
                    ]
                )

            def _send_sse(self, text: str) -> None:
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {"content": text},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                    },
                ]
                self._write_sse(chunks)

            def _write_sse(self, chunks: list[dict[str, Any]]) -> None:
                payload = (
                    "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                    + "data: [DONE]\n\n"
                )
                encoded = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.release_cancel_request.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class E2eClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.permission_requests: list[tuple[str, ToolCallUpdate, list[PermissionOption]]] = []
        self._update_event = asyncio.Event()

    async def session_update(
        self,
        session_id: str,
        update: object,
        **_kwargs: Any,
    ) -> None:
        self.updates.append((session_id, update))
        self._update_event.set()

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **_kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((session_id, tool_call, options))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow_once")
        )

    async def wait_for_updates(self, count: int) -> None:
        async def wait() -> None:
            while len(self.updates) < count:
                self._update_event.clear()
                if len(self.updates) < count:
                    await self._update_event.wait()

        await asyncio.wait_for(wait(), timeout=5)


class AcpSubprocessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = ProviderServer()
        self.server.start()

    def tearDown(self) -> None:
        self.server.close()

    def _configure_workspace(self, root: Path) -> tuple[Path, dict[str, str]]:
        state = root / "state"
        state.mkdir()
        (state / "config.toml").write_text(
            f"""
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "http://127.0.0.1:{self.server.port}/v1"
auth = "proxy-managed"
proxy_mode = "direct"
context_window_tokens = 32000
""",
            encoding="utf-8",
        )
        return state, {"NEURO_CODE_HOME": str(state)}

    async def test_official_client_drives_tools_close_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, environment = self._configure_workspace(root)
            (root / "input.txt").write_text("read me", encoding="utf-8")
            (root / "editable.txt").write_text("before", encoding="utf-8")
            client = E2eClient()

            async with spawn_agent_process(
                cast(Client, client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, process):
                initialized = await connection.initialize(
                    1,
                    ClientCapabilities(terminal=True),
                    Implementation(name="e2e-client", version="1.0"),
                )
                capabilities = initialized.agent_capabilities.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                    exclude_unset=True,
                )
                self.assertEqual(
                    capabilities,
                    {
                        "loadSession": True,
                        "mcpCapabilities": {"http": True, "sse": True},
                        "sessionCapabilities": {
                            "list": {},
                            "delete": {},
                            "fork": {},
                            "resume": {},
                            "close": {},
                        },
                    },
                )

                with self.assertRaises(RequestError):
                    await connection.new_session(
                        str(root),
                        additional_directories=[str(root)],
                    )
                with self.assertRaises(RequestError):
                    await connection.new_session(
                        str(root),
                        mcp_servers=[
                            HttpMcpServer(
                                name="unsupported",
                                type="http",
                                url="https://example.invalid/mcp",
                                headers=[],
                            )
                        ],
                    )

                created = await connection.new_session(str(root))
                response = await connection.prompt(
                    created.session_id,
                    [
                        TextContentBlock(type="text", text="Use the tools."),
                        ResourceContentBlock.model_validate(
                            {
                                "type": "resource_link",
                                "uri": "https://example.invalid/reference",
                                "name": "reference",
                                "_meta": {"secret": "must-not-reach-model"},
                            }
                        ),
                    ],
                )
                self.assertEqual(response.stop_reason, "end_turn")
                self.assertIsNone(await connection.cancel("unknown-session"))
                self.assertIsNotNone(await connection.close_session(created.session_id))
                with self.assertRaises(RequestError):
                    await connection.close_session(created.session_id)
                self.assertIsNone(process.returncode)

            self.assertEqual((root / "editable.txt").read_text(encoding="utf-8"), "after")
            self.assertEqual(
                (root / "command.txt").read_text(encoding="utf-8"),
                "command-ok",
            )
            self.assertEqual(len(client.permission_requests), 2)
            first_model_prompt = self.server.requests[0]["messages"][-1]["content"]
            self.assertLess(
                first_model_prompt.index("Use the tools."),
                first_model_prompt.index("resource_link"),
            )
            self.assertNotIn("must-not-reach-model", first_model_prompt)
            self.assertTrue(
                all(session_id == created.session_id for session_id, _ in client.updates)
            )
            chunks = [
                update for _, update in client.updates if isinstance(update, AgentMessageChunk)
            ]
            self.assertEqual("".join(chunk.content.text for chunk in chunks), "tools completed")
            tool_updates = [
                update
                for _, update in client.updates
                if isinstance(update, (ToolCallStart, ToolCallProgress))
            ]
            states: dict[str, list[str | None]] = {}
            for update in tool_updates:
                states.setdefault(update.tool_call_id, []).append(update.status)
            self.assertEqual(
                states,
                {
                    "call-read": ["pending", "in_progress", "completed"],
                    "call-edit": ["pending", "in_progress", "completed"],
                    "call-command": ["pending", "in_progress", "completed"],
                },
            )

            store = SqliteSessionStore(state / "sessions.db")
            await store.initialize()
            persisted = await store.list_sessions()
            self.assertEqual(len(persisted), 1)
            self.assertNotEqual(persisted[0].id, created.session_id)
            self.assertEqual(
                await store.resolve_session_alias("acp-v1", created.session_id),
                persisted[0].id,
            )

            resumed_client = E2eClient()
            async with spawn_agent_process(
                cast(Client, resumed_client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, _):
                initialized = await connection.initialize(1)
                self.assertTrue(initialized.agent_capabilities.load_session)
                self.assertIsNotNone(initialized.agent_capabilities.session_capabilities)
                self.assertIsNotNone(initialized.agent_capabilities.session_capabilities.list)
                self.assertIsNotNone(initialized.agent_capabilities.session_capabilities.delete)
                self.assertIsNotNone(initialized.agent_capabilities.session_capabilities.fork)
                self.assertIsNotNone(initialized.agent_capabilities.session_capabilities.resume)
                listed = await connection.list_sessions()
                self.assertEqual(len(listed.sessions), 1)
                self.assertEqual(listed.sessions[0].session_id, created.session_id)
                self.assertTrue(workspaces_match(listed.sessions[0].cwd, root))
                self.assertTrue(listed.sessions[0].title.startswith("Use the tools."))
                self.assertIsNotNone(listed.sessions[0].updated_at)
                self.assertIsNone(listed.sessions[0].additional_directories)
                self.assertIsNone(listed.sessions[0].field_meta)
                self.assertIsNone(listed.next_cursor)
                filtered = await connection.list_sessions(str(root))
                self.assertEqual(
                    [session.session_id for session in filtered.sessions],
                    [created.session_id],
                )
                await connection.load_session(
                    str(root),
                    listed.sessions[0].session_id,
                    mcp_servers=[],
                )
                await resumed_client.wait_for_updates(8)
                replay = [update for _, update in resumed_client.updates]
                self.assertIsInstance(replay[0], UserMessageChunk)
                self.assertTrue(replay[0].content.text.startswith("Use the tools."))
                self.assertEqual(
                    [update.status for update in replay if isinstance(update, ToolCallStart)],
                    ["pending", "pending", "pending"],
                )
                self.assertEqual(
                    [update.status for update in replay if isinstance(update, ToolCallProgress)],
                    ["completed", "completed", "completed"],
                )
                self.assertIsInstance(replay[-1], AgentMessageChunk)
                self.assertEqual(replay[-1].content.text, "tools completed")
                self.assertNotIn("must-not-reach-model", repr(replay))
                self.assertTrue(
                    all(
                        session_id == created.session_id for session_id, _ in resumed_client.updates
                    )
                )

                resumed = await connection.prompt(
                    created.session_id,
                    [TextContentBlock(type="text", text="Continue loaded session.")],
                )
                self.assertEqual(resumed.stop_reason, "end_turn")
                await connection.close_session(created.session_id)

            persisted_after_load = await store.list_sessions()
            self.assertEqual(len(persisted_after_load), 1)
            resumed_model_messages = self.server.requests[-1]["messages"]
            self.assertTrue(
                any(
                    message.get("content") == "Continue loaded session."
                    for message in resumed_model_messages
                )
            )
            self.assertTrue(
                any(message.get("role") == "tool" for message in resumed_model_messages)
            )

            lifecycle_client = E2eClient()
            async with spawn_agent_process(
                cast(Client, lifecycle_client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, _):
                await connection.initialize(1)
                await connection.resume_session(
                    created.session_id,
                    str(root),
                    mcp_servers=[],
                )
                self.assertEqual(lifecycle_client.updates, [])
                await connection.close_session(created.session_id)

                forked = await connection.fork_session(
                    created.session_id,
                    str(root),
                    mcp_servers=[],
                )
                self.assertNotEqual(forked.session_id, created.session_id)
                self.assertEqual(lifecycle_client.updates, [])
                await connection.close_session(forked.session_id)

                listed_after_fork = await connection.list_sessions()
                self.assertEqual(
                    {session.session_id for session in listed_after_fork.sessions},
                    {created.session_id, forked.session_id},
                )

            persisted_after_fork = await store.list_sessions()
            self.assertEqual(len(persisted_after_fork), 2)
            forked_internal_id = await store.resolve_session_alias(
                "acp-v1",
                forked.session_id,
            )
            self.assertEqual(
                await store.load_session_items(forked_internal_id),
                await store.load_session_items(persisted[0].id),
            )
            self.assertEqual(await store.load_events(forked_internal_id), [])

    async def test_official_cancel_notification_returns_cancelled_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, environment = self._configure_workspace(root)
            client = E2eClient()

            async with spawn_agent_process(
                cast(Client, client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, _):
                await connection.initialize(1)
                created = await connection.new_session(str(root))
                prompt_task = asyncio.create_task(
                    connection.prompt(
                        created.session_id,
                        [TextContentBlock(type="text", text="WAIT_FOR_CANCEL")],
                    )
                )
                received = await asyncio.to_thread(
                    self.server.cancel_request_received.wait,
                    5,
                )
                self.assertTrue(received)
                await connection.cancel(created.session_id)
                response = await asyncio.wait_for(prompt_task, timeout=5)
                self.assertEqual(response.stop_reason, "cancelled")
                self.server.release_cancel_request.set()

    async def test_official_client_drives_session_owned_stdio_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, environment = self._configure_workspace(root)
            client = E2eClient()

            async with spawn_agent_process(
                cast(Client, client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, process):
                initialized = await connection.initialize(1)
                declared = initialized.agent_capabilities.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                    exclude_unset=True,
                )
                self.assertEqual(declared["mcpCapabilities"], {"http": True, "sse": True})
                created = await connection.new_session(
                    str(root),
                    mcp_servers=[
                        McpServerStdio.model_validate(
                            {
                                "name": "fixture",
                                "command": sys.executable,
                                "args": [str(MCP_FIXTURE)],
                                "env": [
                                    {
                                        "name": "MCP_FIXTURE_SECRET",
                                        "value": "fixture-secret-value",
                                    }
                                ],
                                "_meta": {"ignored": "must-not-be-forwarded"},
                            }
                        )
                    ],
                )
                response = await connection.prompt(
                    created.session_id,
                    [TextContentBlock(type="text", text="USE_MCP")],
                )

                self.assertEqual(response.stop_reason, "end_turn")
                self.assertEqual(len(client.permission_requests), 1)
                self.assertEqual(
                    client.permission_requests[0][1].tool_call_id,
                    "call-mcp-echo",
                )
                states = [
                    update.status
                    for _, update in client.updates
                    if isinstance(update, (ToolCallStart, ToolCallProgress))
                    and update.tool_call_id == "call-mcp-echo"
                ]
                self.assertEqual(states, ["pending", "in_progress", "completed"])
                self.assertNotIn("fixture-secret-value", repr(client.updates))
                self.assertNotIn("must-not-be-forwarded", repr(client.updates))
                self.assertEqual(
                    "".join(
                        update.content.text
                        for _, update in client.updates
                        if isinstance(update, AgentMessageChunk)
                    ),
                    "mcp completed",
                )
                await connection.close_session(created.session_id)
                self.assertIsNone(process.returncode)

    async def test_mcp_call_cancel_terminates_before_cancelled_prompt_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, environment = self._configure_workspace(root)
            client = E2eClient()

            async with spawn_agent_process(
                cast(Client, client),
                sys.executable,
                "-m",
                "neuro_code",
                "acp",
                "--cwd",
                str(root),
                env=environment,
                cwd=REPOSITORY_ROOT,
            ) as (connection, _):
                await connection.initialize(1)
                created = await connection.new_session(
                    str(root),
                    mcp_servers=[
                        McpServerStdio(
                            name="fixture",
                            command=sys.executable,
                            args=[str(MCP_FIXTURE)],
                            env=[],
                        )
                    ],
                )
                prompt_task = asyncio.create_task(
                    connection.prompt(
                        created.session_id,
                        [TextContentBlock(type="text", text="USE_MCP_CANCEL")],
                    )
                )
                await client.wait_for_updates(2)
                await connection.cancel(created.session_id)
                response = await asyncio.wait_for(prompt_task, timeout=8)

                self.assertEqual(response.stop_reason, "cancelled")
                states = [
                    update.status
                    for _, update in client.updates
                    if isinstance(update, (ToolCallStart, ToolCallProgress))
                    and update.tool_call_id == "call-mcp-wait"
                ]
                self.assertEqual(states, ["pending", "in_progress", "failed"])
                await connection.close_session(created.session_id)


if __name__ == "__main__":
    unittest.main()
