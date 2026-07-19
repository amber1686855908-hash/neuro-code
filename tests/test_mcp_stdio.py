from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types

import neuro_code.adapters.mcp_stdio as mcp_stdio
from neuro_code.adapters.mcp_stdio import (
    MAX_MCP_SERVERS,
    McpStdioError,
    McpStdioServerConfig,
    McpStdioToolCollection,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.errors import ToolError
from neuro_code.ports.tools import ToolContext

_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


class McpStdioToolCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collection = await McpStdioToolCollection.open(
            (
                McpStdioServerConfig(
                    name="fixture",
                    command=sys.executable,
                    args=(str(_FIXTURE),),
                    env=(("MCP_FIXTURE_SECRET", "fixture-secret-value"),),
                ),
            ),
            cwd=_FIXTURE.parent,
        )

    async def asyncTearDown(self) -> None:
        await self.collection.close()

    async def test_lists_and_calls_official_sdk_tools(self) -> None:
        tools = {tool.definition.name: tool for tool in self.collection.tools}

        self.assertEqual(
            set(tools),
            {
                "configured_secret",
                "disconnect",
                "echo",
                "empty_error",
                "rich_result",
                "wait_forever",
            },
        )
        self.assertTrue(all(tool.side_effecting for tool in tools.values()))
        result = await tools["echo"].execute(
            {"text": "hello from MCP"},
            ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "hello from MCP")

    async def test_explicit_environment_values_are_redacted_from_results(self) -> None:
        tools = {tool.definition.name: tool for tool in self.collection.tools}

        result = await tools["configured_secret"].execute(
            {},
            ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
        )

        self.assertNotIn("fixture-secret-value", result.content)
        self.assertIn("[REDACTED]", result.content)

    async def test_results_are_allowlisted_bounded_and_do_not_dereference_resources(
        self,
    ) -> None:
        tools = {tool.definition.name: tool for tool in self.collection.tools}

        rich = await tools["rich_result"].execute(
            {},
            ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
        )
        empty_error = await tools["empty_error"].execute(
            {},
            ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
        )

        self.assertIn("\ufffd text", rich.content)
        self.assertIn("<resource_link>", rich.content)
        self.assertIn("[MCP image content omitted]", rich.content)
        self.assertIn("[MCP audio content omitted]", rich.content)
        self.assertIn("[MCP embedded resource content omitted]", rich.content)
        self.assertIn("<structured_content>", rich.content)
        self.assertNotIn("fixture-secret-value", rich.content)
        self.assertNotIn("must-not-appear", rich.content)
        self.assertNotIn("embedded-secret", rich.content)
        self.assertTrue(empty_error.is_error)
        self.assertIn("no model-visible content", empty_error.content)

    async def test_indeterminate_call_failure_terminates_the_server(self) -> None:
        tools = {tool.definition.name: tool for tool in self.collection.tools}

        with self.assertRaisesRegex(Exception, "call_aborted"):
            await tools["disconnect"].execute(
                {},
                ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
            )
        with self.assertRaisesRegex(Exception, "not_active"):
            await tools["echo"].execute(
                {"text": "must not run"},
                ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
            )

    async def test_call_cancellation_terminates_server_and_fails_closed(self) -> None:
        tools = {tool.definition.name: tool for tool in self.collection.tools}
        call = asyncio.create_task(
            tools["wait_forever"].execute(
                {},
                ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
            )
        )
        await asyncio.sleep(0.1)
        call.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await call
        with self.assertRaisesRegex(Exception, "not_active"):
            await tools["echo"].execute(
                {"text": "must not run"},
                ToolContext(_FIXTURE.parent, sandbox_profile=SandboxProfile.OFF),
            )


class McpStdioValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_command_fails_without_publishing_tools(self) -> None:
        with self.assertRaisesRegex(McpStdioError, "initialization_failed"):
            await McpStdioToolCollection.open(
                (McpStdioServerConfig(name="missing", command="definitely-missing-mcp"),),
                cwd=_FIXTURE.parent,
            )

    async def test_malformed_oversized_and_unterminated_frames_fail_startup(self) -> None:
        for mode in ("malformed", "missing-newline", "oversized"):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(
                    McpStdioError,
                    "initialization_failed",
                ),
            ):
                await McpStdioToolCollection.open(
                    (
                        McpStdioServerConfig(
                            name=mode,
                            command=sys.executable,
                            args=(str(_FIXTURE), mode),
                        ),
                    ),
                    cwd=_FIXTURE.parent,
                )

    async def test_initialization_timeout_terminates_the_server(self) -> None:
        with (
            patch.object(mcp_stdio, "MCP_INITIALIZE_TIMEOUT_SECONDS", 0.05),
            patch.object(mcp_stdio, "MCP_CLOSE_TIMEOUT_SECONDS", 0.05),
            self.assertRaisesRegex(McpStdioError, "initialization_timeout"),
        ):
            await McpStdioToolCollection.open(
                (
                    McpStdioServerConfig(
                        name="hang",
                        command=sys.executable,
                        args=(str(_FIXTURE), "hang"),
                    ),
                ),
                cwd=_FIXTURE.parent,
            )

    async def test_collection_rejects_server_and_tool_count_or_name_collisions(self) -> None:
        configuration = McpStdioServerConfig(name="fixture", command=sys.executable)
        with self.assertRaisesRegex(McpStdioError, "too_many_mcp_servers"):
            await McpStdioToolCollection.open(
                (configuration,) * (MAX_MCP_SERVERS + 1),
                cwd=_FIXTURE.parent,
            )

        duplicate = mcp_types.Tool(
            name="duplicate",
            description="duplicate",
            inputSchema={"type": "object"},
        )
        with (
            patch.object(
                mcp_stdio._McpServerConnection,
                "start",
                new=AsyncMock(side_effect=((duplicate,), (duplicate,))),
            ),
            self.assertRaisesRegex(McpStdioError, "name_collision"),
        ):
            await McpStdioToolCollection.open(
                (
                    McpStdioServerConfig(name="one", command=sys.executable),
                    McpStdioServerConfig(name="two", command=sys.executable),
                ),
                cwd=_FIXTURE.parent,
            )

        with (
            patch.object(mcp_stdio, "MAX_MCP_TOTAL_TOOLS", 0),
            patch.object(
                mcp_stdio._McpServerConnection,
                "start",
                new=AsyncMock(return_value=(duplicate,)),
            ),
            self.assertRaisesRegex(McpStdioError, "too_many_mcp_tools"),
        ):
            await McpStdioToolCollection.open(
                (configuration,),
                cwd=_FIXTURE.parent,
            )


