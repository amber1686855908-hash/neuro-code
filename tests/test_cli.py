from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from neuro_code.cli import _normalize_rule, main
from neuro_code.config import AppConfig
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
from neuro_code.domain.tools import ToolDefinition


class CliProvider:
    provider_name = "cli-fixture"
    model_name = "fixture-model"

    def __init__(self) -> None:
        self.contexts: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        self.contexts.append(context)
        yield ModelTextDelta("fixture response")
        yield ModelCompleted("stop", 2, 3)


class CliTests(unittest.TestCase):
    @staticmethod
    def _write_provider_config(state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "config.toml").write_text(
            """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
""",
            encoding="utf-8",
        )

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
        self.assertEqual(payload, {"name": "neuro-code", "version": "0.1.0.dev0"})

    def test_inspect_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_provider_config(root / "state")
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(root / "state"),
                        "FIXTURE_KEY": "never-print-this",
                    },
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
                (("version",), "neuro-code"),
                (("inspect", "--cwd", str(root)), "provider: (not configured)"),
                (("completions", "bash"), "complete -F"),
                (("completions", "zsh"), "#compdef"),
                (("completions", "fish"), "complete -c"),
                (("completions", "powershell"), "Register-ArgumentCompleter"),
            ):
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {"HOME": str(root), "NEURO_CODE_HOME": str(root / "state")},
                        clear=True,
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main(arguments)
                self.assertEqual(exit_code, 0)
                self.assertIn(expected, output.getvalue())

    def test_headless_plain_json_and_jsonl_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output_format in ("plain", "json", "jsonl"):
                self._write_provider_config(root / output_format)
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "NEURO_CODE_HOME": str(root / output_format),
                            "HOME": str(root),
                            "FIXTURE_KEY": "fixture-key",
                        },
                        clear=True,
                    ),
                    patch("neuro_code.cli.create_routed_provider", return_value=CliProvider()),
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

    def test_agent_subcommand_requires_a_prompt(self) -> None:
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            exit_code = main(("agent",))
        self.assertEqual(exit_code, 2)
        self.assertIn("agent subcommand requires", errors.getvalue())

    def test_no_subcommand_launches_the_tui(self) -> None:
        launch = AsyncMock(return_value=0)
        with patch("neuro_code.cli._run_tui", launch):
            exit_code = main(())
        self.assertEqual(exit_code, 0)
        launch.assert_awaited_once()

    def test_tui_composition_uses_the_selected_provider_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_provider_config(root / "state")
            captured: dict[str, object] = {}

            class TuiFixture:
                def __init__(
                    self,
                    runner: object,
                    *,
                    approval_controller: object,
                    provider_controller: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                ) -> None:
                    captured.update(
                        runner=runner,
                        approval_controller=approval_controller,
                        provider_controller=provider_controller,
                        provider_name=provider_name,
                        model_name=model_name,
                        cwd=cwd,
                    )

                async def run_async(self) -> None:
                    captured["ran"] = True

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(root / "state"),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch("neuro_code.cli.create_routed_provider", return_value=CliProvider()),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["provider_name"], "cli-fixture")
            self.assertEqual(captured["model_name"], "fixture-model")
            self.assertEqual(captured["cwd"], root.resolve())
            self.assertIs(captured["runner"], captured["provider_controller"])
            self.assertTrue(captured["ran"])

    def test_tui_profile_controller_recomposes_a_fresh_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "first"

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                encoding="utf-8",
            )
            selected: list[str] = []
            captured: dict[str, object] = {}

            def create(config: AppConfig, *, failover: bool) -> CliProvider:
                del failover
                selected.append(config.provider.name)
                provider = CliProvider()
                provider.provider_name = config.provider.name
                provider.model_name = config.provider.model
                return provider

            class TuiFixture:
                def __init__(
                    self,
                    runner: object,
                    *,
                    approval_controller: object,
                    provider_controller: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                ) -> None:
                    del approval_controller, provider_name, model_name, cwd
                    self.runner = runner
                    self.provider_controller = provider_controller

                async def run_async(self) -> None:
                    selection = await self.provider_controller.select_profile("second")
                    captured["selection"] = selection
                    captured["same_controller"] = self.runner is self.provider_controller

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIRST_KEY": "first-secret",
                        "SECOND_KEY": "second-secret",
                    },
                    clear=True,
                ),
                patch("neuro_code.cli.create_routed_provider", side_effect=create),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(selected, ["first", "second"])
            self.assertTrue(captured["same_controller"])
            selection = captured["selection"]
            self.assertTrue(selection.changed)
            self.assertIsNone(selection.previous_session_id)

    def test_resume_list_and_export_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(root / "state"),
                "FIXTURE_KEY": "fixture-key",
            }
            self._write_provider_config(root / "state")

            def run(arguments: tuple[str, ...]) -> tuple[int, str]:
                output = io.StringIO()
                with (
                    patch.dict("os.environ", environment, clear=True),
                    patch("neuro_code.cli.create_routed_provider", return_value=CliProvider()),
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
                        "current_model_id": "xai-test-model",
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
            environment = {"HOME": str(root), "NEURO_CODE_HOME": str(root / "state")}
            self._write_provider_config(root / "state")

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("import-session", str(source), "--json", "--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["session"]["id"], "rust-cli-id")
            self.assertEqual(payload["session"]["provider"], "upstream-rust-import")
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

            resume_provider = CliProvider()
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {**environment, "FIXTURE_KEY": "fixture-key"},
                    clear=True,
                ),
                patch("neuro_code.cli.create_routed_provider", return_value=resume_provider),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "continue imported session",
                        "--resume",
                        "rust-cli-id",
                        "--cwd",
                        str(root),
                    )
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(resume_provider.contexts), 1)
            resumed_context = resume_provider.contexts[0]
            self.assertEqual(resumed_context.source_provider, "upstream-rust-import")
            self.assertEqual(resumed_context.source_model, "xai-test-model")
            self.assertEqual(len(resumed_context.preserved_items), 3)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (source / "summary.json", source / "chat_history.jsonl")
                },
                source_before,
            )

    def test_provider_list_inspect_and_one_shot_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "first"
fallbacks = ["second"]

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                encoding="utf-8",
            )
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIRST_KEY": "first-secret",
                "SECOND_KEY": "second-secret",
            }

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(("providers", "list", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            profiles = json.loads(output.getvalue())
            self.assertEqual([profile["name"] for profile in profiles], ["first", "second"])
            self.assertTrue(profiles[0]["default"])
            self.assertTrue(profiles[1]["fallback"])
            self.assertNotIn("first-secret", output.getvalue())

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(("providers", "inspect", "second", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["model"], "second-model")

            selected: list[str] = []
            failover_values: list[bool] = []

            def create(config: AppConfig, *, failover: bool) -> CliProvider:
                selected.append(config.provider.name)
                failover_values.append(failover)
                return CliProvider()

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch("neuro_code.cli.create_routed_provider", side_effect=create),
                redirect_stdout(output),
            ):
                exit_code = main(("-p", "hello", "--provider", "second", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(selected, ["second"])
            self.assertEqual(failover_values, [True])

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch("neuro_code.cli.create_routed_provider", side_effect=create),
                redirect_stdout(output),
            ):
                exit_code = main(("-p", "hello", "--no-failover", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(failover_values, [True, False])


if __name__ == "__main__":
    unittest.main()
