from __future__ import annotations

import unittest

from neuro_code.tui_commands import slash_completions


class SlashCompletionTests(unittest.TestCase):
    def test_command_prefix_completes_command_before_its_first_parameter(self) -> None:
        self.assertEqual(slash_completions("/eff")[0].value, "/effort")
        self.assertEqual(slash_completions("/effort")[0].value, "/effort low")
        self.assertEqual(slash_completions("/effort h")[0].value, "/effort high")
        self.assertEqual(slash_completions("/mode")[0].value, "/mode normal")
        self.assertEqual(slash_completions("/mode p")[0].value, "/mode plan")
        self.assertEqual(slash_completions("/plan")[0].value, "/plan ")
        self.assertEqual(slash_completions("/view-")[0].value, "/view-plan")
        self.assertEqual(slash_completions("/comment-")[0].value, "/comment-plan")
        self.assertEqual(slash_completions("/comment-plan")[0].value, "/comment-plan ")
        self.assertEqual(slash_completions("/execute-")[0].value, "/execute-plan")
        self.assertEqual(slash_completions("/schedule-")[0].value, "/schedule-plan")
        self.assertEqual(slash_completions("/run-t")[0].value, "/run-task")
        self.assertEqual(slash_completions("/run-task")[0].value, "/run-task ")
        self.assertEqual(slash_completions("/view-t")[0].value, "/view-task")
        self.assertEqual(slash_completions("/view-task")[0].value, "/view-task ")

    def test_provider_choices_and_free_form_parameter_syntax_are_exposed(self) -> None:
        provider = slash_completions(
            "/provider",
            provider_names=("deepseek", "fallback"),
        )
        resume = slash_completions("/resume")

        self.assertEqual(
            [item.value for item in provider],
            [
                "/provider deepseek",
                "/provider fallback",
            ],
        )
        self.assertEqual(resume[0].value, "/resume ")
        self.assertEqual(resume[0].display, "/resume SESSION_ID")
        self.assertEqual(slash_completions("ordinary prompt"), ())


if __name__ == "__main__":
    unittest.main()
