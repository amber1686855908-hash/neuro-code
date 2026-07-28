"""Tests for repository AGENTS.md instruction discovery."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from neuro_code.adapters.instruction_discovery import FilesystemInstructionDiscovery
from neuro_code.domain.instructions import (
    INSTRUCTION_FILENAME,
    MAX_DIRECTORY_DEPTH,
    MAX_INSTRUCTION_FILES,
    MAX_SINGLE_FILE_BYTES,
    InstructionDiscoveryResult,
    InstructionFile,
    InstructionRejection,
    InstructionRejectionReason,
    compute_instruction_fingerprint,
)
from tests.fakes import EmptyWorkspaceChangeObserver

# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


class TestInstructionFile:
    def test_valid_file(self) -> None:
        f = InstructionFile(relative_path="AGENTS.md", content="hello", depth=0)
        assert f.relative_path == "AGENTS.md"
        assert f.content == "hello"
        assert f.depth == 0

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            InstructionFile(relative_path="", content="x", depth=0)

    def test_negative_depth_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            InstructionFile(relative_path="a.md", content="x", depth=-1)

    def test_nul_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            InstructionFile(relative_path="a\x00b.md", content="x", depth=0)

    def test_c1_control_char_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            InstructionFile(relative_path="a\x85b.md", content="x", depth=0)


class TestInstructionRejection:
    def test_valid_rejection(self) -> None:
        r = InstructionRejection("sub/AGENTS.md", InstructionRejectionReason.FILE_TOO_LARGE)
        assert r.relative_path == "sub/AGENTS.md"
        assert r.reason is InstructionRejectionReason.FILE_TOO_LARGE

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            InstructionRejection("", InstructionRejectionReason.TOO_DEEP)

    def test_control_character_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            InstructionRejection("bad\x01/AGENTS.md", InstructionRejectionReason.READ_ERROR)


class TestInstructionDiscoveryResult:
    def test_empty_result(self) -> None:
        result = InstructionDiscoveryResult(files=(), rejections=(), fingerprint="abc")
        assert result.loaded_count == 0
        assert result.rejected_count == 0
        assert result.total_bytes == 0
        assert result.model_context_text() == ""

    def test_model_context_text(self) -> None:
        f = InstructionFile(relative_path="AGENTS.md", content="Use tabs.", depth=0)
        result = InstructionDiscoveryResult(files=(f,), rejections=(), fingerprint="abc")
        text = result.model_context_text()
        assert "repository-provided instructions" in text
        assert "Use tabs." in text
        assert "[Repository instruction: AGENTS.md]" in text

    def test_instruction_message_is_synthetic_user(self) -> None:
        from neuro_code.domain.messages import Role, SyntheticReason

        f = InstructionFile(relative_path="AGENTS.md", content="Use tabs.", depth=0)
        result = InstructionDiscoveryResult(files=(f,), rejections=(), fingerprint="abc")
        msg = result.instruction_message()
        assert msg.role is Role.USER
        assert msg.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        assert "Use tabs." in msg.content

    def test_total_bytes(self) -> None:
        f1 = InstructionFile(relative_path="AGENTS.md", content="abc", depth=0)
        f2 = InstructionFile(relative_path="sub/AGENTS.md", content="de", depth=1)
        result = InstructionDiscoveryResult(files=(f1, f2), rejections=(), fingerprint="x")
        assert result.total_bytes == 5


class TestFingerprint:
    def test_deterministic(self) -> None:
        files = (InstructionFile("AGENTS.md", "hello", 0),)
        assert compute_instruction_fingerprint(files) == compute_instruction_fingerprint(files)

    def test_different_content_different_fingerprint(self) -> None:
        f1 = (InstructionFile("AGENTS.md", "hello", 0),)
        f2 = (InstructionFile("AGENTS.md", "world", 0),)
        assert compute_instruction_fingerprint(f1) != compute_instruction_fingerprint(f2)

    def test_different_path_different_fingerprint(self) -> None:
        f1 = (InstructionFile("AGENTS.md", "hello", 0),)
        f2 = (InstructionFile("sub/AGENTS.md", "hello", 1),)
        assert compute_instruction_fingerprint(f1) != compute_instruction_fingerprint(f2)

    def test_empty_files_stable(self) -> None:
        fp = compute_instruction_fingerprint(())
        assert len(fp) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------


class TestFilesystemInstructionDiscovery:
    def setup_method(self) -> None:
        self.discovery = FilesystemInstructionDiscovery()

    def test_no_agents_md(self, tmp_path: Path) -> None:
        result = self.discovery.discover(tmp_path)
        assert result.files == ()
        assert result.rejections == ()

    def test_root_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("Use 4-space indent.", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1
        assert result.files[0].relative_path == INSTRUCTION_FILENAME
        assert result.files[0].content == "Use 4-space indent."
        assert result.files[0].depth == 0

    def test_nested_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("root rules", encoding="utf-8")
        sub = tmp_path / "src" / "lib"
        sub.mkdir(parents=True)
        (sub / INSTRUCTION_FILENAME).write_text("lib rules", encoding="utf-8")
        result = self.discovery.discover(tmp_path, target=sub)
        assert result.loaded_count == 2
        assert result.files[0].relative_path == INSTRUCTION_FILENAME
        assert result.files[0].depth == 0
        assert result.files[1].relative_path == "src/lib/AGENTS.md"
        assert result.files[1].depth == 2

    def test_target_none_only_root(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("root", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / INSTRUCTION_FILENAME).write_text("sub", encoding="utf-8")
        result = self.discovery.discover(tmp_path, target=None)
        assert result.loaded_count == 1
        assert result.files[0].content == "root"

    def test_intermediate_directories_included(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("root", encoding="utf-8")
        mid = tmp_path / "a"
        mid.mkdir()
        (mid / INSTRUCTION_FILENAME).write_text("mid", encoding="utf-8")
        deep = mid / "b"
        deep.mkdir()
        (deep / INSTRUCTION_FILENAME).write_text("deep", encoding="utf-8")
        result = self.discovery.discover(tmp_path, target=deep)
        assert result.loaded_count == 3
        assert [f.content for f in result.files] == ["root", "mid", "deep"]

    def test_file_too_large(self, tmp_path: Path) -> None:
        big_content = "x" * (MAX_SINGLE_FILE_BYTES + 1)
        (tmp_path / INSTRUCTION_FILENAME).write_text(big_content, encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejected_count == 1
        assert result.rejections[0].reason is InstructionRejectionReason.FILE_TOO_LARGE

    def test_too_many_files(self, tmp_path: Path) -> None:
        current = tmp_path
        for i in range(MAX_INSTRUCTION_FILES + 1):
            (current / INSTRUCTION_FILENAME).write_text(f"level {i}", encoding="utf-8")
            if i < MAX_INSTRUCTION_FILES:
                current = current / f"d{i}"
                current.mkdir()
        result = self.discovery.discover(tmp_path, target=current)
        assert result.loaded_count == MAX_INSTRUCTION_FILES
        assert any(r.reason is InstructionRejectionReason.TOO_MANY_FILES for r in result.rejections)

    def test_too_deep(self, tmp_path: Path) -> None:
        current = tmp_path
        for i in range(MAX_DIRECTORY_DEPTH + 2):
            current = current / f"d{i}"
            current.mkdir()
        (current / INSTRUCTION_FILENAME).write_text("deep", encoding="utf-8")
        result = self.discovery.discover(tmp_path, target=current)
        assert any(r.reason is InstructionRejectionReason.TOO_DEEP for r in result.rejections)

    def test_invalid_encoding(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_bytes(b"\xff\xfe invalid utf8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.INVALID_ENCODING

    def test_control_characters_c0(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("hello\x01world", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.CONTROL_CHARACTERS

    def test_control_characters_c1(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("hello\x85world", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.CONTROL_CHARACTERS

    def test_control_characters_del(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("hello\x7fworld", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.CONTROL_CHARACTERS

    @pytest.mark.skipif(os.name == "nt", reason="Windows forbids this control path")
    def test_control_character_in_directory_name_is_safely_reported(
        self,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "bad\x01directory"
        target.mkdir()
        (target / INSTRUCTION_FILENAME).write_text("rules", encoding="utf-8")

        result = FilesystemInstructionDiscovery().discover(tmp_path, target=target)

        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.CONTROL_CHARACTERS
        assert "\\u0001" in result.rejections[0].relative_path
        assert "\x01" not in result.rejections[0].relative_path

    def test_tab_newline_allowed(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("col1\tcol2\nline2\r\n", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1

    def test_bom_stripped(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("\ufeffcontent here", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1
        assert result.files[0].content == "content here"

    def test_directory_named_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).mkdir()
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.NOT_A_FILE

    @pytest.mark.skipif(os.name == "nt", reason="symlink escape test requires POSIX")
    def test_symlink_escape(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_agents.md"
        outside.write_text("escaped", encoding="utf-8")
        link = tmp_path / INSTRUCTION_FILENAME
        link.symlink_to(outside)
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.SYMLINK_ESCAPE

    @pytest.mark.skipif(os.name == "nt", reason="symlink within workspace is POSIX-only")
    def test_symlink_within_workspace_rejected(self, tmp_path: Path) -> None:
        """A safe symlink (target within workspace) is still rejected.

        The TOCTOU-safe read uses O_NOFOLLOW and does not follow any
        symlinks, so even a safe symlink is rejected with
        SYMLINK_NOT_SUPPORTED rather than being followed.
        """
        real = tmp_path / "real_agents.md"
        real.write_text("internal", encoding="utf-8")
        link = tmp_path / INSTRUCTION_FILENAME
        link.symlink_to(real)
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejections[0].reason is InstructionRejectionReason.SYMLINK_NOT_SUPPORTED

    @pytest.mark.skipif(os.name == "nt", reason="circular symlink detection is POSIX-only")
    def test_circular_symlink_posix(self, tmp_path: Path) -> None:
        link = tmp_path / INSTRUCTION_FILENAME
        link.symlink_to(link)  # self-referential symlink
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert any(
            r.reason is InstructionRejectionReason.CIRCULAR_SYMLINK
            or r.reason is InstructionRejectionReason.NOT_A_FILE
            for r in result.rejections
        )

    def test_target_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """Target outside workspace must be rejected, not silently clamped."""
        (tmp_path / INSTRUCTION_FILENAME).write_text("root", encoding="utf-8")
        outside = tmp_path.parent / "elsewhere_dir"
        result = self.discovery.discover(tmp_path, target=outside)
        assert result.loaded_count == 0
        assert result.rejected_count >= 1
        assert any(
            r.reason is InstructionRejectionReason.ESCAPES_WORKSPACE for r in result.rejections
        )

    def test_fingerprint_changes_with_content(self, tmp_path: Path) -> None:
        agents = tmp_path / INSTRUCTION_FILENAME
        agents.write_text("version 1", encoding="utf-8")
        r1 = self.discovery.discover(tmp_path)
        agents.write_text("version 2", encoding="utf-8")
        r2 = self.discovery.discover(tmp_path)
        assert r1.fingerprint != r2.fingerprint

    def test_fingerprint_stable_without_changes(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("stable", encoding="utf-8")
        r1 = self.discovery.discover(tmp_path)
        r2 = self.discovery.discover(tmp_path)
        assert r1.fingerprint == r2.fingerprint

    def test_empty_file_loaded(self, tmp_path: Path) -> None:
        (tmp_path / INSTRUCTION_FILENAME).write_text("", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1
        assert result.files[0].content == ""

    def test_toctou_safe_read(self, tmp_path: Path) -> None:
        """Verify that the adapter reads bytes first then checks size (TOCTOU-safe)."""
        # Write a file exactly at the size limit.
        (tmp_path / INSTRUCTION_FILENAME).write_text("x" * MAX_SINGLE_FILE_BYTES, encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1


# ---------------------------------------------------------------------------
# Windows-specific reparse point tests (junctions and NTFS symlinks)
# ---------------------------------------------------------------------------


def _can_create_junction(tmp_path: Path) -> bool:
    """Probe whether directory junctions can be created in this environment.

    Junctions do not require admin privileges on Windows, but they may still
    fail in some sandboxes or when the user lacks the relevant filesystem
    permissions.  Returns True on non-Windows platforms so that the gating
    logic in callers reduces to ``os.name == "nt"``.
    """
    if os.name != "nt":
        return False
    import subprocess

    target = tmp_path / "_junction_probe_target"
    target.mkdir()
    junction = tmp_path / "_junction_probe_link"
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return False
    return junction.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only reparse point tests")
class TestWindowsReparsePoints:
    """Tests for Windows directory junctions and NTFS symlinks.

    On Windows, directory junctions are reparse points that ``is_symlink()``
    reports as False (they use ``IO_REPARSE_TAG_MOUNT_POINT`` rather than
    ``IO_REPARSE_TAG_SYMLINK``).  The adapter must still reject them when
    they would let repository instructions escape the workspace.
    """

    def setup_method(self) -> None:
        self.discovery = FilesystemInstructionDiscovery()

    def test_directory_junction_named_agents_md_rejected(self, tmp_path: Path) -> None:
        """A directory junction named AGENTS.md is a reparse point, rejected."""
        if not _can_create_junction(tmp_path):
            pytest.skip("cannot create directory junctions in this environment")
        import subprocess

        target_dir = tmp_path / "real_dir"
        target_dir.mkdir()
        junction = tmp_path / INSTRUCTION_FILENAME
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target_dir)],
            check=True,
            capture_output=True,
        )
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 0
        assert result.rejected_count == 1
        assert result.rejections[0].reason is InstructionRejectionReason.SYMLINK_NOT_SUPPORTED

    def test_directory_junction_escape_rejected(self, tmp_path: Path) -> None:
        """A subdirectory junction that points outside must reject the escaped AGENTS.md."""
        if not _can_create_junction(tmp_path):
            pytest.skip("cannot create directory junctions in this environment")
        import subprocess

        # Place a real AGENTS.md outside the workspace.
        outside = tmp_path.parent / "neuro_code_junction_outside"
        outside.mkdir(exist_ok=True)
        try:
            (outside / INSTRUCTION_FILENAME).write_text("escaped via junction", encoding="utf-8")
            # Create a junction inside the workspace that points outside.
            junction = tmp_path / "escaped_subdir"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=True,
                capture_output=True,
            )
            # Discover with target inside the junction.  The AGENTS.md found
            # there actually lives outside the workspace and must be rejected.
            result = self.discovery.discover(tmp_path, target=junction)
            assert result.loaded_count == 0
            assert any(
                r.reason is InstructionRejectionReason.ESCAPES_WORKSPACE for r in result.rejections
            )
        finally:
            # Best-effort cleanup: remove the junction from inside the
            # workspace so pytest can clean up tmp_path; remove the outside
            # directory too so we don't leave litter in the parent.
            try:
                if junction.exists():
                    junction.rmdir()
            except OSError:
                pass
            try:
                if outside.exists():
                    import shutil

                    shutil.rmtree(outside, ignore_errors=True)
            except OSError:
                pass

    def test_ntfs_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """An NTFS file symlink that escapes the workspace is rejected.

        This test requires ``SeCreateSymbolicLinkPrivilege`` (admin or Developer
        Mode).  It is skipped gracefully when the privilege is unavailable.
        """
        outside = tmp_path.parent / "neuro_code_symlink_outside.md"
        outside.write_text("escaped", encoding="utf-8")
        link = tmp_path / INSTRUCTION_FILENAME
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("NTFS symlinks require admin or Developer Mode")
        try:
            result = self.discovery.discover(tmp_path)
            assert result.loaded_count == 0
            assert any(
                r.reason is InstructionRejectionReason.SYMLINK_ESCAPE for r in result.rejections
            )
        finally:
            with contextlib.suppress(OSError):
                link.unlink()
            with contextlib.suppress(OSError):
                outside.unlink()


# ---------------------------------------------------------------------------
# Integration: runtime instruction injection
# ---------------------------------------------------------------------------


class TestRuntimeInstructionInjection:
    async def test_instruction_injected_as_synthetic_user(self, tmp_path: Path) -> None:
        """Verify instructions are injected as a synthetic User message, not System."""
        from collections.abc import AsyncIterator, Sequence

        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.messages import Message, Role, SyntheticReason
        from neuro_code.domain.model_context import ModelContext
        from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
        from neuro_code.domain.reasoning import ReasoningEffort
        from neuro_code.domain.tools import ToolDefinition
        from neuro_code.permissions import PermissionManager, PermissionMode
        from neuro_code.tools.registry import ToolRegistry

        (tmp_path / INSTRUCTION_FILENAME).write_text("Use tabs.", encoding="utf-8")
        discovery = FilesystemInstructionDiscovery()

        def provider() -> InstructionDiscoveryResult | None:
            return discovery.discover(tmp_path, target=tmp_path)

        captured_contexts: list[ModelContext] = []

        class ScriptedProvider:
            provider_name = "scripted"
            model_name = "fixture-model"
            context_affinity = "profile-v1:scripted"

            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelEvent]:
                captured_contexts.append(context)
                yield ModelTextDelta("done")
                yield ModelCompleted("stop")

        runtime = AgentRuntime(
            provider=ScriptedProvider(),
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(tmp_path),
            max_steps=1,
            reasoning_effort=ReasoningEffort.HIGH,
            instruction_provider=provider,
        )
        result = await runtime.run("hello")
        assert result.response == "done"
        assert len(captured_contexts) == 1

        items = captured_contexts[0].items
        system_indices = [
            i
            for i, item in enumerate(items)
            if isinstance(item, Message) and item.role is Role.SYSTEM
        ]
        assert len(system_indices) == 1
        sys_idx = system_indices[0]

        # The instruction message should be right after the system message.
        assert sys_idx + 1 < len(items)
        instruction_msg = items[sys_idx + 1]
        assert isinstance(instruction_msg, Message)
        assert instruction_msg.role is Role.USER
        assert instruction_msg.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        assert "Use tabs." in instruction_msg.content
        assert "repository-provided instructions" in instruction_msg.content

        # The system message should NOT contain instruction text.
        sys_msg = items[sys_idx]
        assert isinstance(sys_msg, Message)
        assert "repository-provided instructions" not in sys_msg.model_content()
        assert "Use tabs." not in sys_msg.model_content()

    async def test_no_instruction_provider_no_injection(self, tmp_path: Path) -> None:
        """Without an instruction provider, no synthetic User message is injected."""
        from collections.abc import AsyncIterator, Sequence

        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.messages import Message, SyntheticReason
        from neuro_code.domain.model_context import ModelContext
        from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
        from neuro_code.domain.reasoning import ReasoningEffort
        from neuro_code.domain.tools import ToolDefinition
        from neuro_code.permissions import PermissionManager, PermissionMode
        from neuro_code.tools.registry import ToolRegistry

        captured_contexts: list[ModelContext] = []

        class ScriptedProvider:
            provider_name = "scripted"
            model_name = "fixture-model"
            context_affinity = "profile-v1:scripted"

            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelEvent]:
                captured_contexts.append(context)
                yield ModelTextDelta("done")
                yield ModelCompleted("stop")

        runtime = AgentRuntime(
            provider=ScriptedProvider(),
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(tmp_path),
            max_steps=1,
            reasoning_effort=ReasoningEffort.HIGH,
        )
        result = await runtime.run("hello")
        assert result.response == "done"
        assert len(captured_contexts) == 1

        items = captured_contexts[0].items
        synthetic_msgs = [
            item
            for item in items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(synthetic_msgs) == 0

    async def test_instruction_refreshed_between_calls(self, tmp_path: Path) -> None:
        """Verify that instruction content changes are picked up on the next call."""
        from collections.abc import AsyncIterator, Sequence

        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.messages import Message, SyntheticReason
        from neuro_code.domain.model_context import ModelContext
        from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
        from neuro_code.domain.reasoning import ReasoningEffort
        from neuro_code.domain.tools import ToolDefinition
        from neuro_code.permissions import PermissionManager, PermissionMode
        from neuro_code.tools.registry import ToolRegistry

        agents_file = tmp_path / INSTRUCTION_FILENAME
        agents_file.write_text("version 1", encoding="utf-8")
        discovery = FilesystemInstructionDiscovery()

        def provider() -> InstructionDiscoveryResult | None:
            return discovery.discover(tmp_path, target=tmp_path)

        captured_contexts: list[ModelContext] = []

        class ScriptedProvider:
            provider_name = "scripted"
            model_name = "fixture-model"
            context_affinity = "profile-v1:scripted"

            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelEvent]:
                captured_contexts.append(context)
                yield ModelTextDelta("done")
                yield ModelCompleted("stop")

        runtime = AgentRuntime(
            provider=ScriptedProvider(),
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(tmp_path),
            max_steps=1,
            reasoning_effort=ReasoningEffort.HIGH,
            instruction_provider=provider,
        )
        # First run — instruction contains "version 1".
        await runtime.run("hello")
        assert len(captured_contexts) == 1
        first_instr = [
            item
            for item in captured_contexts[0].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(first_instr) == 1
        assert "version 1" in first_instr[0].content

        # Change the file and run again.
        agents_file.write_text("version 2", encoding="utf-8")
        captured_contexts.clear()
        await runtime.run("hello again")
        assert len(captured_contexts) == 1
        second_instr = [
            item
            for item in captured_contexts[0].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(second_instr) == 1
        assert "version 2" in second_instr[0].content
        assert "version 1" not in second_instr[0].content


# ---------------------------------------------------------------------------
# Integration: InstructionTracker deep-scope discovery
# ---------------------------------------------------------------------------


class TestInstructionTracker:
    """Unit tests for the InstructionTracker.

    The tracker is the session-scoped component that moves the discovery
    target deeper when file-access tools touch a path.  These tests verify
    target movement, subtree isolation, and workspace-boundary clamping
    without spinning up a full AgentRuntime.
    """

    def test_initial_target_is_workspace_root(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )
        assert tracker.target == tmp_path.resolve()
        assert tracker.workspace_root == tmp_path.resolve()

    def test_check_path_file_moves_to_parent(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)
        deep_file = deep_dir / "file.txt"
        deep_file.write_text("x", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )
        tracker.check_path(deep_file)
        assert tracker.target == deep_dir.resolve()

    def test_check_path_directory_moves_to_self(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )
        tracker.check_path(deep_dir)
        assert tracker.target == deep_dir.resolve()

    def test_check_path_outside_workspace_ignored(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        outside = tmp_path.parent / "outside_workspace_dir"
        outside.mkdir(exist_ok=True)
        try:
            discovery = FilesystemInstructionDiscovery()
            tracker = InstructionTracker(
                discovery=discovery,
                workspace_root=tmp_path,
            )
            original_target = tracker.target
            tracker.check_path(outside)
            assert tracker.target == original_target
        finally:
            with contextlib.suppress(OSError):
                outside.rmdir()

    def test_subtree_isolation_sibling_excluded(self, tmp_path: Path) -> None:
        """Moving from src/foo/ to src/bar/ must exclude src/foo/AGENTS.md."""
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        foo_dir = tmp_path / "src" / "foo"
        bar_dir = tmp_path / "src" / "bar"
        foo_dir.mkdir(parents=True)
        bar_dir.mkdir(parents=True)
        (foo_dir / INSTRUCTION_FILENAME).write_text("foo rules", encoding="utf-8")
        (bar_dir / INSTRUCTION_FILENAME).write_text("bar rules", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )

        # Move to foo: result includes foo rules.
        tracker.check_path(foo_dir)
        result_foo = tracker.current_result()
        contents_foo = [f.content for f in result_foo.files]
        assert "foo rules" in contents_foo
        assert "bar rules" not in contents_foo

        # Move to bar: result includes bar rules, NOT foo rules (subtree isolation).
        tracker.check_path(bar_dir)
        result_bar = tracker.current_result()
        contents_bar = [f.content for f in result_bar.files]
        assert "bar rules" in contents_bar
        assert "foo rules" not in contents_bar

    def test_current_result_includes_root_and_deep(self, tmp_path: Path) -> None:
        """When target is deep, result includes AGENTS.md from root to target."""
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        deep_dir = tmp_path / "packages" / "api"
        deep_dir.mkdir(parents=True)
        (tmp_path / INSTRUCTION_FILENAME).write_text("root rules", encoding="utf-8")
        (deep_dir / INSTRUCTION_FILENAME).write_text("deep rules", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )
        tracker.check_path(deep_dir)
        result = tracker.current_result()
        contents = [f.content for f in result.files]
        assert "root rules" in contents
        assert "deep rules" in contents

    def test_current_result_not_cached(self, tmp_path: Path) -> None:
        """current_result() re-runs discovery on each call (no caching)."""
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker

        agents = tmp_path / INSTRUCTION_FILENAME
        agents.write_text("version 1", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
        )
        r1 = tracker.current_result()
        assert r1.files[0].content == "version 1"

        # Change the file and call again — no cache, so fresh content.
        agents.write_text("version 2", encoding="utf-8")
        r2 = tracker.current_result()
        assert r2.files[0].content == "version 2"


# ---------------------------------------------------------------------------
# Integration: AgentRuntime with tracker picks up deep AGENTS.md via tools
# ---------------------------------------------------------------------------


class TestAgentRuntimeDeepScope:
    """End-to-end tests that the InstructionTracker is wired into ToolContext
    and that file-access tools update the discovery target, so the next model
    step's instruction context includes deeper AGENTS.md files.
    """

    async def test_read_file_tool_moves_tracker_target_deeper(self, tmp_path: Path) -> None:
        """read_file on a deep path updates the tracker, so the second model
        step sees both root and deep AGENTS.md instructions."""
        from collections.abc import AsyncIterator, Sequence

        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.domain.messages import Message, SyntheticReason, ToolCall
        from neuro_code.domain.model_context import ModelContext
        from neuro_code.domain.model_events import (
            ModelCompleted,
            ModelEvent,
            ModelTextDelta,
            ModelToolCall,
        )
        from neuro_code.domain.reasoning import ReasoningEffort
        from neuro_code.domain.tools import ToolDefinition
        from neuro_code.permissions import PermissionManager, PermissionMode
        from neuro_code.tools import default_tool_registry

        # Workspace layout:
        #   tmp_path/AGENTS.md            -> "root: use 2-space indent"
        #   tmp_path/src/deep/file.txt   -> "x"
        #   tmp_path/src/deep/AGENTS.md  -> "deep: use 4-space indent"
        (tmp_path / INSTRUCTION_FILENAME).write_text("root: use 2-space indent", encoding="utf-8")
        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)
        (deep_dir / "file.txt").write_text("x", encoding="utf-8")
        (deep_dir / INSTRUCTION_FILENAME).write_text("deep: use 4-space indent", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
            initial_target=tmp_path,
        )

        def instruction_provider() -> InstructionDiscoveryResult | None:
            return tracker.current_result()

        captured_contexts: list[ModelContext] = []

        class ScriptedProvider:
            provider_name = "scripted"
            model_name = "fixture-model"
            context_affinity = "profile-v1:scripted"

            def __init__(self) -> None:
                self._scripts = [
                    # Step 1: call read_file on the deep file.
                    (
                        ModelToolCall(
                            ToolCall("read-1", "read_file", {"path": "src/deep/file.txt"})
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    # Step 2: respond with text.
                    (
                        ModelTextDelta("done"),
                        ModelCompleted("stop"),
                    ),
                ]

            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelEvent]:
                captured_contexts.append(context)
                script = self._scripts.pop(0)
                for event in script:
                    yield event

        runtime = AgentRuntime(
            provider=ScriptedProvider(),
            tools=default_tool_registry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(tmp_path, instruction_tracker=tracker),
            max_steps=5,
            reasoning_effort=ReasoningEffort.HIGH,
            instruction_provider=instruction_provider,
        )
        result = await runtime.run("Read the deep file.")
        assert result.response == "done"
        assert len(captured_contexts) == 2

        # Step 1: tracker target is still root, so only root instructions.
        step1_instr = [
            item
            for item in captured_contexts[0].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(step1_instr) == 1
        assert "root: use 2-space indent" in step1_instr[0].content
        assert "deep: use 4-space indent" not in step1_instr[0].content

        # Step 2: tracker target moved to src/deep/, so both root and deep.
        step2_instr = [
            item
            for item in captured_contexts[1].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(step2_instr) == 1
        assert "root: use 2-space indent" in step2_instr[0].content
        assert "deep: use 4-space indent" in step2_instr[0].content

    async def test_subtree_isolation_in_runtime(self, tmp_path: Path) -> None:
        """When read_file moves from src/foo/ to src/bar/, the deep AGENTS.md
        from src/foo/ is excluded on the next step (subtree isolation)."""
        from collections.abc import AsyncIterator, Sequence

        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.domain.messages import Message, SyntheticReason, ToolCall
        from neuro_code.domain.model_context import ModelContext
        from neuro_code.domain.model_events import (
            ModelCompleted,
            ModelEvent,
            ModelTextDelta,
            ModelToolCall,
        )
        from neuro_code.domain.reasoning import ReasoningEffort
        from neuro_code.domain.tools import ToolDefinition
        from neuro_code.permissions import PermissionManager, PermissionMode
        from neuro_code.tools import default_tool_registry

        foo_dir = tmp_path / "src" / "foo"
        bar_dir = tmp_path / "src" / "bar"
        foo_dir.mkdir(parents=True)
        bar_dir.mkdir(parents=True)
        (foo_dir / INSTRUCTION_FILENAME).write_text("foo rules", encoding="utf-8")
        (bar_dir / INSTRUCTION_FILENAME).write_text("bar rules", encoding="utf-8")
        (foo_dir / "file.txt").write_text("foo file", encoding="utf-8")
        (bar_dir / "file.txt").write_text("bar file", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
            initial_target=tmp_path,
        )

        def instruction_provider() -> InstructionDiscoveryResult | None:
            return tracker.current_result()

        captured_contexts: list[ModelContext] = []

        class ScriptedProvider:
            provider_name = "scripted"
            model_name = "fixture-model"
            context_affinity = "profile-v1:scripted"

            def __init__(self) -> None:
                self._scripts = [
                    # Step 1: read a file in src/foo/.
                    (
                        ModelToolCall(
                            ToolCall("read-1", "read_file", {"path": "src/foo/file.txt"})
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    # Step 2: read a file in src/bar/ (switches subtree).
                    (
                        ModelToolCall(
                            ToolCall("read-2", "read_file", {"path": "src/bar/file.txt"})
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    # Step 3: respond.
                    (
                        ModelTextDelta("done"),
                        ModelCompleted("stop"),
                    ),
                ]

            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelEvent]:
                captured_contexts.append(context)
                script = self._scripts.pop(0)
                for event in script:
                    yield event

        runtime = AgentRuntime(
            provider=ScriptedProvider(),
            tools=default_tool_registry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(tmp_path, instruction_tracker=tracker),
            max_steps=5,
            reasoning_effort=ReasoningEffort.HIGH,
            instruction_provider=instruction_provider,
        )
        result = await runtime.run("Read foo then bar.")
        assert result.response == "done"
        assert len(captured_contexts) == 3

        # Step 2 (after reading src/foo/file.txt): instruction includes "foo rules".
        step2_instr = [
            item
            for item in captured_contexts[1].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(step2_instr) == 1
        assert "foo rules" in step2_instr[0].content
        assert "bar rules" not in step2_instr[0].content

        # Step 3 (after reading src/bar/file.txt): instruction includes "bar rules"
        # but NOT "foo rules" (subtree isolation).
        step3_instr = [
            item
            for item in captured_contexts[2].items
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
        ]
        assert len(step3_instr) == 1
        assert "bar rules" in step3_instr[0].content
        assert "foo rules" not in step3_instr[0].content


# ---------------------------------------------------------------------------
# Integration: path substitution (lstat -> open race detection)
# ---------------------------------------------------------------------------


class TestPathSubstitutionDetection:
    """Tests that the lstat-with-fstat identity comparison detects path
    substitution between lstat and open.
    """

    def setup_method(self) -> None:
        self.discovery = FilesystemInstructionDiscovery()

    def test_path_substitution_between_lstat_and_open_rejected(self, tmp_path: Path) -> None:
        """If the file is replaced between lstat and open, the read is rejected.

        This simulates a TOCTOU attack where a regular file is substituted
        with a different file between the lstat() and os.open() calls.  The
        lstat-with-fstat identity comparison (st_dev/st_ino) should detect
        the mismatch and reject the read.
        """
        import unittest.mock

        original = tmp_path / INSTRUCTION_FILENAME
        original.write_text("original content", encoding="utf-8")
        substitute = tmp_path / "substitute.md"
        substitute.write_text("substituted content", encoding="utf-8")

        real_open = os.open

        def substituting_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            # Open the substitute file instead of the real path
            return real_open(substitute, flags)

        with unittest.mock.patch("os.open", side_effect=substituting_open):
            result = self.discovery.discover(tmp_path)

        # The substitution should be detected via lstat-with-fstat identity mismatch
        assert result.loaded_count == 0
        assert any(r.reason is InstructionRejectionReason.READ_ERROR for r in result.rejections)

    def test_no_substitution_no_false_positive(self, tmp_path: Path) -> None:
        """A normal file (no substitution) should not trigger a false positive."""
        (tmp_path / INSTRUCTION_FILENAME).write_text("normal content", encoding="utf-8")
        result = self.discovery.discover(tmp_path)
        assert result.loaded_count == 1
        assert result.files[0].content == "normal content"


# ---------------------------------------------------------------------------
# Integration: search_replace pre-flight check
# ---------------------------------------------------------------------------


class TestSearchReplacePreFlight:
    """Tests that search_replace aborts when new AGENTS.md files are found
    in the target directory that the model has not yet seen.
    """

    async def test_direct_deep_search_replace_triggers_preflight(self, tmp_path: Path) -> None:
        """search_replace on a deep file with unseen AGENTS.md must NOT
        modify the file -- it should return the instructions instead.
        """
        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.tools.filesystem import SearchReplaceTool

        # Workspace layout:
        #   tmp_path/AGENTS.md           -> "root rules"
        #   tmp_path/src/deep/file.txt   -> "original"
        #   tmp_path/src/deep/AGENTS.md  -> "deep: never modify"
        (tmp_path / INSTRUCTION_FILENAME).write_text("root rules", encoding="utf-8")
        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)
        (deep_dir / "file.txt").write_text("original", encoding="utf-8")
        (deep_dir / INSTRUCTION_FILENAME).write_text("deep: never modify", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
            initial_target=tmp_path,
        )
        tracker.model_context_result()
        tool = SearchReplaceTool()
        context = ToolContext(tmp_path, instruction_tracker=tracker)

        # Direct search_replace on a deep file -- the tracker is at root,
        # so the deep AGENTS.md has NOT been seen by the model yet.
        result = await tool.execute(
            {"path": "src/deep/file.txt", "old": "original", "new": "changed"},
            context,
        )

        # The pre-flight check should have aborted the write.
        assert result.is_error
        assert "project instructions" in result.content.lower()
        assert "never modify" in result.content
        # The file should NOT have been modified.
        assert (deep_dir / "file.txt").read_text(encoding="utf-8") == "original"

    async def test_search_replace_after_read_proceeds(self, tmp_path: Path) -> None:
        """If the model has already read a file in the deep directory (moving
        the tracker there), a subsequent search_replace should proceed because
        the deep AGENTS.md was already in the model instruction context.
        """
        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.tools.filesystem import ReadFileTool, SearchReplaceTool

        (tmp_path / INSTRUCTION_FILENAME).write_text("root rules", encoding="utf-8")
        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)
        (deep_dir / "file.txt").write_text("original", encoding="utf-8")
        (deep_dir / INSTRUCTION_FILENAME).write_text("deep: never modify", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
            initial_target=tmp_path,
        )
        tracker.model_context_result()
        read_tool = ReadFileTool()
        replace_tool = SearchReplaceTool()
        context = ToolContext(tmp_path, instruction_tracker=tracker)

        # Step 1: read the deep file -- this moves the tracker to src/deep/.
        await read_tool.execute({"path": "src/deep/file.txt"}, context)

        # A new model step injects the newly discovered deep instructions.
        tracker.model_context_result()

        # Step 2: now search_replace should proceed -- the tracker is
        # already at src/deep/, so no new AGENTS.md files are discovered.
        result = await replace_tool.execute(
            {"path": "src/deep/file.txt", "old": "original", "new": "changed"},
            context,
        )
        # The write should have proceeded.
        assert "replaced" in result.content
        assert (deep_dir / "file.txt").read_text(encoding="utf-8") == "changed"

    async def test_same_path_instruction_change_after_injection_aborts_write(
        self,
        tmp_path: Path,
    ) -> None:
        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.tools.filesystem import SearchReplaceTool

        instructions = tmp_path / INSTRUCTION_FILENAME
        instructions.write_text("version one", encoding="utf-8")
        target = tmp_path / "file.txt"
        target.write_text("original", encoding="utf-8")
        tracker = InstructionTracker(FilesystemInstructionDiscovery(), tmp_path)
        seen = tracker.model_context_result()
        assert seen.files[0].content == "version one"
        instructions.write_text("version two", encoding="utf-8")

        result = await SearchReplaceTool().execute(
            {"path": "file.txt", "old": "original", "new": "changed"},
            ToolContext(tmp_path, instruction_tracker=tracker),
        )

        assert result.is_error
        assert "version two" in result.content
        assert target.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# Integration: grep recursive search moves tracker for deep matches
# ---------------------------------------------------------------------------


class TestGrepRecursiveTracker:
    """Tests that grep recursive search calls check_path for each matched
    file, moving the tracker to the deepest matched file directory.
    """

    async def test_grep_recursive_moves_tracker_for_deep_matches(self, tmp_path: Path) -> None:
        from neuro_code.application.ports.tools import ToolContext
        from neuro_code.application.runtime.instruction_tracker import InstructionTracker
        from neuro_code.tools.filesystem import GrepTool

        # Workspace layout:
        #   tmp_path/AGENTS.md           -> "root rules"
        #   tmp_path/src/deep/AGENTS.md  -> "deep rules"
        #   tmp_path/src/deep/file.txt   -> "match me"
        #   tmp_path/src/other/AGENTS.md -> "other rules"
        (tmp_path / INSTRUCTION_FILENAME).write_text("root rules", encoding="utf-8")
        deep_dir = tmp_path / "src" / "deep"
        deep_dir.mkdir(parents=True)
        (deep_dir / INSTRUCTION_FILENAME).write_text("deep rules", encoding="utf-8")
        (deep_dir / "file.txt").write_text("match me", encoding="utf-8")
        other_dir = tmp_path / "src" / "other"
        other_dir.mkdir(parents=True)
        (other_dir / INSTRUCTION_FILENAME).write_text("other rules", encoding="utf-8")

        discovery = FilesystemInstructionDiscovery()
        tracker = InstructionTracker(
            discovery=discovery,
            workspace_root=tmp_path,
            initial_target=tmp_path,
        )
        tool = GrepTool()
        context = ToolContext(tmp_path, instruction_tracker=tracker)

        # Grep recursively from root -- should find "match me" in src/deep/file.txt.
        result = await tool.execute({"query": "match", "path": "."}, context)
        assert "match me" in result.content

        # The tracker should have moved to src/deep/ (the matched file dir).
        assert tracker.target == deep_dir.resolve()

        # The current result should include deep AGENTS.md.
        current = tracker.current_result()
        contents = [f.content for f in current.files]
        assert "deep rules" in contents
