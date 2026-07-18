from __future__ import annotations

import unittest

from neuro_code.domain.context_usage import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.messages import Message, Role, ToolCall


class ContextUsageTests(unittest.TestCase):
    def test_text_estimate_distinguishes_empty_ascii_and_cjk_text(self) -> None:
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertEqual(estimate_text_tokens("abcdefghij"), 3)
        self.assertEqual(estimate_text_tokens("测试上下文"), 3)

    def test_context_estimate_includes_messages_reasoning_and_tool_arguments(self) -> None:
        plain = estimate_context_tokens((Message(Role.USER, "inspect"),))
        with_tool = estimate_context_tokens(
            (
                Message(Role.USER, "inspect"),
                Message(
                    Role.ASSISTANT,
                    reasoning_content="check carefully",
                    tool_calls=(ToolCall("read-1", "read_file", {"path": "README.md"}),),
                ),
            )
        )

        self.assertGreater(plain, 0)
        self.assertGreater(with_tool, plain)


if __name__ == "__main__":
    unittest.main()
