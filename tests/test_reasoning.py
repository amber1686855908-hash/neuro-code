from __future__ import annotations

import unittest

from neuro_code.domain.reasoning import ReasoningEffort, reasoning_guidance


class ReasoningEffortTests(unittest.TestCase):
    def test_levels_have_stable_order_glyphs_and_default_fallback(self) -> None:
        self.assertEqual(
            tuple(ReasoningEffort),
            (
                ReasoningEffort.LOW,
                ReasoningEffort.MEDIUM,
                ReasoningEffort.HIGH,
                ReasoningEffort.XHIGH,
                ReasoningEffort.ULTRACODE,
            ),
        )
        self.assertEqual(
            tuple(effort.glyph for effort in ReasoningEffort),
            ("○", "◐", "●", "⬤", "⚡"),
        )
        self.assertIs(ReasoningEffort.ULTRACODE.effective, ReasoningEffort.XHIGH)
        self.assertIs(ReasoningEffort.HIGH.effective, ReasoningEffort.HIGH)
        self.assertTrue(ReasoningEffort.ULTRACODE.requires_workflow_orchestration)
        self.assertFalse(ReasoningEffort.XHIGH.requires_workflow_orchestration)

    def test_ultracode_guidance_discloses_missing_workflow(self) -> None:
        guidance = reasoning_guidance(ReasoningEffort.ULTRACODE)

        self.assertIn("extra-high review depth", guidance)
        self.assertIn("workflow orchestration is not available", guidance)
        self.assertIn("do not claim", guidance)


if __name__ == "__main__":
    unittest.main()
