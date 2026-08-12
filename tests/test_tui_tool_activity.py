from __future__ import annotations

import unittest

from neuro_code.interfaces.tui.tool_activity import (
    ToolCallSnapshot,
    present_tool_activity_peek,
    present_tool_inspector,
)
from neuro_code.interfaces.tui.tool_activity.renderers import renderer_for
from neuro_code.shared.ui_language import UiLanguage


class ToolActivityPresentationTests(unittest.TestCase):
    def test_generic_renderer_fallback_is_bounded_and_redacted(self) -> None:
        content = "\n".join(
            ["API_KEY=sk-genericsecret123", *(f"result line {index}" for index in range(40))]
        )
        call = ToolCallSnapshot(
            call_id="generic-call",
            name="unknown_tool",
            arguments={"path": "src/example.py"},
            phase="completed",
            content=content,
            metadata={"count": 41},
        )

        renderer = renderer_for(call.name)
        rendered = renderer.render(call, UiLanguage.ENGLISH, budget=7)

        self.assertEqual(renderer.name, "generic")
        self.assertLessEqual(len(rendered.lines), 7)
        text = "\n".join(line.text for line in rendered.lines)
        self.assertIn("41 results", text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("sk-genericsecret123", text)
        self.assertIn("more result lines", text)

    def test_specialized_renderers_consume_existing_metadata(self) -> None:
        fixtures = (
            (
                ToolCallSnapshot(
                    "tree",
                    "list_tree",
                    {"path": "."},
                    content="src/\n  app.py",
                    metadata={"path": "/workspace", "count": 412, "max_depth": 4},
                ),
                "list_tree",
                "412 entries · depth 4",
            ),
            (
                ToolCallSnapshot(
                    "grep",
                    "grep",
                    {"query": "Tool", "path": "src"},
                    content="src/a.py:1:Tool",
                    metadata={"count": 71, "files_matched": 9, "scanned_files": 120},
                ),
                "grep",
                "71 matches · 9 files",
            ),
            (
                ToolCallSnapshot(
                    "read",
                    "read_file",
                    {"path": "src/a.py", "start_line": 20, "max_lines": 10},
                    content="    20\tvalue = 1",
                    metadata={"path": "/workspace/src/a.py", "total_lines": 340},
                ),
                "read_file",
                "340 total lines",
            ),
            (
                ToolCallSnapshot(
                    "bash",
                    "bash",
                    {"command": "pytest -q"},
                    content="1 passed",
                    metadata={"exit_code": 0, "truncated": False},
                ),
                "bash",
                "Exit code 0",
            ),
        )
        for call, expected_renderer, expected_summary in fixtures:
            with self.subTest(tool=call.name):
                rendered = renderer_for(call.name).render(
                    call,
                    UiLanguage.ENGLISH,
                    budget=7,
                )
                self.assertEqual(rendered.renderer, expected_renderer)
                self.assertIn(
                    expected_summary,
                    "\n".join(line.text for line in rendered.lines),
                )

    def test_normal_permission_and_completed_are_not_repeated_in_peek(self) -> None:
        call = ToolCallSnapshot(
            call_id="allowed",
            name="bash",
            arguments={"command": "printf ok"},
            phase="completed",
            permission_effect="allow",
            permission_reason="normal workspace policy",
            duration="12ms",
            content="ok",
            metadata={"exit_code": 0},
        )

        peek = present_tool_activity_peek(
            title="Agent activity",
            calls=(call,),
            selected_index=0,
            language=UiLanguage.ENGLISH,
        )
        text = "\n".join((peek.selected_summary, *(line.text for line in peek.lines)))

        self.assertEqual(peek.marker, "✓")
        self.assertLessEqual(peek.logical_line_count, 10)
        self.assertNotIn("allow", text.casefold())
        self.assertNotIn("normal workspace policy", text)
        self.assertNotIn("completed", text.casefold())

    def test_inspector_redacts_input_and_whitelists_metadata(self) -> None:
        call = ToolCallSnapshot(
            call_id="safe-call-id",
            name="bash",
            arguments={
                "command": "curl -H 'Authorization: Bearer secretvalue123' example.test",
                "api_key": "sk-inputsecret123",
            },
            phase="completed",
            permission_effect="allow",
            permission_reason="workspace policy",
            duration="20ms",
            content="API_KEY=sk-outputsecret123",
            metadata={
                "exit_code": 0,
                "output_artifact_truncated": True,
                "output_artifact_id": "hidden-artifact-id",
                "output_artifact_path": "hidden/path",
                "private_debug": "must not leak",
            },
            has_artifact=True,
            artifact_content="token=sk-artifactsecret123",
            artifact_stored_truncated=True,
            artifact_read_truncated=True,
        )

        inspector = present_tool_inspector(call, language=UiLanguage.ENGLISH)

        self.assertNotIn("sk-inputsecret123", inspector.input)
        self.assertNotIn("secretvalue123", inspector.input)
        self.assertIn("[REDACTED]", inspector.input)
        self.assertNotIn("sk-artifactsecret123", inspector.output)
        self.assertIn("[REDACTED]", inspector.output)
        self.assertIn("safe-call-id", inspector.meta)
        self.assertIn("allow · workspace policy", inspector.meta)
        self.assertIn("exit_code", inspector.meta)
        self.assertNotIn("hidden-artifact-id", inspector.meta)
        self.assertNotIn("hidden/path", inspector.meta)
        self.assertNotIn("private_debug", inspector.meta)
        self.assertTrue(inspector.output_truncated)
        self.assertIn("256 KiB", inspector.output_notice)
        self.assertIn("stored output artifact", inspector.output_notice)


if __name__ == "__main__":
    unittest.main()
