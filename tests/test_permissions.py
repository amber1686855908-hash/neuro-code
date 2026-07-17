from __future__ import annotations

import unittest

from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)


class PermissionTests(unittest.TestCase):
    def test_explicit_deny_wins_over_allow_and_bypass(self) -> None:
        manager = PermissionManager(
            mode=PermissionMode.BYPASS,
            rules=(
                PermissionRule(PermissionEffect.ALLOW, "bash:git *"),
                PermissionRule(PermissionEffect.DENY, "bash:git push*"),
            ),
        )
        decision = manager.decide("bash", {"command": "git push --force"}, side_effecting=True)
        self.assertEqual(decision.effect, PermissionEffect.DENY)

    def test_read_only_tool_is_approved_in_headless_mode(self) -> None:
        manager = PermissionManager()
        decision = manager.decide("read_file", {"path": "README.md"}, side_effecting=False)
        self.assertTrue(decision.allowed)

    def test_unmatched_side_effect_is_denied_in_headless_default_mode(self) -> None:
        manager = PermissionManager(mode=PermissionMode.DEFAULT, interactive=False)
        decision = manager.decide("bash", {"command": "touch x"}, side_effecting=True)
        self.assertEqual(decision.effect, PermissionEffect.DENY)

    def test_ask_rule_becomes_denial_when_no_ui_can_prompt(self) -> None:
        manager = PermissionManager(
            rules=(PermissionRule(PermissionEffect.ASK, "search_replace"),),
            interactive=False,
        )
        decision = manager.decide("search_replace", {}, side_effecting=True)
        self.assertEqual(decision.effect, PermissionEffect.DENY)
        self.assertIn("cannot prompt", decision.reason)

    def test_bash_deny_checks_every_segment_wrapper_and_nested_shell(self) -> None:
        manager = PermissionManager(
            mode=PermissionMode.BYPASS,
            rules=(PermissionRule(PermissionEffect.DENY, "bash:rm *"),),
        )
        commands = (
            "git status && rm -rf generated",
            "timeout 30 env FOO=x rm -rf generated",
            "bash -c 'git status && rm -rf generated'",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = manager.decide("bash", {"command": command}, side_effecting=True)
                self.assertEqual(decision.effect, PermissionEffect.DENY)
                self.assertIn("sequence", decision.reason)

    def test_default_mode_requires_every_bash_segment_to_be_allowed(self) -> None:
        partial = PermissionManager(rules=(PermissionRule(PermissionEffect.ALLOW, "bash:git *"),))
        complete = PermissionManager(
            rules=(
                PermissionRule(PermissionEffect.ALLOW, "bash:git *"),
                PermissionRule(PermissionEffect.ALLOW, "bash:pytest*"),
            )
        )
        command = "git status && pytest -q"

        self.assertEqual(
            partial.decide("bash", {"command": command}, side_effecting=True).effect,
            PermissionEffect.DENY,
        )
        complete_decision = complete.decide("bash", {"command": command}, side_effecting=True)
        self.assertEqual(complete_decision.effect, PermissionEffect.ALLOW)
        self.assertIn("every bash command segment", complete_decision.reason)

    def test_complex_bash_fails_closed_when_restrictive_rule_exists(self) -> None:
        headless = PermissionManager(
            mode=PermissionMode.BYPASS,
            rules=(PermissionRule(PermissionEffect.DENY, "bash:rm *"),),
        )
        interactive = PermissionManager(
            mode=PermissionMode.BYPASS,
            rules=(PermissionRule(PermissionEffect.DENY, "bash:rm *"),),
            interactive=True,
        )
        command = "echo $(generated-command)"

        denied = headless.decide("bash", {"command": command}, side_effecting=True)
        ask = interactive.decide("bash", {"command": command}, side_effecting=True)

        self.assertEqual(denied.effect, PermissionEffect.DENY)
        self.assertIn("safely decomposed", denied.reason)
        self.assertEqual(ask.effect, PermissionEffect.ASK)

    def test_unrelated_rule_does_not_restrict_bypass_bash(self) -> None:
        manager = PermissionManager(
            mode=PermissionMode.BYPASS,
            rules=(PermissionRule(PermissionEffect.DENY, "search_replace"),),
        )
        decision = manager.decide(
            "bash", {"command": "echo $(dynamic-command)"}, side_effecting=True
        )
        self.assertEqual(decision.effect, PermissionEffect.ALLOW)


if __name__ == "__main__":
    unittest.main()
