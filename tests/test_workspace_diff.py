from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import cast
from unittest.mock import patch

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceDiffFile,
    WorkspaceDiffMove,
    WorkspaceDiffResult,
)
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.infrastructure.tools.filesystem import ApplyPatchTool, SearchReplaceTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from neuro_code.infrastructure.tools.workspace_diff import (
    WorkspaceDiffTool,
    WorkspaceMutationJournal,
)
from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver
from neuro_code.shared.errors import ToolError


def _canonical_posix(path: Path) -> str:
    return path.resolve().as_posix()


class WorkspaceDiffTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_edit_capture_is_targeted_not_repository_scoped(self) -> None:
        """A known edit target must not trigger a full repository walk."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(128):
                (root / f"unrelated-{index:03d}.txt").write_text("noise\n", encoding="utf-8")
            target = root / "target.py"
            target.write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            journal.begin_task()

            import neuro_code.infrastructure.workspace.changes as changes_module

            with patch.object(
                changes_module.os,
                "walk",
                side_effect=AssertionError("targeted capture must not walk the repository"),
            ):
                journal.before_mutation(
                    (root,),
                    tool_name="search_replace",
                    explicit_redactions=(),
                    target_paths=("target.py",),
                )
                target.write_text("after\n", encoding="utf-8")
                journal.after_mutation(
                    (root,),
                    tool_name="search_replace",
                    mutation_metadata=None,
                    explicit_redactions=(),
                    target_paths=("target.py",),
                )

            result = await WorkspaceDiffTool().execute(
                {"paths": ["target.py"]},
                ToolContext(root, workspace_change_journal=journal),
            )
            self.assertIn("+after", result.content)
            self.assertEqual(result.metadata and result.metadata["changed_files"], ["target.py"])

    async def test_workspace_diff_fails_closed_for_capability_and_input_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = WorkspaceDiffTool()
            with self.assertRaisesRegex(ToolError, "local workspace observation"):
                await tool.execute(
                    {},
                    ToolContext(root, client_file_system=cast(ClientFileSystem, object())),
                )
            with self.assertRaisesRegex(ToolError, "unavailable"):
                await tool.execute({}, ToolContext(root))
            context = ToolContext(root, workspace_change_journal=WorkspaceMutationJournal())
            invalid = (
                ({"paths": "foo.py"}, "paths must be a list"),
                ({"paths": ["x"] * 101}, "at most"),
                ({"max_files": True}, "max_files must be"),
                ({"max_diff_bytes": 0}, "max_diff_bytes must be"),
                ({"context_lines": 21}, "context_lines must be"),
            )
            for arguments, message in invalid:
                with self.subTest(arguments=arguments), self.assertRaisesRegex(ToolError, message):
                    await tool.execute(arguments, context)

    async def test_workspace_diff_hides_sensitive_binary_and_large_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sensitive = root / ".env"
            binary = root / "data.bin"
            large = root / "large.txt"
            sensitive.write_text("TOKEN=before\n", encoding="utf-8")
            binary.write_bytes(b"\xffbefore")
            large.write_text("x" * 256_001, encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()
            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            sensitive.write_text("TOKEN=after\n", encoding="utf-8")
            binary.write_bytes(b"\xffafter")
            large.write_text("y" * 256_001, encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            result = await WorkspaceDiffTool().execute({}, context)
            assert result.metadata is not None
            self.assertEqual(result.metadata["modified_files"], [".env", "data.bin", "large.txt"])
            self.assertIn("[sensitive]", result.content)
            self.assertIn("[binary]", result.content)
            self.assertIn("[large]", result.content)
            self.assertNotIn("TOKEN=after", result.content)

    async def test_workspace_diff_marks_unattributed_changes_and_malformed_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = root / "tracked.txt"
            unrelated = root / "unrelated.txt"
            tracked.write_text("a\n", encoding="utf-8")
            unrelated.write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()
            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            tracked.write_text("b\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata={
                    "moved_files": [
                        "not-a-mapping",
                        {"from": 1, "to": "new.txt"},
                        {"from": "tracked.txt", "to": "tracked.txt"},
                        {"from": "tracked.txt", "to": "new.txt"},
                        {"from": "tracked.txt", "to": "new.txt"},
                    ]
                },
                explicit_redactions=(),
            )
            unrelated.write_text("after\n", encoding="utf-8")
            result = await WorkspaceDiffTool().execute({}, context)
            assert result.metadata is not None
            self.assertTrue(result.metadata["unattributed_changes_detected"])
            self.assertTrue(result.metadata["coverage"]["partial"])
            self.assertEqual(
                result.metadata["moved_files"], [{"from": "tracked.txt", "to": "new.txt"}]
            )

            journal.after_mutation(
                (root,),
                tool_name="bash",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            final_result = await WorkspaceDiffTool().execute({}, context)
            assert final_result.metadata is not None
            self.assertTrue(final_result.metadata["coverage"]["partial"])

    async def test_workspace_diff_handles_absolute_moves_filters_links_and_output_limit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "destination.txt"
            source.write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            journal.begin_task()
            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            source.rename(destination)
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata={"moved_files": [{"from": str(source), "to": str(destination)}]},
                explicit_redactions=(),
            )
            context = ToolContext(
                root,
                output_byte_limit=4,
                workspace_change_journal=journal,
            )
            result = await WorkspaceDiffTool().execute({"paths": ["."]}, context)
            self.assertTrue(result.metadata and result.metadata["truncated"])
            self.assertIn("diff truncated", result.content)
            self.assertEqual(
                result.metadata and result.metadata["moved_files"],
                [{"from": "source.txt", "to": "destination.txt"}],
            )

            link = root / "link.txt"
            if hasattr(os, "symlink"):
                with suppress(OSError):
                    link.symlink_to(destination)
                if link.is_symlink():
                    with self.assertRaisesRegex(ToolError, "symlinks"):
                        await WorkspaceDiffTool().execute({"paths": ["link.txt"]}, context)

    def test_workspace_diff_value_objects_reject_invalid_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            WorkspaceDiffMove("", "new.txt")
        with self.assertRaisesRegex(ValueError, "NUL"):
            WorkspaceDiffMove("old.txt", "new\x00.txt")
        with self.assertRaisesRegex(ValueError, "differ"):
            WorkspaceDiffMove("same.txt", "same.txt")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            WorkspaceDiffFile("", "modified", 0, 0, "", False)
        with self.assertRaisesRegex(ValueError, "negative"):
            WorkspaceDiffFile("x", "modified", -1, 0, "", False)
        with self.assertRaisesRegex(ValueError, "boolean"):
            WorkspaceDiffFile("x", "modified", 0, 0, "", 1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "reason"):
            WorkspaceDiffFile("x", "modified", 0, 0, None, False)
        with self.assertRaisesRegex(ValueError, "negative"):
            WorkspaceDiffResult((), (), -1, False, False, False, False, False, False)
        with self.assertRaisesRegex(ValueError, "boolean"):
            WorkspaceDiffResult((), (), 0, False, False, False, False, 1, False)  # type: ignore[arg-type]

    async def test_no_changes_and_task_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)

            journal.begin_task()
            empty = await WorkspaceDiffTool().execute({}, context)
            self.assertIn("no workspace changes", empty.content)
            self.assertEqual(empty.metadata and empty.metadata["file_count"], 0)

            target = root / "task.txt"
            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            target.write_text("agent change\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            self.assertIn("task.txt", (await WorkspaceDiffTool().execute({}, context)).content)

            journal.begin_task()
            isolated = await WorkspaceDiffTool().execute({}, context)
            self.assertNotIn("task.txt", isolated.content)

    async def test_existing_dirty_file_reports_only_agent_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "foo.py"
            target.write_text("original\nuser edit\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            journal.begin_task()
            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            target.write_text("original\nuser edit\nagent edit\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            result = await WorkspaceDiffTool().execute(
                {"paths": ["foo.py"]}, ToolContext(root, workspace_change_journal=journal)
            )
            self.assertIn("+agent edit", result.content)
            self.assertNotIn("+user edit", result.content)
            self.assertEqual(result.metadata and result.metadata["modified_files"], ["foo.py"])

    async def test_paths_are_filtered_and_results_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("b.txt", "a.txt"):
                (root / name).write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()
            for name in ("b.txt", "a.txt"):
                journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
                (root / name).write_text("after\n", encoding="utf-8")
                journal.after_mutation(
                    (root,),
                    tool_name="search_replace",
                    mutation_metadata=None,
                    explicit_redactions=(),
                )

            all_changes = await WorkspaceDiffTool().execute({}, context)
            only_a = await WorkspaceDiffTool().execute({"paths": ["a.txt"]}, context)
            assert all_changes.metadata is not None
            assert only_a.metadata is not None
            self.assertEqual(all_changes.metadata["changed_files"], ["a.txt", "b.txt"])
            self.assertEqual(only_a.metadata["changed_files"], ["a.txt"])
            self.assertNotIn("b.txt", only_a.content)

    async def test_additional_workspace_root_is_reported_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as extra_dir:
            root = Path(directory)
            extra = Path(extra_dir)
            target = extra / "external.txt"
            target.write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(
                root,
                additional_workspace_roots=(extra,),
                workspace_change_journal=journal,
            )
            journal.begin_task()
            journal.before_mutation(
                (root, extra), tool_name="search_replace", explicit_redactions=()
            )
            target.write_text("after\n", encoding="utf-8")
            journal.after_mutation(
                (root, extra),
                tool_name="search_replace",
                mutation_metadata=None,
                explicit_redactions=(),
            )

            result = await WorkspaceDiffTool().execute({}, context)
            # The implementation renders additional roots with slash-separated
            # paths; normalize the spelling returned by tempfile on Windows.
            canonical_extra = _canonical_posix(extra)
            self.assertIn(canonical_extra, result.content)
            assert result.metadata is not None
            self.assertFalse(result.metadata["unattributed_changes_detected"])

    async def test_workspace_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ToolContext(root, workspace_change_journal=WorkspaceMutationJournal())
            with self.assertRaises(ToolError):
                await WorkspaceDiffTool().execute({"paths": ["../outside.txt"]}, context)

    async def test_repeated_structured_edits_keep_first_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "foo.py"
            target.write_text("A\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()

            for before, after in (("A\n", "B\n"), ("B\n", "C\n")):
                self.assertEqual(target.read_text(encoding="utf-8"), before)
                journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
                target.write_text(after, encoding="utf-8")
                journal.after_mutation(
                    (root,),
                    tool_name="search_replace",
                    mutation_metadata=None,
                    explicit_redactions=(),
                )

            result = await WorkspaceDiffTool().execute({}, context)
            self.assertIn("-A", result.content)
            self.assertIn("+C", result.content)
            self.assertNotIn("-B", result.content)
            self.assertTrue(result.metadata and result.metadata["coverage"]["structured_edits"])

    async def test_existing_untracked_file_is_not_reported_as_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "untracked.txt"
            target.write_text("user file\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()
            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            target.write_text("user file\nagent edit\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata=None,
                explicit_redactions=(),
            )

            result = await WorkspaceDiffTool().execute({}, context)
            assert result.metadata is not None
            self.assertEqual(result.metadata["modified_files"], ["untracked.txt"])
            self.assertEqual(result.metadata["added_files"], [])

    async def test_add_delete_move_and_bounded_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.txt"
            old.write_text("old\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()

            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            new = root / "new.txt"
            old.rename(new)
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata={"moved_files": [{"from": "old.txt", "to": "new.txt"}]},
                explicit_redactions=(),
            )
            added = root / "added.txt"
            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            added.write_text("added\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            added.unlink()
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata=None,
                explicit_redactions=(),
            )

            result = await WorkspaceDiffTool().execute({"max_diff_bytes": 8}, context)
            assert result.metadata is not None
            self.assertEqual(result.metadata["moved_files"], [{"from": "old.txt", "to": "new.txt"}])
            self.assertTrue(result.metadata["truncated"])
            self.assertNotIn("added.txt", result.content)

    async def test_apply_patch_and_search_replace_update_journal_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "foo.txt"
            target.write_text("one\ntwo\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(
                root,
                sandbox_profile=SandboxProfile.OFF,
                workspace_change_journal=journal,
            )
            journal.begin_task()

            journal.before_mutation((root,), tool_name="search_replace", explicit_redactions=())
            replaced = await SearchReplaceTool().execute(
                {"path": "foo.txt", "old": "one", "new": "ONE"}, context
            )
            journal.after_mutation(
                (root,),
                tool_name="search_replace",
                mutation_metadata=replaced.metadata,
                explicit_redactions=(),
            )

            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            patched = await ApplyPatchTool().execute(
                {
                    "patch": "*** Begin Patch\n*** Update File: foo.txt\n@@\n-ONE\n+ONE!\n*** End Patch"
                },
                context,
            )
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata=patched.metadata,
                explicit_redactions=(),
            )
            result = await WorkspaceDiffTool().execute({}, context)
            self.assertIn("-one", result.content)
            self.assertIn("+ONE!", result.content)

            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            with self.assertRaises(ToolError):
                await ApplyPatchTool().execute(
                    {
                        "patch": "*** Begin Patch\n*** Update File: foo.txt\n@@\n-missing\n+broken\n*** End Patch"
                    },
                    context,
                )
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            self.assertNotIn("+broken", (await WorkspaceDiffTool().execute({}, context)).content)

    async def test_bash_and_redaction_report_limited_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "secret.txt"
            target.write_text("before\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            journal.begin_task()
            journal.before_mutation(
                (root,), tool_name="bash", explicit_redactions=("fixture-secret",)
            )
            target.write_text("fixture-secret\n", encoding="utf-8")
            journal.after_mutation(
                (root,),
                tool_name="bash",
                mutation_metadata=None,
                explicit_redactions=("fixture-secret",),
            )
            result = await WorkspaceDiffTool().execute(
                {},
                ToolContext(
                    root,
                    redaction_values=("fixture-secret",),
                    workspace_change_journal=journal,
                ),
            )
            assert result.metadata is not None
            self.assertTrue(result.metadata["coverage"]["partial"])
            self.assertNotIn("fixture-secret", result.content)

    async def test_apply_patch_rollback_does_not_leave_files_or_journal_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "a.txt"
            target.write_text("a\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            journal.begin_task()
            journal.before_mutation((root,), tool_name="apply_patch", explicit_redactions=())
            with self.assertRaises(ToolError):
                await ApplyPatchTool().execute(
                    {
                        "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n-a\n+b\n*** Add File: b.txt\n+bad\n*** Update File: missing.txt\n@@\n-x\n+y\n*** End Patch"
                    },
                    context,
                )
            journal.after_mutation(
                (root,),
                tool_name="apply_patch",
                mutation_metadata=None,
                explicit_redactions=(),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "a\n")
            result = await WorkspaceDiffTool().execute({}, context)
            self.assertEqual(result.metadata and result.metadata["file_count"], 0)

    async def test_tool_executor_records_structured_edit_in_task_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "foo.txt"
            target.write_text("before\nafter\n", encoding="utf-8")
            journal = WorkspaceMutationJournal()
            context = ToolContext(root, workspace_change_journal=journal)
            context_builder = ContextBuilder(
                reasoning_effort=ReasoningEffort.HIGH,
                interaction_mode=InteractionMode.NORMAL,
                plan=None,
                instruction_provider=None,
                skill_provider=None,
            )
            executor = ToolExecutor(
                tools=ToolRegistry([SearchReplaceTool(), WorkspaceDiffTool()]),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                approver=None,
                tool_context=context,
                session_store=None,
                workspace_change_observer=FilesystemWorkspaceChangeObserver(),
                context_builder=context_builder,
            )
            events: list[object] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                events.append((kind, data))
                return AgentEvent.create(len(events), kind, data)

            journal.begin_task()
            await executor.execute(
                ToolCall(
                    "call-1",
                    "search_replace",
                    {"path": "foo.txt", "old": "before", "new": "changed"},
                ),
                [],
                [],
                emit,
                None,
            )
            result = await WorkspaceDiffTool().execute({}, context)
            self.assertIn("+changed", result.content)
            self.assertTrue(result.metadata and result.metadata["coverage"]["structured_edits"])


if __name__ == "__main__":
    unittest.main()
