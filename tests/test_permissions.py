from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.permissions.contracts import build_permission_request
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
    PermissionRuleStore,
)
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemAccessTarget,
    FilesystemTargetRequest,
)
from neuro_code.infrastructure.workspace.paths import (
    resolve_delegated_workspace_path,
    resolve_filesystem_access_targets,
)
from neuro_code.shared.errors import ToolError


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

    def test_canonical_targets_collapse_lexical_aliases_to_one_policy_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            target = root / "src" / "secret.py"
            target.write_text("secret = 1\n", encoding="utf-8")

            plan = resolve_filesystem_access_targets(
                "read_file",
                root,
                tuple(
                    FilesystemTargetRequest(path, FilesystemAccessOperation.READ, must_exist=True)
                    for path in ("./src/secret.py", "src/../src/secret.py", str(target))
                ),
            )

        self.assertEqual({item.canonical_path for item in plan.targets}, {target.resolve()})
        self.assertEqual({item.policy_path for item in plan.targets}, {"src/secret.py"})
        self.assertTrue(all(item.is_primary_workspace for item in plan.targets))

    def test_create_target_proves_an_existing_ancestor_before_authorizing_missing_leaf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            plan = resolve_filesystem_access_targets(
                "apply_patch",
                root,
                (
                    FilesystemTargetRequest(
                        "src/generated/deep/new.py",
                        FilesystemAccessOperation.CREATE,
                    ),
                ),
            )
            target = plan.targets[0]
            self.assertFalse(target.exists)
            self.assertEqual(target.canonical_path, root / "src" / "generated" / "deep" / "new.py")
            with self.assertRaises(ToolError):
                resolve_filesystem_access_targets(
                    "apply_patch",
                    root,
                    (
                        FilesystemTargetRequest(
                            "../outside/new.py",
                            FilesystemAccessOperation.CREATE,
                        ),
                    ),
                )

    def test_structured_permission_requires_every_target_to_match_a_path_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "a.py").write_text("a\n", encoding="utf-8")
            (root / "tests" / "b.py").write_text("b\n", encoding="utf-8")
            plan = resolve_filesystem_access_targets(
                "search_replace",
                root,
                (
                    FilesystemTargetRequest(
                        "src/a.py", FilesystemAccessOperation.UPDATE, must_exist=True
                    ),
                    FilesystemTargetRequest(
                        "tests/b.py", FilesystemAccessOperation.UPDATE, must_exist=True
                    ),
                ),
            )
            manager = PermissionManager(
                mode=PermissionMode.DEFAULT,
                interactive=False,
                rules=(
                    PermissionRule(
                        PermissionEffect.ALLOW,
                        "search_replace",
                        path_pattern="src/*",
                        operation="update",
                    ),
                ),
            )

            mixed = manager.decide_targets("search_replace", plan.targets, side_effecting=True)
            allowed = manager.decide_targets(
                "search_replace", (plan.targets[0],), side_effecting=True
            )

        self.assertEqual(mixed.effect, PermissionEffect.DENY)
        self.assertIn("outside explicit path allow rules", mixed.reason)
        self.assertEqual(allowed.effect, PermissionEffect.ALLOW)

    def test_canonical_target_contract_rejects_invalid_shapes_and_indices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_path = root / "target.py"
            target = FilesystemAccessTarget(
                "target.py",
                target_path,
                root,
                "target.py",
                FilesystemAccessOperation.UPDATE,
                True,
                True,
            )
            plan = FilesystemAccessPlan("search_replace", (target,))

            self.assertIs(plan.target_at(0), target)
            for invalid_index in (True, -1, 1):
                with self.subTest(invalid_index=invalid_index), self.assertRaises(IndexError):
                    plan.target_at(invalid_index)
            with self.assertRaises(ValueError):
                FilesystemAccessPlan("", (target,))
            with self.assertRaises(ValueError):
                FilesystemAccessPlan("tool", ())
            with self.assertRaises(TypeError):
                FilesystemAccessPlan("tool", (object(),))  # type: ignore[tuple-item]

            invalid_requests = (
                ("", FilesystemAccessOperation.READ, False, True),
                ("bad\x00path", FilesystemAccessOperation.READ, False, True),
                ("path", "read", False, True),
                ("path", FilesystemAccessOperation.READ, 1, True),
                ("path", FilesystemAccessOperation.READ, False, 1),
            )
            for values in invalid_requests:
                with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                    FilesystemTargetRequest(*values)  # type: ignore[arg-type]

            invalid_targets = (
                {"requested_path": "", "canonical_path": target_path},
                {"requested_path": "target.py", "canonical_path": "not-a-path"},
                {"requested_path": "target.py", "owning_workspace_root": "not-a-path"},
                {"requested_path": "target.py", "policy_path": ""},
                {"requested_path": "target.py", "operation": "update"},
            )
            for overrides in invalid_targets:
                values = {
                    "requested_path": "target.py",
                    "canonical_path": target_path,
                    "owning_workspace_root": root,
                    "policy_path": "target.py",
                    "operation": FilesystemAccessOperation.UPDATE,
                    "exists": True,
                    "is_primary_workspace": True,
                    **overrides,
                }
                with self.subTest(overrides=overrides), self.assertRaises((TypeError, ValueError)):
                    FilesystemAccessTarget(**values)  # type: ignore[arg-type]

            with self.assertRaises(ValueError):
                FilesystemAccessTarget(
                    "target.py",
                    target_path,
                    root,
                    "target.py",
                    FilesystemAccessOperation.UPDATE,
                    True,
                    True,
                    additional_workspace_root=root,
                )
            with self.assertRaises(ValueError):
                FilesystemAccessTarget(
                    "target.py",
                    target_path,
                    root,
                    "target.py",
                    FilesystemAccessOperation.UPDATE,
                    True,
                    False,
                )

    def test_canonical_resolver_handles_extra_roots_windows_ambiguity_and_delegation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as primary_directory,
            tempfile.TemporaryDirectory() as extra_directory,
        ):
            root = Path(primary_directory)
            extra = Path(extra_directory)
            extra_target = extra / "shared.py"
            extra_target.write_text("shared\n", encoding="utf-8")
            plan = resolve_filesystem_access_targets(
                "read_file",
                root,
                (
                    FilesystemTargetRequest(
                        str(extra_target), FilesystemAccessOperation.READ, must_exist=True
                    ),
                ),
                additional_workspace_roots=(extra,),
            )
            self.assertFalse(plan.targets[0].is_primary_workspace)
            self.assertEqual(plan.targets[0].additional_workspace_root, extra.resolve())
            self.assertEqual(plan.targets[0].policy_path, str(extra_target.resolve()))
            with self.assertRaises(ToolError):
                resolve_filesystem_access_targets(
                    "read_file",
                    root,
                    (FilesystemTargetRequest("shared.py", FilesystemAccessOperation.READ),),
                    additional_workspace_roots=(root / "nested",),
                )

            delegated = resolve_delegated_workspace_path(root, "./remote/../remote.txt")
            self.assertEqual(delegated, root / "remote.txt")
            with self.assertRaises(ToolError):
                resolve_delegated_workspace_path(root, "../outside.txt")

            ambiguous = (
                r"\\?\C:\workspace\file.txt",
                r"\\.\PIPE\name",
                "C:relative",
                "file.txt:stream",
                "CON",
            )
            with patch("neuro_code.infrastructure.workspace.paths.os.name", "nt"):
                for requested in ambiguous:
                    with self.subTest(requested=requested), self.assertRaises(ToolError):
                        resolve_filesystem_access_targets(
                            "read_file",
                            root,
                            (FilesystemTargetRequest(requested, FilesystemAccessOperation.READ),),
                        )

    def test_structured_permission_aggregates_ask_and_preserves_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_path = root / "target.py"
            target_path.write_text("target\n", encoding="utf-8")
            plan = resolve_filesystem_access_targets(
                "search_replace",
                root,
                (
                    FilesystemTargetRequest(
                        "target.py", FilesystemAccessOperation.UPDATE, must_exist=True
                    ),
                ),
            )
            target = plan.targets

            self.assertEqual(
                PermissionManager(mode=PermissionMode.BYPASS)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.ALLOW,
            )
            self.assertEqual(
                PermissionManager(mode=PermissionMode.ACCEPT_EDITS)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.ALLOW,
            )
            self.assertEqual(
                PermissionManager(mode=PermissionMode.DONT_ASK)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.DENY,
            )
            self.assertEqual(
                PermissionManager(interactive=True)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.ASK,
            )
            self.assertEqual(
                PermissionManager()
                .decide_targets("read_file", target, side_effecting=False)
                .effect,
                PermissionEffect.ALLOW,
            )
            ask_rule = PermissionRule(PermissionEffect.ASK, "search_replace", operation="update")
            self.assertEqual(
                PermissionManager(rules=(ask_rule,), interactive=False)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.DENY,
            )
            self.assertEqual(
                PermissionManager(rules=(ask_rule,), interactive=True)
                .decide_targets("search_replace", target, side_effecting=True)
                .effect,
                PermissionEffect.ASK,
            )
            self.assertEqual(
                PermissionManager().decide_targets("read_file", (), side_effecting=False).effect,
                PermissionEffect.DENY,
            )


if __name__ == "__main__":
    unittest.main()
