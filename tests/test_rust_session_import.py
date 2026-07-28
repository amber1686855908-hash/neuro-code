from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from neuro_code.adapters.rust_session import (
    UPSTREAM_IMPORT_PROVIDER,
    load_rust_session,
)
from neuro_code.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.providers.anthropic import AnthropicProvider
from neuro_code.shared.errors import SessionError


def _write_session(
    root: Path,
    records: list[str],
    *,
    chat_format_version: int = 1,
    sandbox_profile: object | None = None,
    generated_title: object | None = None,
) -> Path:
    session_dir = root / "rust-session-id"
    session_dir.mkdir()
    summary = {
        "info": {"id": "rust-session-id", "cwd": "/source/workspace"},
        "created_at": "2026-07-01T10:20:30.123456789Z",
        "updated_at": "2026-07-02T11:22:33.987654321Z",
        "current_model_id": "xai-test-model",
        "chat_format_version": chat_format_version,
    }
    if sandbox_profile is not None:
        summary["sandbox_profile"] = sandbox_profile
    if generated_title is not None:
        summary["generated_title"] = generated_title
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
    def test_saved_sandbox_profile_is_preserved_and_unsupported_profiles_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supported = _write_session(
                root,
                [],
                sandbox_profile="readonly",
            )
            imported = load_rust_session(supported)
            self.assertIs(
                imported.snapshot.summary.sandbox_profile,
                SandboxProfile.READ_ONLY,
            )

        for unsupported in ("custom-development", 42):
            with tempfile.TemporaryDirectory() as directory:
                source = _write_session(
                    Path(directory),
                    [],
                    sandbox_profile=unsupported,
                )
                with self.assertRaisesRegex(SessionError, "sandbox profile"):
                    load_rust_session(source)

    def test_generated_title_is_preserved_and_invalid_title_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _write_session(
                Path(directory),
                [],
                generated_title="  Imported session title  ",
            )
            imported = load_rust_session(source)
            self.assertEqual(imported.snapshot.summary.title, "Imported session title")

        with tempfile.TemporaryDirectory() as directory:
            source = _write_session(Path(directory), [], generated_title=42)
            with self.assertRaisesRegex(SessionError, "generated_title"):
                load_rust_session(source)

    def test_current_jsonl_format_is_converted_without_writing_the_source(self) -> None:
        records = [
            json.dumps({"type": "system", "content": "source system"}),
            json.dumps(
                {
                    "type": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image", "url": "data:image/png;base64,aW1hZ2U="},
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
                    "images": [{"type": "image", "url": "data:image/png;base64,aW1hZ2U="}],
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
            self.assertEqual(imported.snapshot.summary.provider, UPSTREAM_IMPORT_PROVIDER)
            self.assertEqual(imported.snapshot.summary.model, "xai-test-model")
            self.assertEqual(imported.imported_messages, 4)
            self.assertEqual(imported.total_records, 8)
            self.assertEqual(imported.invalid_records, 1)
            self.assertEqual(imported.unsupported_records, 1)
            self.assertEqual(imported.preserved_context_records, 2)
            self.assertEqual(imported.recovered_context_records, 0)
            self.assertEqual(imported.deduplicated_context_records, 0)
            self.assertEqual(imported.invalid_embedded_records, 0)
            self.assertEqual(imported.preserved_images, 2)
            self.assertEqual(imported.omitted_tool_calls, 1)

            system, user, assistant, tool = imported.snapshot.messages
            self.assertEqual(system.role, Role.SYSTEM)
            self.assertEqual(user.content, "inspect\ncontinue")
            self.assertEqual(
                [part.kind for part in user.content_parts],
                [ContentPartKind.TEXT, ContentPartKind.IMAGE, ContentPartKind.TEXT],
            )
            self.assertEqual(
                user.model_content(),
                f"inspect\n{IMAGE_MODEL_PLACEHOLDER}\ncontinue",
            )
            self.assertEqual(assistant.tool_calls[0].arguments, {"path": "src/main.rs"})
            self.assertEqual(tool.role, Role.TOOL)
            self.assertEqual(tool.name, "read_file")
            self.assertEqual(tool.tool_call_id, "call-1")
            self.assertEqual(tool.content, "file contents")
            self.assertEqual(tool.content_parts[1].kind, ContentPartKind.IMAGE)

            _, anthropic_messages = AnthropicProvider._convert_messages(imported.snapshot.messages)
            self.assertEqual(anthropic_messages[0]["content"][1]["type"], "image")
            self.assertEqual(
                anthropic_messages[2]["content"][0]["content"][1]["type"],
                "image",
            )

            items = imported.snapshot.items
            self.assertEqual(len(items), 6)
            self.assertIsInstance(items[0], Message)
            reasoning = items[2]
            backend_call = items[5]
            self.assertIsInstance(reasoning, PreservedContextItem)
            self.assertIsInstance(backend_call, PreservedContextItem)
            assert isinstance(reasoning, PreservedContextItem)
            assert isinstance(backend_call, PreservedContextItem)
            self.assertEqual(reasoning.kind, ContextItemKind.REASONING)
            self.assertEqual(reasoning.to_dict()["id"], "reasoning-1")
            self.assertEqual(backend_call.kind, ContextItemKind.BACKEND_TOOL_CALL)

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
                    "reasoning_content": "legacy thinking",
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
            self.assertEqual(imported.preserved_context_records, 1)
            self.assertEqual(imported.recovered_context_records, 1)
            self.assertEqual(imported.preserved_images, 1)
            self.assertEqual(imported.snapshot.messages[1].content, "legacy question")
            self.assertEqual(
                imported.snapshot.messages[1].content_parts[1].url,
                "fixture",
            )
            self.assertEqual(imported.snapshot.messages[2].tool_calls[0].name, "bash")
            self.assertEqual(imported.snapshot.messages[3].name, "bash")
            reasoning = imported.snapshot.items[2]
            self.assertIsInstance(reasoning, PreservedContextItem)
            assert isinstance(reasoning, PreservedContextItem)
            self.assertEqual(
                reasoning.to_dict()["summary"][0]["text"],
                "legacy thinking",
            )

    def test_raw_output_recovers_ordered_context_and_deduplicates_backend_calls(self) -> None:
        records = [
            json.dumps(
                {
                    "type": "backend_tool_call",
                    "kind": {
                        "tool_type": "web_search",
                        "id": "web-existing",
                        "status": "completed",
                        "action": {"type": "search", "query": "existing"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "content": "answer",
                    "reasoning": {
                        "text": "ignored because raw_output is authoritative",
                        "id": "reasoning-ignored",
                    },
                    "raw_output": [
                        {
                            "type": "reasoning",
                            "id": "reasoning-parallel",
                            "summary": [],
                            "encrypted_content": "opaque",
                        },
                        {
                            "type": "web_search_call",
                            "id": "web-existing",
                            "status": "completed",
                            "action": {"type": "search", "query": "duplicate"},
                        },
                        {
                            "type": "custom_tool_call",
                            "id": "x-new",
                            "status": "completed",
                            "name": "x_keyword_search",
                            "input": '{"query":"fixture"}',
                        },
                        {
                            "type": "code_interpreter_call",
                            "id": "code-new",
                            "status": "completed",
                            "code": "print('fixture')",
                            "outputs": [],
                        },
                        {
                            "type": "web_search_call",
                            "status": "completed",
                            "action": {"type": "search", "query": "missing id"},
                        },
                        {"type": "reasoning", "id": "invalid", "summary": "bad"},
                        {"type": "future_output_item", "id": "future-1"},
                        42,
                        {
                            "type": "message",
                            "id": "message-1",
                            "status": "completed",
                            "role": "assistant",
                            "content": [],
                        },
                    ],
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = _write_session(Path(directory), records)

            imported = load_rust_session(session_dir)

            self.assertEqual(imported.imported_messages, 1)
            self.assertEqual(imported.preserved_context_records, 4)
            self.assertEqual(imported.recovered_context_records, 3)
            self.assertEqual(imported.deduplicated_context_records, 1)
            self.assertEqual(imported.invalid_embedded_records, 3)
            self.assertEqual(imported.unsupported_embedded_records, 1)
            self.assertEqual(imported.invalid_records, 0)
            self.assertEqual(
                [
                    item.kind.value if isinstance(item, PreservedContextItem) else "assistant"
                    for item in imported.snapshot.items
                ],
                [
                    "backend_tool_call",
                    "reasoning",
                    "backend_tool_call",
                    "backend_tool_call",
                    "assistant",
                ],
            )
            payloads = [
                item.to_dict()
                for item in imported.snapshot.items
                if isinstance(item, PreservedContextItem)
            ]
            self.assertEqual(payloads[1]["id"], "reasoning-parallel")
            self.assertEqual(payloads[2]["kind"]["tool_type"], "x_search")
            self.assertEqual(payloads[2]["kind"]["id"], "x-new")
            self.assertEqual(payloads[3]["kind"]["tool_type"], "code_interpreter")
            self.assertNotIn("reasoning-ignored", json.dumps(payloads))

    def test_singular_v1_reasoning_is_recovered_before_the_assistant(self) -> None:
        records = [
            json.dumps(
                {
                    "type": "assistant",
                    "content": "answer",
                    "reasoning": {
                        "text": "visible reasoning",
                        "encrypted": "opaque signature",
                        "id": "reasoning-legacy",
                    },
                }
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            imported = load_rust_session(_write_session(Path(directory), records))

            self.assertEqual(imported.recovered_context_records, 1)
            self.assertEqual(len(imported.snapshot.items), 2)
            reasoning = imported.snapshot.items[0]
            self.assertIsInstance(reasoning, PreservedContextItem)
            assert isinstance(reasoning, PreservedContextItem)
            payload = reasoning.to_dict()
            self.assertEqual(payload["id"], "reasoning-legacy")
            self.assertEqual(payload["summary"][0]["text"], "visible reasoning")
            self.assertEqual(payload["encrypted_content"], "opaque signature")

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
