from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pygrok_build.cli import _normalize_rule, main
from pygrok_build.domain.messages import Message
from pygrok_build.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
from pygrok_build.domain.tools import ToolDefinition


class CliProvider:
    provider_name = "cli-fixture"
    model_name = "fixture-model"

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        yield ModelTextDelta("fixture response")
        yield ModelCompleted("stop", 2, 3)


class CliTests(unittest.TestCase):
    def test_native_bash_permission_patterns_are_normalized(self) -> None:
        self.assertEqual(_normalize_rule("Bash"), "bash:*")
        self.assertEqual(_normalize_rule("Bash(*)"), "bash:*")
        self.assertEqual(_normalize_rule("Bash(git:*)"), "bash:git*")
        self.assertEqual(_normalize_rule("Bash(git status)"), "bash:git status")

    def test_version_json_is_machine_readable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("version", "--json"))
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["name"], "pygrok-build")
        self.assertEqual(len(payload["source_oracle_commit"]), 40)

    def test_inspect_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {"PYGROK_HOME": str(root / "state"), "XAI_API_KEY": "never-print-this"},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(("inspect", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertNotIn("never-print-this", output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["provider"]["credential_configured"])

    def test_plain_inspect_version_and_completion_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arguments, expected in (
                (("version",), "pygrok-build"),
                (("inspect", "--cwd", str(root)), "credential_configured: false"),
                (("completions", "bash"), "complete -F"),
                (("completions", "zsh"), "#compdef"),
                (("completions", "fish"), "complete -c"),
                (("completions", "powershell"), "Register-ArgumentCompleter"),
            ):
                output = io.StringIO()
                with (
                    patch.dict("os.environ", {"PYGROK_HOME": str(root / "state")}, clear=True),
                    redirect_stdout(output),
                ):
                    exit_code = main(arguments)
                self.assertEqual(exit_code, 0)
                self.assertIn(expected, output.getvalue())

    def test_headless_plain_json_and_jsonl_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output_format in ("plain", "json", "jsonl"):
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "PYGROK_HOME": str(root / output_format),
                            "XAI_API_KEY": "fixture-key",
                        },
                        clear=True,
                    ),
                    patch("pygrok_build.cli.create_provider", return_value=CliProvider()),
                    redirect_stdout(output),
                ):
                    exit_code = main(
                        (
                            "-p",
                            "hello",
                            "--cwd",
                            str(root),
                            "--output-format",
                            output_format,
                        )
                    )
                self.assertEqual(exit_code, 0)
                self.assertIn("fixture response", output.getvalue())
                if output_format == "json":
                    self.assertEqual(json.loads(output.getvalue())["steps"], 1)
                if output_format == "jsonl":
                    records = [json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(records[-1]["kind"], "turn_completed")

    def test_missing_prompt_returns_configuration_error(self) -> None:
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            exit_code = main(())
        self.assertEqual(exit_code, 2)
        self.assertIn("TUI is not implemented", errors.getvalue())

    def test_resume_list_and_export_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "PYGROK_HOME": str(root / "state"),
                "XAI_API_KEY": "fixture-key",
            }

            def run(arguments: tuple[str, ...]) -> tuple[int, str]:
                output = io.StringIO()
                with (
                    patch.dict("os.environ", environment, clear=True),
                    patch("pygrok_build.cli.create_provider", return_value=CliProvider()),
                    redirect_stdout(output),
                ):
                    return main(arguments), output.getvalue()

            exit_code, first_output = run(
                ("-p", "first", "--cwd", str(root), "--output-format", "json")
            )
            self.assertEqual(exit_code, 0)
            session_id = json.loads(first_output)["session_id"]

            exit_code, second_output = run(
                (
                    "-p",
                    "second",
                    "--cwd",
                    str(root),
                    "--resume",
                    session_id,
                    "--output-format",
                    "json",
                )
            )
            self.assertEqual(exit_code, 0)
            resumed = json.loads(second_output)
            self.assertEqual(resumed["session_id"], session_id)
            self.assertGreater(resumed["events"][0]["sequence"], 1)

            exit_code, list_output = run(("sessions", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(list_output)[0]["id"], session_id)

            exit_code, markdown = run(("export", session_id, "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertIn("## User\n\nfirst", markdown)
            self.assertIn("## User\n\nsecond", markdown)

            export_path = root / "exports" / "session.json"
            exit_code, export_output = run(
                (
                    "export",
                    session_id,
                    "--cwd",
                    str(root),
                    "--format",
                    "json",
                    "--output",
                    str(export_path),
                )
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(export_output.strip(), str(export_path.resolve()))
            exported = json.loads(export_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["schema_version"], 2)
            self.assertEqual(exported["session"]["id"], session_id)
            self.assertEqual(exported["conversation_items"], exported["messages"])

    def test_import_rust_session_is_available_to_list_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rust-session"
            source.mkdir()
            (source / "summary.json").write_text(
                json.dumps(
                    {
                        "info": {"id": "rust-cli-id", "cwd": str(root)},
                        "created_at": "2026-07-01T10:20:30Z",
                        "updated_at": "2026-07-02T11:22:33Z",
                        "current_model_id": "grok-4.5",
                        "chat_format_version": 1,
                    }
                ),
                encoding="utf-8",
            )
            (source / "chat_history.jsonl").write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "user",
                                "content": [
                                    {"type": "text", "text": "legacy prompt"},
                                    {
                                        "type": "image",
                                        "url": "data:image/png;base64,fixture",
                                    },
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "type": "reasoning",
                                "id": "reasoning-cli",
                                "summary": [{"type": "summary_text", "text": "careful thought"}],
                            }
                        ),
                        json.dumps(
                            {
                                "type": "backend_tool_call",
                                "kind": {
                                    "tool_type": "web_search",
                                    "id": "web-cli",
                                    "action": {"type": "search", "query": "fixture query"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "content": "legacy response",
                                "raw_output": [
                                    {
                                        "type": "reasoning",
                                        "id": "reasoning-recovered",
                                        "summary": [
                                            {
                                                "type": "summary_text",
                                                "text": "recovered thought",
                                            }
                                        ],
                                    },
                                    {
                                        "type": "web_search_call",
                                        "id": "web-cli",
                                        "status": "completed",
                                        "action": {
                                            "type": "search",
                                            "query": "duplicate query",
                                        },
                                    },
                                    {
                                        "type": "message",
                                        "id": "message-cli",
                                        "status": "completed",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                ],
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            source_before = {
                path.name: path.read_bytes()
                for path in (source / "summary.json", source / "chat_history.jsonl")
            }
            environment = {"PYGROK_HOME": str(root / "state")}

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("import-session", str(source), "--json", "--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["session"]["id"], "rust-cli-id")
            self.assertEqual(payload["session"]["provider"], "grok-build-import")
            self.assertEqual(payload["imported_messages"], 2)
            self.assertEqual(payload["preserved_context_records"], 3)
            self.assertEqual(payload["recovered_context_records"], 1)
            self.assertEqual(payload["deduplicated_context_records"], 1)
            self.assertEqual(payload["invalid_embedded_records"], 0)
            self.assertEqual(payload["unsupported_embedded_records"], 0)
            self.assertEqual(payload["preserved_images"], 1)

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("export", "rust-cli-id", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            markdown = output.getvalue()
            self.assertIn("legacy prompt", markdown)
            self.assertIn("image content preserved in session", markdown)
            self.assertIn("## Reasoning\n\ncareful thought", markdown)
            self.assertIn("## Reasoning\n\nrecovered thought", markdown)
            self.assertIn("legacy response", markdown)
            self.assertIn("## Backend tool call", markdown)
            self.assertIn("fixture query", markdown)

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("export", "rust-cli-id", "--format", "json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            exported = json.loads(output.getvalue())
            self.assertEqual(exported["schema_version"], 2)
            self.assertEqual(
                [item.get("type") for item in exported["conversation_items"]],
                [None, "reasoning", "backend_tool_call", "reasoning", None],
            )
            self.assertEqual(
                exported["conversation_items"][0]["content_parts"][1]["url"],
                "data:image/png;base64,fixture",
            )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (source / "summary.json", source / "chat_history.jsonl")
                },
                source_before,
            )


if __name__ == "__main__":
    unittest.main()
