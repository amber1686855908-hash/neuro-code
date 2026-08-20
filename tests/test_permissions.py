from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.permissions.contracts import build_permission_request
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
    PermissionRuleStore,
)


class PermissionTests(unittest.TestCase):
    def test_permission_rule_and_persistent_store_reject_malformed_state(self) -> None:
        invalid_rules = (
            (PermissionEffect.ALLOW, " ", None, None),
            (PermissionEffect.ALLOW, "read", " ", None),
            (PermissionEffect.ALLOW, "read", None, " "),
        )
        for effect, pattern, path_pattern, operation in invalid_rules:
            with (
                self.subTest(pattern=pattern, path_pattern=path_pattern, operation=operation),
                self.assertRaises(ValueError),
            ):
                PermissionRule(effect, pattern, path_pattern, operation)
        with self.assertRaises(TypeError):
            PermissionRule("allow", "read")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "requires effect"):
            PermissionRule.from_dict({"effect": "allow"})
        with self.assertRaisesRegex(ValueError, "effect is invalid"):
            PermissionRule.from_dict({"effect": "invalid", "pattern": "read"})
        with self.assertRaisesRegex(ValueError, "path_pattern"):
            PermissionRule.from_dict({"effect": "allow", "pattern": "read", "path_pattern": 1})
        with self.assertRaisesRegex(ValueError, "operation"):
            PermissionRule.from_dict({"effect": "allow", "pattern": "read", "operation": 1})

        rule = PermissionRule(PermissionEffect.ALLOW, "read", path_pattern="src/*")
        self.assertEqual(
            rule.to_dict(), {"effect": "allow", "pattern": "read", "path_pattern": "src/*"}
        )
        self.assertFalse(rule.matches("read", {"path": "tests/a.py"}))
        self.assertFalse(
            PermissionRule(PermissionEffect.ALLOW, "read", operation="read").matches(
                "read", {"operation": 1}
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "permissions.json"
            store = PermissionRuleStore(path)
            self.assertEqual(store.load(), ())
            for payload, reason in (
                ("not json", "unreadable"),
                ({"schema_version": 99, "rules": []}, "schema"),
                ({"schema_version": 1, "rules": [1]}, "entry"),
                ({"schema_version": 1, "rules": [None]}, "entry"),
            ):
                path.write_text(
                    payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8",
                )
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    store.load()
            with self.assertRaises(ValueError):
                store.save([object()])  # type: ignore[list-item]
            with self.assertRaises(ValueError):
                store.save([rule] * 513)

    def test_permission_manager_modes_and_rule_lifecycle_are_explicit(self) -> None:
        manager = PermissionManager(interactive=True)
        with self.assertRaises(TypeError):
            manager.replace_rules((object(),))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            manager.set_mode("default")  # type: ignore[arg-type]
        manager.set_mode(PermissionMode.DONT_ASK)
        self.assertEqual(
            manager.decide("bash", {"command": "echo hi"}, side_effecting=True).effect,
            PermissionEffect.DENY,
        )
        manager.set_mode(PermissionMode.ACCEPT_EDITS)
        self.assertTrue(manager.decide("apply_patch", {}, side_effecting=True).allowed)
        self.assertEqual(
            manager.decide("custom", {}, side_effecting=True).effect,
            PermissionEffect.ASK,
        )
        manager.replace_rules((PermissionRule(PermissionEffect.ALLOW, "custom"),))
        self.assertEqual(manager.rules[0].pattern, "custom")
        with tempfile.TemporaryDirectory() as directory:
            store = PermissionRuleStore(Path(directory) / "rules.json")
            manager.save_rules(store)
            manager.replace_rules(())
            manager.load_rules(store)
            self.assertEqual(manager.rules[0].pattern, "custom")

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

    def test_permission_request_hides_edit_content_and_hashes_exact_arguments(self) -> None:
        first = build_permission_request(
            "call-1",
            "search_replace",
            {
                "path": "settings.toml",
                "old": "old-private-value",
                "new": "new-private-value",
            },
            "interactive approval required",
        )
        same_action = build_permission_request(
            "call-2",
            "search_replace",
            {
                "path": "settings.toml",
                "old": "old-private-value",
                "new": "new-private-value",
            },
            "interactive approval required",
        )
        changed_action = build_permission_request(
            "call-3",
            "search_replace",
            {
                "path": "settings.toml",
                "old": "old-private-value",
                "new": "different-private-value",
            },
            "interactive approval required",
        )

        self.assertIn("settings.toml", first.summary)
        self.assertNotIn("old-private-value", first.summary)
        self.assertNotIn("new-private-value", first.summary)
        self.assertEqual(first.scope_key, same_action.scope_key)
        self.assertNotEqual(first.scope_key, changed_action.scope_key)
        assert first.scope_key is not None
        self.assertEqual(len(first.scope_key), 64)

    def test_non_json_arguments_cannot_create_a_session_approval_scope(self) -> None:
        request = build_permission_request(
            "call-1",
            "custom_tool",
            {"value": object()},
            "interactive approval required",
        )

        self.assertIsNone(request.scope_key)

    def test_bash_permission_summary_is_bounded(self) -> None:
        request = build_permission_request(
            "call-1",
            "bash",
            {"command": f"echo {'x' * 3_000}"},
            "interactive approval required",
        )

        self.assertTrue(request.summary.startswith("Run shell command:\necho "))
        self.assertIn("[truncated]", request.summary)
        self.assertLess(len(request.summary), 2_100)

    def test_dynamic_bash_cannot_create_a_session_approval_scope(self) -> None:
        request = build_permission_request(
            "call-1",
            "bash",
            {"command": "echo $(dynamic-command)"},
            "bash command could not be safely decomposed",
        )

        self.assertIsNone(request.scope_key)

    def test_terminal_permission_summary_shows_command_but_not_environment_values(self) -> None:
        request = build_permission_request(
            "call-1",
            "create_terminal",
            {
                "command": "python -m fixture",
                "cwd": "/workspace",
                "environment_fingerprint": "opaque-digest",
                "rows": 24,
                "columns": 80,
            },
            "interactive approval required",
        )

        self.assertIn("Create interactive terminal", request.summary)
        self.assertIn("python -m fixture", request.summary)
        self.assertIn("/workspace", request.summary)
        self.assertNotIn("opaque-digest", request.summary)
        self.assertIsNotNone(request.scope_key)

    def test_path_and_operation_rules_round_trip_atomically(self) -> None:
        rule = PermissionRule(
            PermissionEffect.ALLOW,
            "path:src/*",
            path_pattern="src/*",
            operation="read",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "permissions.json"
            store = PermissionRuleStore(path)
            store.save([rule])
            self.assertEqual(store.load(), (rule,))
            self.assertTrue(rule.matches("read_file", {"operation": "read", "path": "src/a.py"}))
            self.assertFalse(rule.matches("read_file", {"operation": "write", "path": "src/a.py"}))
            self.assertFalse(rule.matches("read_file", {"operation": "read", "path": "tests/a.py"}))


if __name__ == "__main__":
    unittest.main()
