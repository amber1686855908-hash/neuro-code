from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pygrok_build.adapters.rust_session import (
    IMAGE_PLACEHOLDER,
    RUST_IMPORT_PROVIDER,
    load_rust_session,
)
from pygrok_build.domain.messages import Role
from pygrok_build.errors import SessionError


def _write_session(
    root: Path,
    records: list[str],
    *,
    chat_format_version: int = 1,
) -> Path:
    session_dir = root / "rust-session-id"
    session_dir.mkdir()
    summary = {
        "info": {"id": "rust-session-id", "cwd": "/source/workspace"},
        "created_at": "2026-07-01T10:20:30.123456789Z",
        "updated_at": "2026-07-02T11:22:33.987654321Z",
        "current_model_id": "grok-4.5",
        "chat_format_version": chat_format_version,
    }
    (session_dir / "summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    (session_dir / "chat_history.jsonl").write_text(
        "\n".join(records) + "\n",
        encoding="utf-8",
    )
    return session_dir


class RustSessionImportTests(unittest.TestCase):
    def test_current_jsonl_format_is_converted_without_writing_the_source(self) -> None:
        records = [
            json.dumps({"type": "system", "content": "source system"}),
            json.dumps(
                {
                    "type": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image", "url": "data:image/png;base64,fixture"},
                        {"type": "text", "text": "continue"},
                    ],
                }
            ),
            json.dumps({"type": "reasoning", "id": "reasoning-1", "summary": []}),
            json.dumps(
                {
                    "type": "assistant",
                    "content": "running",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "arguments": '{"path":"src/main.rs"}',
                        },
                        {
                            "id": "call-invalid",
                            "name": "bash",
                            "arguments": "[]",
                        },
                    ],
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "content": "file contents",
                    "images": [{"type": "image", "url": "data:image/png;base64,fixture"}],
                }
            ),
            json.dumps({"type": "backend_tool_call", "kind": {"tool_type": "web_search"}}),
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_call_id": "orphan-call",
                    "content": "orphan",
                }
            ),
            '{"type":"assistant","content":"torn"',
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = _write_session(Path(directory), records)
            summary_before = (session_dir / "summary.json").read_bytes()
            chat_before = (session_dir / "chat_history.jsonl").read_bytes()
            summary_mtime = (session_dir / "summary.json").stat().st_mtime_ns
            chat_mtime = (session_dir / "chat_history.jsonl").stat().st_mtime_ns

            imported = load_rust_session(session_dir)

            self.assertEqual(imported.snapshot.summary.id, "rust-session-id")
            self.assertEqual(imported.snapshot.summary.cwd, "/source/workspace")
            self.assertEqual(imported.snapshot.summary.provider, RUST_IMPORT_PROVIDER)
            self.assertEqual(imported.snapshot.summary.model, "grok-4.5")
            self.assertEqual(imported.imported_messages, 4)
            self.assertEqual(imported.total_records, 8)
            self.assertEqual(imported.invalid_records, 1)
            self.assertEqual(imported.unsupported_records, 3)
            self.assertEqual(imported.omitted_images, 2)
            self.assertEqual(imported.omitted_tool_calls, 1)

            system, user, assistant, tool = imported.snapshot.messages
            self.assertEqual(system.role, Role.SYSTEM)
            self.assertEqual(user.content, f"inspect\n{IMAGE_PLACEHOLDER}\ncontinue")
            self.assertEqual(assistant.tool_calls[0].arguments, {"path": "src/main.rs"})
            self.assertEqual(tool.role, Role.TOOL)
            self.assertEqual(tool.name, "read_file")
            self.assertEqual(tool.tool_call_id, "call-1")
            self.assertEqual(tool.content, f"file contents\n{IMAGE_PLACEHOLDER}")

            self.assertEqual((session_dir / "summary.json").read_bytes(), summary_before)
            self.assertEqual((session_dir / "chat_history.jsonl").read_bytes(), chat_before)
            self.assertEqual((session_dir / "summary.json").stat().st_mtime_ns, summary_mtime)
            self.assertEqual((session_dir / "chat_history.jsonl").stat().st_mtime_ns, chat_mtime)

    def test_legacy_and_current_records_can_coexist_in_version_zero(self) -> None:
        records = [
            json.dumps({"type": "system", "content": "current-shaped system"}),
            json.dumps(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "legacy question"},
                        {"type": "image_url", "image_url": {"url": "fixture"}},
                    ],
                }
            ),
            json.dumps(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "legacy-call",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "role": "tool",
                    "content": "workspace",
                    "tool_call_id": "legacy-call",
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = _write_session(
                Path(directory),
                records,
                chat_format_version=0,
            )

            imported = load_rust_session(session_dir / "summary.json")

            self.assertEqual(imported.imported_messages, 4)
            self.assertEqual(imported.invalid_records, 0)
            self.assertEqual(imported.unsupported_records, 0)
            self.assertEqual(imported.omitted_images, 1)
            self.assertEqual(
                imported.snapshot.messages[1].content,
                f"legacy question\n{IMAGE_PLACEHOLDER}",
            )
            self.assertEqual(imported.snapshot.messages[2].tool_calls[0].name, "bash")
            self.assertEqual(imported.snapshot.messages[3].name, "bash")

    def test_empty_session_without_chat_file_is_importable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_dir = _write_session(Path(directory), [])
            (session_dir / "chat_history.jsonl").unlink()

            imported = load_rust_session(session_dir)

            self.assertEqual(imported.snapshot.messages, ())
            self.assertEqual(imported.total_records, 0)

    def test_unknown_future_format_and_invalid_summary_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            future = _write_session(root, [], chat_format_version=2)
            with self.assertRaisesRegex(SessionError, "unsupported Rust chat format version"):
                load_rust_session(future)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "summary.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(SessionError, "must be a JSON object"):
                load_rust_session(source)


if __name__ == "__main__":
    unittest.main()
