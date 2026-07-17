from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping
from typing import Any

from pygrok_build.adapters.process_tree import ProcessTree
from pygrok_build.domain.tools import ToolDefinition, ToolResult
from pygrok_build.errors import ToolError
from pygrok_build.ports.tools import ToolContext


class BashTool:
    definition = ToolDefinition(
        name="bash",
        description="Run a shell command in the current workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, byte_limit: int) -> tuple[bytes, bool]:
        captured = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = max(0, byte_limit - len(captured))
            captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(captured), truncated

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        command = arguments.get("command")
        timeout = arguments.get("timeout_seconds", context.command_timeout_seconds)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ToolError("command must be a non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ToolError("timeout_seconds must be positive")
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        if context.termination_grace_seconds <= 0:
            raise ToolError("termination_grace_seconds must be positive")

        env = dict(os.environ)
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        tree = await ProcessTree.spawn_shell(
            command,
            cwd=context.cwd,
            env=env,
        )
        process = tree.process
        assert process.stdout is not None
        assert process.stderr is not None
        execution = asyncio.gather(
            self._read_limited(process.stdout, context.output_byte_limit),
            self._read_limited(process.stderr, context.output_byte_limit),
            process.wait(),
        )
        try:
            (stdout, stdout_truncated), (stderr, stderr_truncated), _ = await asyncio.wait_for(
                asyncio.shield(execution), float(timeout)
            )
        except TimeoutError as error:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise ToolError(f"command timed out after {timeout:g} seconds") from error
        except asyncio.CancelledError:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise
        except Exception:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise

        combined = stdout + (b"\n" if stdout and stderr else b"") + stderr
        combined_truncated = len(combined) > context.output_byte_limit
        truncated = stdout_truncated or stderr_truncated or combined_truncated
        if combined_truncated:
            combined = combined[: context.output_byte_limit]
        content = combined.decode("utf-8", "replace")
        if truncated:
            content += "\n[output truncated]"
        return ToolResult(
            content,
            is_error=process.returncode != 0,
            metadata={"exit_code": process.returncode, "truncated": truncated},
        )
