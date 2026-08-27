from __future__ import annotations

import unittest

from neuro_code.domain.conversation.reasoning import ReasoningEffort, reasoning_guidance


class ReasoningEffortTests(unittest.TestCase):
    def test_levels_have_stable_order_glyphs_and_default_fallback(self) -> None:
        self.assertEqual(
            tuple(ReasoningEffort),
            (
                ReasoningEffort.LOW,
                ReasoningEffort.MEDIUM,
                ReasoningEffort.HIGH,
                ReasoningEffort.XHIGH,
                ReasoningEffort.MAX,
                ReasoningEffort.ULTRACODE,
            ),
        )
        self.assertEqual(
            tuple(effort.glyph for effort in ReasoningEffort),
            ("○", "◐", "●", "⬤", "◆", "⚡"),
        )
        self.assertIs(ReasoningEffort.ULTRACODE.effective, ReasoningEffort.MAX)
        self.assertIs(ReasoningEffort.HIGH.effective, ReasoningEffort.HIGH)
        self.assertTrue(ReasoningEffort.ULTRACODE.requires_workflow_orchestration)
        self.assertFalse(ReasoningEffort.XHIGH.requires_workflow_orchestration)
        self.assertFalse(ReasoningEffort.MAX.requires_workflow_orchestration)

    def test_ultracode_guidance_discloses_application_delegation(self) -> None:
        guidance = reasoning_guidance(ReasoningEffort.ULTRACODE)

        self.assertIn("maximum ordinary single-agent review depth", guidance)
        self.assertIn("application-level delegation strategy", guidance)
        self.assertIn("exactly one bounded path", guidance)
        self.assertIn("Never claim", guidance)

    def test_max_guidance_is_deep_but_remains_single_agent(self) -> None:
        guidance = reasoning_guidance(ReasoningEffort.MAX)

        self.assertIn("trace all relevant dependencies", guidance)
        self.assertIn("broad but bounded verification", guidance)
        self.assertIn("do not start child agents", guidance)


if __name__ == "__main__":
    unittest.main()