class McpStdioProjectionTests(unittest.TestCase):
    def test_json_validation_strips_meta_and_rejects_unsafe_shapes(self) -> None:
        projected = mcp_stdio._bounded_json(
            {
                "safe": [None, True, 1, 1.5, "token=sk-secretvalue"],
                "_meta": {"hidden": True},
            },
            explicit_redactions=(),
            redact=True,
        )

        self.assertNotIn("_meta", projected)
        self.assertNotIn("secretvalue", repr(projected))
        for value, reason in (
            (float("nan"), "not_finite"),
            ({1: "value"}, "key_invalid"),
            ({"value": object()}, "type_invalid"),
            ([[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]], "too_complex"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(McpStdioError, reason):
                mcp_stdio._bounded_json(
                    value,
                    explicit_redactions=(),
                    redact=False,
                )

    def test_argument_and_tool_definition_limits_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(ToolError, "finite JSON"):
            mcp_stdio._validated_arguments({"value": float("nan")})
        with self.assertRaisesRegex(ToolError, "byte limit"):
            mcp_stdio._validated_arguments(
                {"value": "x" * (mcp_stdio.MAX_MCP_TOOL_ARGUMENT_BYTES + 1)}
            )
        with self.assertRaisesRegex(McpStdioError, "name_invalid"):
            mcp_stdio._tool_definition(
                "fixture",
                mcp_types.Tool(
                    name="invalid name",
                    inputSchema={"type": "object"},
                ),
                explicit_redactions=(),
            )
        with (
            patch.object(mcp_stdio, "MAX_MCP_TOOL_SCHEMA_BYTES", 4),
            self.assertRaisesRegex(McpStdioError, "schema_too_large"),
        ):
            mcp_stdio._tool_definition(
                "fixture",
                mcp_types.Tool(
                    name="valid",
                    inputSchema={"type": "object"},
                ),
                explicit_redactions=(),
            )

    def test_text_truncation_preserves_utf8_boundaries(self) -> None:
        truncated = mcp_stdio._truncate_utf8("界" * 10, 17)

        self.assertLessEqual(len(truncated.encode("utf-8")), 17)
        self.assertIn("truncated", truncated)

    def test_windows_batch_wrappers_keep_atomic_job_spawn_and_quote_arguments(
        self,
    ) -> None:
        executable, arguments = mcp_stdio._windows_server_command(
            "C:\\Program Files\\nodejs\\npx.cmd",
            ("--yes", "package & value"),
            {"SYSTEMROOT": "C:\\Windows"},
        )

        self.assertEqual(executable, "C:\\Windows\\System32\\cmd.exe")
        self.assertEqual(arguments[:4], ("/d", "/s", "/v:off", "/c"))
        self.assertEqual(
            arguments[4],
            '""C:\\Program Files\\nodejs\\npx.cmd" "--yes" "package & value""',
        )
        self.assertEqual(
            mcp_stdio._windows_server_command(
                "C:\\Python\\python.exe",
                ("server.py",),
                {},
            ),
            ("C:\\Python\\python.exe", ("server.py",)),
        )
        for executable_name, reason in (
            ("server.ps1", "powershell_wrapper_unsupported"),
            ("bad%name.cmd", "batch_argument_unsupported"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(McpStdioError, reason):
                mcp_stdio._windows_server_command(
                    executable_name,
                    (),
                    {"SYSTEMROOT": "C:\\Windows"},
                )
        with self.assertRaisesRegex(McpStdioError, "processor_unavailable"):
            mcp_stdio._windows_server_command("server.cmd", (), {})
