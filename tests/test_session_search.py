from __future__ import annotations

import unittest
from datetime import UTC, datetime

from neuro_code.domain.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.sessions.search import (
    SessionSearchHit,
    SessionSearchPage,
    fallback_session_title,
    searchable_session_text,
)


class SessionSearchDomainTests(unittest.TestCase):
    def test_fallback_title_uses_visible_first_user_words(self) -> None:
        reminder = "hidden " * 2_000
        title = fallback_session_title(
            (
                Message(Role.SYSTEM, "system"),
                Message(
                    Role.USER,
                    f"<system-reminder>{reminder}</system-reminder>\n"
                    "build a portable session search index with safe snippets today",
                ),
                Message(Role.USER, "later prompt"),
            )
        )

        self.assertEqual(
            title,
            "build a portable session search index with safe snippets today",
        )
        self.assertEqual(fallback_session_title(()), "New session")

    def test_searchable_text_excludes_system_and_provider_private_context(self) -> None:
        text = searchable_session_text(
            (
                Message(Role.SYSTEM, "private-system-marker"),
                Message(Role.USER, "visible prompt"),
                PreservedContextItem(
                    ContextItemKind.REASONING,
                    {
                        "type": "reasoning",
                        "id": "reasoning-1",
                        "summary": [{"type": "summary_text", "text": "private-context"}],
                    },
                ),
                Message(
                    Role.ASSISTANT,
                    "visible answer",
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "read_file",
                            {"path": "src/index.py", "line": 42},
                        ),
                    ),
                    reasoning_content="private-reasoning-marker",
                ),
            )
        )

        self.assertIn("visible prompt", text)
        self.assertIn("visible answer", text)
        self.assertIn("read_file", text)
        self.assertNotIn("src/index.py", text)
        self.assertNotIn("private-system-marker", text)
        self.assertNotIn("private-context", text)
        self.assertNotIn("private-reasoning-marker", text)

    def test_search_page_serializes_flat_hits_and_validates_metadata(self) -> None:
        timestamp = datetime(2026, 7, 18, tzinfo=UTC)
        summary = SessionSummary(
            "session-1",
            "/workspace",
            "fixture",
            "model",
            timestamp,
            timestamp,
            title="Search title",
        )
        hit = SessionSearchHit(summary, 1.5, ("title",), "[Search] title")
        page = SessionSearchPage((hit,), None, 1)

        self.assertEqual(page.to_dict()["results"][0]["id"], "session-1")
        with self.assertRaises(ValueError):
            SessionSearchHit(summary, float("nan"), ("title",))
        with self.assertRaises(ValueError):
            SessionSearchHit(summary, 1.0, ("reasoning",))
        with self.assertRaises(ValueError):
            SessionSearchPage((hit,), None, 0)


if __name__ == "__main__":
    unittest.main()
