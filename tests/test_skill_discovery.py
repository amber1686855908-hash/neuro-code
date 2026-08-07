"""Tests for read-only skill file discovery.

Covers the domain model (SkillInfo, frontmatter parsing, fingerprint),
the filesystem adapter (discovery, symlink rejection, size/depth/count
limits, deduplication, scope ordering), the session tracker, and the
AgentRuntime injection of skill listings as synthetic messages.

提供只读技能文件发现的测试,覆盖领域模型、frontmatter、文件系统适配器、跟踪器和 AgentRuntime 注入.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason
from neuro_code.domain.workspace.skills import (
    MAX_NAME_LEN,
    MAX_SINGLE_SKILL_BYTES,
    MAX_SKILL_ANCESTOR_DEPTH,
    SKILL_CONFIG_DIRS,
    SKILL_FILENAME,
    SKILL_SUBDIR,
    SkillDiscoveryResult,
    SkillInfo,
    SkillRejection,
    SkillRejectionReason,
    SkillScope,
    compute_skill_fingerprint,
    is_valid_skill_name,
    normalize_skill_name,
    parse_frontmatter,
)
from neuro_code.infrastructure.workspace.skills import FilesystemSkillDiscovery
from tests.fakes import EmptyWorkspaceChangeObserver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep default USER-scope discovery independent of the developer machine.

    验证默认 USER 范围发现不依赖开发者机器环境."""
    user_home = tmp_path / "isolated-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: user_home)


def _make_workspace(tmp_path: Path) -> Path:
    """Create a workspace root directory and return it.

    创建并返回一个工作区根目录."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _make_skill(
    workspace: Path,
    config_dir: str,
    skill_dir_name: str,
    content: str | None = None,
) -> Path:
    """Create a SKILL.md file inside a skills directory.

    Returns the path to the SKILL.md file.

    在 skills 目录中创建一个 SKILL.md 文件,并返回该文件路径.
    """
    skill_dir = workspace / config_dir / SKILL_SUBDIR / skill_dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / SKILL_FILENAME
    if content is None:
        content = (
            "---\n"
            f"name: {skill_dir_name}\n"
            f"description: Skill {skill_dir_name}\n"
            "---\n\n"
            f"# {skill_dir_name}\n\nBody text.\n"
        )
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


# ---------------------------------------------------------------------------
# Domain: SkillInfo
# ---------------------------------------------------------------------------


class TestSkillInfo:
    def test_construction(self) -> None:
        info = SkillInfo(
            name="commit",
            description="Create a git commit",
            when_to_use="User says commit",
            relative_path=".neuro/skills/commit/SKILL.md",
            scope=SkillScope.LOCAL,
            depth=0,
        )
        assert info.name == "commit"
        assert info.description == "Create a git commit"
        assert info.when_to_use == "User says commit"
        assert info.scope is SkillScope.LOCAL

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            SkillInfo(
                name="",
                description="",
                when_to_use=None,
                relative_path=".neuro/skills/x/SKILL.md",
                scope=SkillScope.LOCAL,
                depth=0,
            )

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="relative path must not be empty"):
            SkillInfo(
                name="commit",
                description="",
                when_to_use=None,
                relative_path="",
                scope=SkillScope.LOCAL,
                depth=0,
            )

    def test_nul_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            SkillInfo(
                name="commit",
                description="",
                when_to_use=None,
                relative_path="bad\x00path",
                scope=SkillScope.LOCAL,
                depth=0,
            )

    def test_negative_depth_rejected(self) -> None:
        with pytest.raises(ValueError, match="depth must be non-negative"):
            SkillInfo(
                name="commit",
                description="",
                when_to_use=None,
                relative_path=".neuro/skills/x/SKILL.md",
                scope=SkillScope.LOCAL,
                depth=-1,
            )

    def test_control_char_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            SkillInfo(
                name="commit",
                description="",
                when_to_use=None,
                relative_path=".neuro/skills/\x01/SKILL.md",
                scope=SkillScope.LOCAL,
                depth=0,
            )

    @pytest.mark.parametrize(
        "relative_path",
        ["../../outside/SKILL.md", "/absolute/SKILL.md", r"C:\outside\SKILL.md"],
    )
    def test_non_relative_posix_path_rejected(self, relative_path: str) -> None:
        with pytest.raises(ValueError, match="relative POSIX"):
            SkillInfo(
                name="commit",
                description="",
                when_to_use=None,
                relative_path=relative_path,
                scope=SkillScope.LOCAL,
                depth=0,
            )


# ---------------------------------------------------------------------------
# Domain: SkillRejection
# ---------------------------------------------------------------------------


class TestSkillRejection:
    def test_construction(self) -> None:
        rej = SkillRejection("foo/SKILL.md", SkillRejectionReason.FILE_TOO_LARGE)
        assert rej.relative_path == "foo/SKILL.md"
        assert rej.reason is SkillRejectionReason.FILE_TOO_LARGE

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="rejection path"):
            SkillRejection("", SkillRejectionReason.READ_ERROR)

    def test_control_character_in_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            SkillRejection("bad\x01/SKILL.md", SkillRejectionReason.READ_ERROR)


# ---------------------------------------------------------------------------
# Domain: SkillDiscoveryResult
# ---------------------------------------------------------------------------


class TestSkillDiscoveryResult:
    def test_empty_result(self) -> None:
        result = SkillDiscoveryResult(
            files=(),
            rejections=(),
            fingerprint="abc123",
        )
        assert result.loaded_count == 0
        assert result.rejected_count == 0
        assert result.model_context_text() == ""

    def test_model_context_text_lists_skills(self) -> None:
        result = SkillDiscoveryResult(
            files=(
                SkillInfo(
                    name="commit",
                    description="Create a git commit",
                    when_to_use="User says commit",
                    relative_path=".neuro/skills/commit/SKILL.md",
                    scope=SkillScope.LOCAL,
                    depth=0,
                ),
                SkillInfo(
                    name="review",
                    description="Code review",
                    when_to_use=None,
                    relative_path=".neuro/skills/review/SKILL.md",
                    scope=SkillScope.LOCAL,
                    depth=0,
                ),
            ),
            rejections=(),
            fingerprint="abc",
        )
        text = result.model_context_text()
        assert "- commit: Create a git commit (when to use: User says commit)" in text
        assert "- review: Code review" in text
        assert "when to use" not in text.split("review")[1]

    def test_skill_message(self) -> None:
        result = SkillDiscoveryResult(
            files=(
                SkillInfo(
                    name="commit",
                    description="Create a git commit",
                    when_to_use=None,
                    relative_path=".neuro/skills/commit/SKILL.md",
                    scope=SkillScope.LOCAL,
                    depth=0,
                ),
            ),
            rejections=(),
            fingerprint="abc",
        )
        msg = result.skill_message()
        assert msg.role is Role.USER
        assert msg.synthetic_reason is SyntheticReason.AVAILABLE_SKILLS
        assert "commit" in msg.content


# ---------------------------------------------------------------------------
# Domain: normalize_skill_name / is_valid_skill_name
# ---------------------------------------------------------------------------


class TestSkillNameNormalization:
    def test_already_valid(self) -> None:
        assert normalize_skill_name("commit") == "commit"
        assert normalize_skill_name("my-skill-123") == "my-skill-123"

    def test_uppercase_lowered(self) -> None:
        assert normalize_skill_name("Commit") == "commit"
        assert normalize_skill_name("MySkill") == "myskill"

    def test_underscores_to_hyphens(self) -> None:
        assert normalize_skill_name("my_skill_name") == "my-skill-name"
        assert normalize_skill_name("narrate_crash_video") == "narrate-crash-video"

    def test_consecutive_hyphens_collapsed(self) -> None:
        assert normalize_skill_name("no--double") == "no-double"
        assert normalize_skill_name("a___b") == "a-b"

    def test_leading_trailing_hyphens_trimmed(self) -> None:
        assert normalize_skill_name("-leading") == "leading"
        assert normalize_skill_name("trailing-") == "trailing"

    def test_dots_become_hyphens(self) -> None:
        assert normalize_skill_name("tool-v1.2") == "tool-v1-2"

    def test_non_ascii_replaced(self) -> None:
        result = normalize_skill_name("日本語")
        assert not result  # All non-ASCII → hyphens → trimmed to empty

    def test_is_valid_skill_name(self) -> None:
        assert is_valid_skill_name("commit")
        assert is_valid_skill_name("my-skill-123")
        assert not is_valid_skill_name("")
        assert not is_valid_skill_name("-leading")
        assert not is_valid_skill_name("trailing-")
        assert not is_valid_skill_name("no--double")
        assert not is_valid_skill_name("a" * (MAX_NAME_LEN + 1))
        assert not is_valid_skill_name("UPPERCASE")
        assert not is_valid_skill_name("with space")


# ---------------------------------------------------------------------------
# Domain: parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic_frontmatter(self) -> None:
        content = "---\nname: commit\ndescription: Create a commit\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.name == "commit"
        assert result.description == "Create a commit"
        assert result.when_to_use is None
        assert result.has_user_specified_description

    def test_when_to_use_kebab(self) -> None:
        content = (
            "---\nname: deploy\ndescription: Deploy\n"
            "when-to-use: User says deploy or ship it\n---\n\nBody"
        )
        result = parse_frontmatter(content)
        assert result is not None
        assert result.when_to_use == "User says deploy or ship it"

    def test_when_to_use_snake_case_alias(self) -> None:
        content = "---\nname: deploy\ndescription: Deploy\nwhen_to_use: trigger\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.when_to_use == "trigger"

    def test_no_frontmatter(self) -> None:
        content = "# My Skill\n\nJust a body."
        result = parse_frontmatter(content, fallback_name="my-skill")
        assert result is None

    def test_fallback_name_used(self) -> None:
        content = "---\nname: 日本語\ndescription: x\n---\n\nBody"
        result = parse_frontmatter(content, fallback_name="validdir")
        assert result is not None
        assert result.name == "validdir"

    def test_quoted_value_with_colon(self) -> None:
        content = '---\nname: deploy\ndescription: "Deploy: push to prod"\n---\n\nBody'
        result = parse_frontmatter(content)
        assert result is not None
        assert result.description == "Deploy: push to prod"

    def test_single_quoted_value(self) -> None:
        content = "---\nname: deploy\ndescription: 'Single quoted'\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.description == "Single quoted"

    def test_inline_comment_stripped(self) -> None:
        content = "---\nname: commit\ndescription: Does X # internal note\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.description == "Does X"

    def test_empty_description(self) -> None:
        content = "---\nname: commit\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.description == ""
        assert not result.has_user_specified_description

    def test_underscore_name_normalized(self) -> None:
        content = "---\nname: narrate_crash_video\ndescription: x\n---\n\nBody"
        result = parse_frontmatter(content)
        assert result is not None
        assert result.name == "narrate-crash-video"

    def test_missing_closing_delimiter(self) -> None:
        content = "---\nname: commit\ndescription: x\n"
        result = parse_frontmatter(content)
        assert result is None

    def test_delimiters_must_be_complete_lines(self) -> None:
        content = "----\nname: commit\n---not-a-delimiter\nBody"
        assert parse_frontmatter(content, fallback_name="commit") is None


# ---------------------------------------------------------------------------
# Domain: fingerprint
# ---------------------------------------------------------------------------


class TestSkillFingerprint:
    def test_deterministic(self) -> None:
        files = (
            SkillInfo(
                name="commit",
                description="Create a commit",
                when_to_use=None,
                relative_path=".neuro/skills/commit/SKILL.md",
                scope=SkillScope.LOCAL,
                depth=0,
            ),
        )
        assert compute_skill_fingerprint(files) == compute_skill_fingerprint(files)

    def test_different_content_different_fingerprint(self) -> None:
        f1 = (SkillInfo("a", "desc1", None, "p1", SkillScope.LOCAL, 0),)
        f2 = (SkillInfo("a", "desc2", None, "p1", SkillScope.LOCAL, 0),)
        assert compute_skill_fingerprint(f1) != compute_skill_fingerprint(f2)

    def test_empty_files(self) -> None:
        fp = compute_skill_fingerprint(())
        assert len(fp) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Adapter: basic discovery
# ---------------------------------------------------------------------------


class TestFilesystemSkillDiscovery:
    def test_no_skills_directory(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert result.rejected_count == 0

    def test_single_skill(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.name == "commit"
        assert skill.description == "Skill commit"
        assert skill.scope is SkillScope.LOCAL

    def test_multiple_config_dirs(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        _make_skill(workspace, ".agents", "review")
        _make_skill(workspace, ".claude", "deploy")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        names = {f.name for f in result.files}
        assert names == {"commit", "review", "deploy"}

    def test_nested_skills(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # Create a nested skill: .neuro/skills/nested/deep/SKILL.md
        skill_dir = workspace / ".neuro" / "skills" / "nested" / "deep"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deep-skill\ndescription: Nested\n---\n\nBody",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "deep-skill"
        assert result.files[0].depth == 1

    def test_lexicographic_order(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        for name in ("zeta", "alpha", "mid"):
            _make_skill(workspace, ".neuro", name, content=f"---\nname: {name}\n---\n\nBody")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        names = [f.name for f in result.files]
        assert names == ["alpha", "mid", "zeta"]

    def test_dedup_by_name_first_seen_wins(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # .neuro has priority over .agents
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: From .neuro\n---\n\nBody",
        )
        _make_skill(
            workspace,
            ".agents",
            "commit",
            content="---\nname: commit\ndescription: From .agents\n---\n\nBody",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].description == "From .neuro"


# ---------------------------------------------------------------------------
# Adapter: frontmatter fallbacks
# ---------------------------------------------------------------------------


class TestFrontmatterFallbacks:
    def test_no_frontmatter_uses_dir_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "simple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# Simple Skill\n\nJust a body, no frontmatter.",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "simple"

    def test_description_fallback_from_body(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "nodesc"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: nodesc\n---\n\n# Title\n\nDoes a real thing.\n\n## Section",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "nodesc"
        assert result.files[0].description == "Does a real thing."

    def test_description_fallback_skips_headings(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "heading-only"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: heading-only\n---\n\n# Only A Title\n",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.files[0].description == "heading-only"

    def test_description_fallback_skips_lists_and_tables(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "table-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: table-skill\n---\n\n| Col | Val |\n|---|---|\n| a | b |\n\n- Item 1\n- Item 2\n\nReal description here.\n",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.files[0].description == "Real description here."


# ---------------------------------------------------------------------------
# Adapter: limits and rejection
# ---------------------------------------------------------------------------


class TestSkillDiscoveryLimits:
    def test_file_too_large(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "big"
        skill_dir.mkdir(parents=True)
        content = "---\nname: big\ndescription: x\n---\n\n" + "x" * (MAX_SINGLE_SKILL_BYTES + 1)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert result.rejected_count == 1
        assert result.rejections[0].reason is SkillRejectionReason.FILE_TOO_LARGE

    def test_total_too_large(self, tmp_path: Path) -> None:
        from neuro_code.domain.workspace.skills import MAX_SKILL_FILES, MAX_TOTAL_SKILL_BYTES

        workspace = _make_workspace(tmp_path)
        # Create skills that collectively exceed MAX_TOTAL_SKILL_BYTES.
        # Each skill is ~64 KiB, so we need enough to exceed 512 KiB.
        per_skill = MAX_SINGLE_SKILL_BYTES - 100
        num_needed = (MAX_TOTAL_SKILL_BYTES // per_skill) + 2
        if num_needed > MAX_SKILL_FILES:
            num_needed = MAX_SKILL_FILES
        for i in range(num_needed):
            d = workspace / ".neuro" / "skills" / f"skill-{i:03d}"
            d.mkdir(parents=True)
            content = f"---\nname: skill-{i:03d}\ndescription: x\n---\n\n" + "y" * per_skill
            (d / "SKILL.md").write_text(content, encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        # Some loaded, some rejected for total-too-large or too-many-files
        assert result.loaded_count > 0
        if result.rejected_count > 0:
            reasons = {r.reason for r in result.rejections}
            assert (
                SkillRejectionReason.TOTAL_TOO_LARGE in reasons
                or SkillRejectionReason.TOO_MANY_FILES in reasons
            )

    def test_invalid_encoding(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "badenc"
        skill_dir.mkdir(parents=True)
        # Write invalid UTF-8 bytes
        (skill_dir / "SKILL.md").write_bytes(b"---\nname: badenc\n---\n\n\xff\xfe\x00")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert any(r.reason is SkillRejectionReason.INVALID_ENCODING for r in result.rejections)

    def test_control_characters_rejected(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "ctrl"
        skill_dir.mkdir(parents=True)
        # Write content with a control character (BEL = 0x07)
        content = "---\nname: ctrl\ndescription: x\n---\n\nBody\x07text"
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert any(r.reason is SkillRejectionReason.CONTROL_CHARACTERS for r in result.rejections)

    def test_too_many_files(self, tmp_path: Path) -> None:
        from neuro_code.domain.workspace.skills import MAX_SKILL_FILES

        workspace = _make_workspace(tmp_path)
        for i in range(MAX_SKILL_FILES + 5):
            d = workspace / ".neuro" / "skills" / f"skill-{i:03d}"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: skill-{i:03d}\ndescription: x\n---\n\nBody",
                encoding="utf-8",
            )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == MAX_SKILL_FILES
        assert result.rejected_count == 5
        assert all(r.reason is SkillRejectionReason.TOO_MANY_FILES for r in result.rejections)

    def test_skills_dir_is_file_not_scanned(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # Create .neuro/skills as a file, not a directory
        (workspace / ".neuro").mkdir()
        (workspace / ".neuro" / "skills").write_text("not a directory", encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0

    def test_walk_depth_limit(self, tmp_path: Path) -> None:
        from neuro_code.domain.workspace.skills import MAX_SKILL_WALK_DEPTH

        workspace = _make_workspace(tmp_path)
        # Create a deeply nested skill beyond the walk depth
        parts = [workspace / ".neuro" / "skills"]
        for i in range(MAX_SKILL_WALK_DEPTH + 3):
            parts.append(parts[-1] / f"level-{i}")
        deep_dir = parts[-1]
        deep_dir.mkdir(parents=True)
        (deep_dir / "SKILL.md").write_text(
            "---\nname: deep\ndescription: x\n---\n\nBody",
            encoding="utf-8",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        # The deeply nested skill should NOT be discovered
        assert result.loaded_count == 0

    def test_directory_named_skill_md_rejected(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # Create a directory named SKILL.md (not a file)
        skill_dir = workspace / ".neuro" / "skills" / "dirskill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").mkdir()
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert any(r.reason is SkillRejectionReason.NOT_A_FILE for r in result.rejections)

    def test_directory_entry_limit_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import neuro_code.infrastructure.workspace.skills as adapter

        workspace = _make_workspace(tmp_path)
        for name in ("alpha", "beta", "gamma"):
            _make_skill(workspace, ".neuro", name)
        monkeypatch.setattr(adapter, "MAX_SKILL_DIRECTORY_ENTRIES", 2)

        result = FilesystemSkillDiscovery().discover(workspace)

        assert result.loaded_count == 0
        assert any(
            rejection.reason is SkillRejectionReason.TOO_MANY_ENTRIES
            for rejection in result.rejections
        )

    def test_candidate_limit_stops_the_walk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import neuro_code.infrastructure.workspace.skills as adapter

        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "alpha")
        _make_skill(workspace, ".neuro", "beta")
        monkeypatch.setattr(adapter, "MAX_SKILL_CANDIDATES", 1)

        result = FilesystemSkillDiscovery().discover(workspace)

        assert [skill.name for skill in result.files] == ["alpha"]
        assert any(
            rejection.reason is SkillRejectionReason.TOO_MANY_FILES
            for rejection in result.rejections
        )

    def test_ancestor_walk_is_bounded_but_keeps_workspace_defaults(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "root-default")
        current = workspace
        first_child: Path | None = None
        for index in range(MAX_SKILL_ANCESTOR_DEPTH + 2):
            current = current / f"d{index}"
            current.mkdir()
            first_child = first_child or current
        assert first_child is not None
        _make_skill(first_child, ".neuro", "omitted-middle")

        result = FilesystemSkillDiscovery().discover(workspace, target=current)

        assert [skill.name for skill in result.files] == ["root-default"]
        assert any(
            rejection.reason is SkillRejectionReason.TOO_DEEP for rejection in result.rejections
        )

    @pytest.mark.skipif(os.name == "nt", reason="Windows forbids this control path")
    def test_control_character_in_skill_path_is_safely_reported(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "bad\x01name")

        result = FilesystemSkillDiscovery().discover(workspace)

        assert result.loaded_count == 0
        rejection = result.rejections[0]
        assert rejection.reason is SkillRejectionReason.CONTROL_CHARACTERS
        assert "\\u0001" in rejection.relative_path
        assert "\x01" not in rejection.relative_path


# ---------------------------------------------------------------------------
# Adapter: symlink rejection (POSIX-only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS symlinks require admin or Developer Mode",
)
class TestSkillSymlinkRejection:
    def test_symlink_skill_md_rejected(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "linked"
        skill_dir.mkdir(parents=True)
        # Create a symlink SKILL.md → outside file
        target = tmp_path / "outside.txt"
        target.write_text("escaped content", encoding="utf-8")
        link = skill_dir / "SKILL.md"
        os.symlink(target, link)
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert any(
            r.reason
            in (SkillRejectionReason.SYMLINK_ESCAPE, SkillRejectionReason.SYMLINK_NOT_SUPPORTED)
            for r in result.rejections
        )

    def test_circular_symlink_rejected(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "circular"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        os.symlink(link, link)  # Self-referencing symlink
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0
        assert any(r.reason is SkillRejectionReason.CIRCULAR_SYMLINK for r in result.rejections)

    def test_circular_directory_symlink_is_not_followed(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skills_dir = workspace / ".neuro" / "skills"
        skills_dir.mkdir(parents=True)
        link = skills_dir / "loop"
        os.symlink(link, link)

        result = FilesystemSkillDiscovery().discover(workspace)

        assert result.loaded_count == 0
        assert any(
            rejection.reason is SkillRejectionReason.CIRCULAR_SYMLINK
            for rejection in result.rejections
        )


# ---------------------------------------------------------------------------
# Adapter: config directory priority
# ---------------------------------------------------------------------------


class TestConfigDirPriority:
    def test_config_dirs_order(self) -> None:
        assert SKILL_CONFIG_DIRS == (".neuro", ".agents", ".claude")

    def test_neuro_priority_over_agents(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # Both .neuro and .agents have a skill named "commit"
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: From neuro\n---\n\nBody",
        )
        _make_skill(
            workspace,
            ".agents",
            "commit",
            content="---\nname: commit\ndescription: From agents\n---\n\nBody",
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].description == "From neuro"


# ---------------------------------------------------------------------------
# Runtime: SkillTracker
# ---------------------------------------------------------------------------


class TestSkillTracker:
    def test_current_result_reruns_discovery(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        result = tracker.current_result()
        assert result.loaded_count == 0

        # Add a skill file and check that the next call picks it up
        _make_skill(workspace, ".neuro", "commit")
        result = tracker.current_result()
        assert result.loaded_count == 1
        assert result.files[0].name == "commit"

    def test_workspace_root_property(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        assert tracker.workspace_root == workspace.resolve()


# ---------------------------------------------------------------------------
# Runtime: AgentRuntime injection
# ---------------------------------------------------------------------------


class TestAgentRuntimeSkillInjection:
    def test_skill_listing_injected_as_synthetic_message(self) -> None:
        """Verify that skill_provider results are injected as synthetic messages.

        验证 skill_provider 结果会作为合成消息注入."""
        from neuro_code.domain.workspace.skills import SkillDiscoveryResult

        skill_result = SkillDiscoveryResult(
            files=(
                SkillInfo(
                    name="commit",
                    description="Create a commit",
                    when_to_use=None,
                    relative_path=".neuro/skills/commit/SKILL.md",
                    scope=SkillScope.LOCAL,
                    depth=0,
                ),
            ),
            rejections=(),
            fingerprint="abc",
        )

        def skill_provider() -> SkillDiscoveryResult | None:
            return skill_result

        # Build a minimal AgentRuntime mock to test the injection logic.
        # We test _model_items_with_reasoning_guidance directly.
        from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
        from neuro_code.application.ports.model import ModelProvider
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.conversation.messages import Role
        from neuro_code.infrastructure.tools.registry import ToolRegistry

        provider = MagicMock(spec=ModelProvider)
        permissions = PermissionManager(mode=PermissionMode.BYPASS)
        tools = ToolRegistry()
        tool_context = MagicMock()
        runtime = AgentRuntime(
            provider=provider,
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=permissions,
            tool_context=tool_context,
            skill_provider=skill_provider,
        )

        items = (Message(Role.SYSTEM, "You are a coding agent."),)
        rendered = runtime._model_items_with_reasoning_guidance(items)
        # System + skill message = 2 items
        assert len(rendered) == 2
        skill_msg = rendered[1]
        assert isinstance(skill_msg, Message)
        assert skill_msg.synthetic_reason is SyntheticReason.AVAILABLE_SKILLS
        assert "commit" in skill_msg.content

    def test_no_skills_no_injection(self) -> None:
        from unittest.mock import MagicMock

        from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
        from neuro_code.application.ports.model import ModelProvider
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.conversation.messages import Role
        from neuro_code.domain.workspace.skills import SkillDiscoveryResult
        from neuro_code.infrastructure.tools.registry import ToolRegistry

        def skill_provider() -> SkillDiscoveryResult | None:
            return SkillDiscoveryResult(files=(), rejections=(), fingerprint="empty")

        provider = MagicMock(spec=ModelProvider)
        permissions = PermissionManager(mode=PermissionMode.BYPASS)
        tools = ToolRegistry()
        tool_context = MagicMock()
        runtime = AgentRuntime(
            provider=provider,
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=permissions,
            tool_context=tool_context,
            skill_provider=skill_provider,
        )

        items = (Message(Role.SYSTEM, "You are a coding agent."),)
        rendered = runtime._model_items_with_reasoning_guidance(items)
        # Only the system message, no skill message
        assert len(rendered) == 1

    def test_skill_provider_none_no_injection(self) -> None:
        from unittest.mock import MagicMock

        from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
        from neuro_code.application.ports.model import ModelProvider
        from neuro_code.application.runtime.agent import AgentRuntime
        from neuro_code.domain.conversation.messages import Role
        from neuro_code.infrastructure.tools.registry import ToolRegistry

        provider = MagicMock(spec=ModelProvider)
        permissions = PermissionManager(mode=PermissionMode.BYPASS)
        tools = ToolRegistry()
        tool_context = MagicMock()
        runtime = AgentRuntime(
            provider=provider,
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=permissions,
            tool_context=tool_context,
        )

        items = (Message(Role.SYSTEM, "You are a coding agent."),)
        rendered = runtime._model_items_with_reasoning_guidance(items)
        assert len(rendered) == 1


# ---------------------------------------------------------------------------
# Integration: full discovery with frontmatter
# ---------------------------------------------------------------------------


class TestSkillDiscoveryIntegration:
    def test_full_skill_with_frontmatter(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content=(
                "---\n"
                "name: commit\n"
                "description: Create a git commit with conventional message format\n"
                "when-to-use: User says commit, save changes, create a commit\n"
                "---\n\n"
                "# Git Commit Skill\n\n"
                "Follow the conventional commit format.\n"
            ),
        )
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.name == "commit"
        assert "conventional" in skill.description
        assert skill.when_to_use is not None
        assert "commit" in skill.when_to_use

    def test_multiple_skills_with_ordering(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        for name in ("zebra", "apple", "mango"):
            _make_skill(workspace, ".neuro", name)
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        names = [f.name for f in result.files]
        assert names == ["apple", "mango", "zebra"]

    def test_fingerprint_changes_on_skill_addition(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        result1 = discovery.discover(workspace)
        _make_skill(workspace, ".neuro", "commit")
        result2 = discovery.discover(workspace)
        assert result1.fingerprint != result2.fingerprint

    def test_fingerprint_changes_when_only_body_changes(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: Same\n---\n\nBody one",
        )
        discovery = FilesystemSkillDiscovery()
        first = discovery.discover(workspace)
        skill_path.write_text(
            "---\nname: commit\ndescription: Same\n---\n\nBody two",
            encoding="utf-8",
        )

        second = discovery.discover(workspace)

        assert first.fingerprint != second.fingerprint

    def test_bom_stripped(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "bom-skill"
        skill_dir.mkdir(parents=True)
        content = "\ufeff---\nname: bom-skill\ndescription: Has BOM\n---\n\nBody"
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "bom-skill"
        assert result.files[0].description == "Has BOM"

    def test_claude_config_dir_scanned(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".claude", "review")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "review"


# ---------------------------------------------------------------------------
# Adapter: USER scope discovery
# ---------------------------------------------------------------------------


def _make_user_skill(
    user_home: Path,
    config_dir: str,
    skill_dir_name: str,
    content: str | None = None,
) -> Path:
    """Create a SKILL.md file inside the user home skills directory.

    在用户主目录的 skills 目录中创建一个 SKILL.md 文件."""
    skill_dir = user_home / config_dir / SKILL_SUBDIR / skill_dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / SKILL_FILENAME
    if content is None:
        content = (
            "---\n"
            f"name: {skill_dir_name}\n"
            f"description: User skill {skill_dir_name}\n"
            "---\n\n"
            f"# {skill_dir_name}\n\nUser body.\n"
        )
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


class TestUserScopeDiscovery:
    def test_user_skill_discovered(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(user_home, ".neuro", "global-helper")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.name == "global-helper"
        assert skill.scope is SkillScope.USER
        assert skill.root == user_home.resolve()

    def test_local_and_user_skills_both_discovered(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(workspace, ".neuro", "local-skill")
        _make_user_skill(user_home, ".neuro", "user-skill")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 2
        names = {f.name for f in result.files}
        assert names == {"local-skill", "user-skill"}

    def test_local_priority_over_user_same_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: Local commit\n---\n\nLocal body.\n",
        )
        _make_user_skill(
            user_home,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: User commit\n---\n\nUser body.\n",
        )
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].description == "Local commit"
        assert result.files[0].scope is SkillScope.LOCAL

    def test_user_scope_skills_ordered_after_local(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(workspace, ".neuro", "zebra")
        _make_user_skill(user_home, ".neuro", "apple")
        _make_user_skill(user_home, ".neuro", "mango")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        scopes = [f.scope for f in result.files]
        # LOCAL first, then USER
        assert scopes == [SkillScope.LOCAL, SkillScope.USER, SkillScope.USER]
        names = [f.name for f in result.files]
        assert names == ["zebra", "apple", "mango"]

    def test_user_home_same_as_workspace_no_duplicate(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        # Pass workspace as user_home — USER scan should be skipped.
        discovery = FilesystemSkillDiscovery(user_home=workspace)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.LOCAL

    def test_user_home_nonexistent_no_user_skills(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        # Non-existent user home — USER scan finds nothing.
        discovery = FilesystemSkillDiscovery(user_home=tmp_path / "nonexistent")
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.LOCAL

    def test_user_skill_from_agents_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(user_home, ".agents", "review")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "review"
        assert result.files[0].scope is SkillScope.USER

    def test_user_skill_from_claude_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(user_home, ".claude", "deploy")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "deploy"
        assert result.files[0].scope is SkillScope.USER

    def test_user_skill_root_set_correctly(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(user_home, ".neuro", "global")
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.root == user_home.resolve()
        # The relative path should be relative to user_home, not workspace.
        assert skill.relative_path.startswith(".neuro/skills/")

    def test_local_skill_root_set_to_workspace(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "local")
        discovery = FilesystemSkillDiscovery(user_home=tmp_path / "nonexistent")
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.root == workspace.resolve()

    def test_user_skill_dedup_across_config_dirs(self, tmp_path: Path) -> None:
        """User-level skills dedup by name, first config dir wins.

        验证用户级技能按名称去重,第一个配置目录优先."""
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(
            user_home,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: From .neuro\n---\n\nBody.\n",
        )
        _make_user_skill(
            user_home,
            ".agents",
            "commit",
            content="---\nname: commit\ndescription: From .agents\n---\n\nBody.\n",
        )
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].description == "From .neuro"

    def test_fingerprint_changes_with_user_skill_addition(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result1 = discovery.discover(workspace)
        _make_user_skill(user_home, ".neuro", "global")
        result2 = discovery.discover(workspace)
        assert result1.fingerprint != result2.fingerprint

    def test_no_user_home_param_uses_path_home(self, tmp_path: Path) -> None:
        """Default constructor uses Path.home() — verify it doesn't crash.

        验证默认构造函数使用 Path.home() 时不会崩溃."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        # Should find the LOCAL skill; USER scan depends on real home dir.
        assert result.loaded_count >= 1
        local_skills = [f for f in result.files if f.scope is SkillScope.LOCAL]
        assert any(s.name == "commit" for s in local_skills)

    def test_path_home_runtime_error_skips_user_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Path.home() raises RuntimeError, USER scan is skipped gracefully.

        验证 Path.home() 抛出 RuntimeError 时会优雅跳过 USER 范围扫描."""

        def _raise_runtime_error() -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", _raise_runtime_error)
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.LOCAL

    def test_local_and_user_different_config_dirs_priority(self, tmp_path: Path) -> None:
        """Verify cross-scope config dir priority: LOCAL .agents > USER .neuro.

        验证跨范围配置目录优先级为 LOCAL .agents 高于 USER .neuro."""
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        # Same skill name in LOCAL .agents and USER .neuro.
        _make_skill(
            workspace,
            ".agents",
            "shared",
            content="---\nname: shared\ndescription: Local agents\n---\n\nBody.\n",
        )
        _make_user_skill(
            user_home,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: User neuro\n---\n\nBody.\n",
        )
        discovery = FilesystemSkillDiscovery(user_home=user_home)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        # LOCAL scope wins over USER scope regardless of config dir priority.
        assert result.files[0].description == "Local agents"
        assert result.files[0].scope is SkillScope.LOCAL


# ---------------------------------------------------------------------------
# Dynamic mid-session discovery (target-based upward walk)
# ---------------------------------------------------------------------------


def _make_nested_skill(
    base: Path,
    subpath: str,
    config_dir: str,
    skill_dir_name: str,
    content: str | None = None,
) -> Path:
    """Create a SKILL.md file at a nested path within *base*.

    *subpath* is a relative path like ``"src/foo"`` — the skill will be
    created at ``base/subpath/config_dir/skills/skill_dir_name/SKILL.md``.

    在 *base* 内的嵌套路径创建一个 SKILL.md 文件.
    """
    skill_dir = base / subpath / config_dir / SKILL_SUBDIR / skill_dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / SKILL_FILENAME
    if content is None:
        content = (
            "---\n"
            f"name: {skill_dir_name}\n"
            f"description: Nested skill {skill_dir_name}\n"
            "---\n\n"
            f"# {skill_dir_name}\n\nNested body.\n"
        )
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


class TestDynamicDiscovery:
    """Tests for target-based upward walk discovery (ADR 0043).

    测试基于目标路径向上遍历的发现逻辑 (ADR 0043)."""

    def test_nested_skill_discovered_with_target(self, tmp_path: Path) -> None:
        """A skill in a subdirectory is discovered when target is set.

        验证设置 target 后可以发现子目录中的技能."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-commit")
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        assert result.files[0].name == "nested-commit"
        assert result.files[0].scope is SkillScope.LOCAL

    def test_nested_skill_not_discovered_without_target(self, tmp_path: Path) -> None:
        """Without a target, only workspace-root config dirs are scanned.

        验证没有 target 时只扫描工作区根目录的配置目录."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        assert result.loaded_count == 0

    def test_root_skill_still_discovered_with_target(self, tmp_path: Path) -> None:
        """Root-level skills are still found when target is a subdirectory.

        验证 target 位于子目录时仍能发现根目录技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "root-commit")
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-commit")
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 2
        names = {f.name for f in result.files}
        assert "root-commit" in names
        assert "nested-commit" in names

    def test_deeper_skill_shadows_shallower_same_name(self, tmp_path: Path) -> None:
        """When a skill name exists at both a nested and root level, the
        deeper (closer to target) one wins due to first-seen-wins.

        验证技能名称同时存在于嵌套目录和根目录时,更接近目标的技能优先."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Root level\n---\n\nRoot.\n",
        )
        _make_nested_skill(
            workspace,
            "src/foo",
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Nested level\n---\n\nNested.\n",
        )
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        assert result.files[0].description == "Nested level"

    def test_target_equal_to_workspace_root(self, tmp_path: Path) -> None:
        """When target equals workspace root, only root level is scanned.

        验证 target 等于工作区根目录时只扫描根目录层级."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace, target=workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "commit"

    def test_target_outside_workspace_falls_back_to_root(self, tmp_path: Path) -> None:
        """Target outside workspace falls back to root-only scan.

        验证 target 位于工作区外时回退为只扫描根目录."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        outside = tmp_path / "outside"
        outside.mkdir()
        _make_nested_skill(outside, ".", ".neuro", "outside-skill")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace, target=outside)
        # Only the workspace root skill should be found, not the outside one.
        assert result.loaded_count == 1
        assert result.files[0].name == "commit"

    def test_file_target_uses_parent_directory(self, tmp_path: Path) -> None:
        """When target is a file path, its parent directory is used.

        验证 target 是文件路径时使用其父目录."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested")
        # Create a dummy file to use as target
        target_file = workspace / "src" / "foo" / "main.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# main", encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace, target=target_file)
        assert result.loaded_count == 1
        assert result.files[0].name == "nested"

    def test_skills_at_multiple_levels_discovered(self, tmp_path: Path) -> None:
        """Skills at multiple nesting levels are all discovered.

        验证可以发现多个嵌套层级的技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "root-skill")
        _make_nested_skill(workspace, "src", ".neuro", "mid-skill")
        _make_nested_skill(workspace, "src/foo", ".neuro", "deep-skill")
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 3
        names = {f.name for f in result.files}
        assert names == {"root-skill", "mid-skill", "deep-skill"}

    def test_nested_skill_root_is_workspace(self, tmp_path: Path) -> None:
        """LOCAL skills share one unambiguous workspace-relative root.

        验证 LOCAL 技能共享唯一明确的工作区相对根目录."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested")
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.root == workspace.resolve()

    def test_nested_skill_relative_path(self, tmp_path: Path) -> None:
        """Nested LOCAL paths retain their workspace-relative location.

        验证嵌套 LOCAL 路径保留相对于工作区的路径."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested")
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.relative_path == "src/foo/.neuro/skills/nested/SKILL.md"

    def test_intermediate_level_skill_shadows_root(self, tmp_path: Path) -> None:
        """A skill at an intermediate level shadows one at the root.

        验证中间层级的技能会覆盖根目录同名技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Root\n---\n\nRoot.\n",
        )
        _make_nested_skill(
            workspace,
            "src",
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Mid\n---\n\nMid.\n",
        )
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        (workspace / "src" / "foo").mkdir(parents=True)
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        assert result.files[0].description == "Mid"

    def test_config_dir_priority_at_nested_level(self, tmp_path: Path) -> None:
        """Config dir priority (.neuro > .agents) applies at nested levels.

        验证配置目录优先级 (.neuro > .agents) 在嵌套层级同样生效."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(
            workspace,
            "src/foo",
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Neuro nested\n---\n\nNeuro.\n",
        )
        _make_nested_skill(
            workspace,
            "src/foo",
            ".agents",
            "shared",
            content="---\nname: shared\ndescription: Agents nested\n---\n\nAgents.\n",
        )
        discovery = FilesystemSkillDiscovery()
        target = workspace / "src" / "foo"
        result = discovery.discover(workspace, target=target)
        assert result.loaded_count == 1
        assert result.files[0].description == "Neuro nested"

    def test_target_none_scans_root_only(self, tmp_path: Path) -> None:
        """target=None scans only the workspace root level.

        验证 target=None 时只扫描工作区根目录层级."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "root-skill")
        _make_nested_skill(workspace, "src", ".neuro", "nested-skill")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace, target=None)
        assert result.loaded_count == 1
        assert result.files[0].name == "root-skill"


class TestSkillTrackerDynamicTarget:
    """Tests for SkillTracker's moving target (check_path / current_result).

    测试 SkillTracker 的移动目标行为 (check_path / current_result)."""

    def test_initial_target_defaults_to_workspace_root(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        assert tracker.target == workspace.resolve()

    def test_initial_target_can_be_set(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        sub = workspace / "src" / "foo"
        sub.mkdir(parents=True)
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(
            discovery=discovery,
            workspace_root=workspace,
            initial_target=sub,
        )
        assert tracker.target == sub.resolve()

    def test_check_path_updates_target(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        sub = workspace / "src" / "foo"
        sub.mkdir(parents=True)
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        tracker.check_path(sub)
        assert tracker.target == sub.resolve()

    def test_check_path_file_uses_parent(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        sub = workspace / "src" / "foo"
        sub.mkdir(parents=True)
        target_file = sub / "main.py"
        target_file.write_text("# main", encoding="utf-8")
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        tracker.check_path(target_file)
        assert tracker.target == sub.resolve()

    def test_check_path_outside_workspace_ignored(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        tracker.check_path(outside)
        # Target should not have moved
        assert tracker.target == workspace.resolve()

    def test_initial_target_outside_workspace_falls_back(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(
            discovery=discovery,
            workspace_root=workspace,
            initial_target=outside,
        )
        # Target should fall back to workspace root
        assert tracker.target == workspace.resolve()

    def test_check_path_resolve_error_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        original_resolve = Path.resolve

        def _fail_resolve(self: Path, strict: bool = False) -> Path:
            raise RuntimeError("mocked resolve failure")

        monkeypatch.setattr(Path, "resolve", _fail_resolve)
        # Should not raise; target stays unchanged
        tracker.check_path(workspace / "src" / "foo")
        # Restore and verify target is still workspace root
        monkeypatch.setattr(Path, "resolve", original_resolve)
        assert tracker.target == workspace.resolve()

    def test_current_result_uses_target(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested")
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        # Without moving target, nested skill is not found
        result = tracker.current_result()
        assert result.loaded_count == 0
        # Move target to the nested directory
        tracker.check_path(workspace / "src" / "foo")
        result = tracker.current_result()
        assert result.loaded_count == 1
        assert result.files[0].name == "nested"

    def test_target_moves_between_subtrees(self, tmp_path: Path) -> None:
        from neuro_code.application.runtime.skill_tracker import SkillTracker

        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "foo-skill")
        _make_nested_skill(workspace, "src/bar", ".neuro", "bar-skill")
        discovery = FilesystemSkillDiscovery()
        tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
        # Move to foo subtree
        tracker.check_path(workspace / "src" / "foo")
        result = tracker.current_result()
        assert result.loaded_count == 1
        assert result.files[0].name == "foo-skill"
        # Move to bar subtree — foo-skill should no longer be present
        tracker.check_path(workspace / "src" / "bar")
        result = tracker.current_result()
        assert result.loaded_count == 1
        assert result.files[0].name == "bar-skill"


# ---------------------------------------------------------------------------
# REPO scope: git root discovery (ADR 0044)
# ---------------------------------------------------------------------------


def _make_repo_skill(
    repo_root: Path,
    config_dir: str,
    skill_dir_name: str,
    content: str | None = None,
) -> Path:
    """Create a SKILL.md file inside a repo root skills directory.

    在仓库根目录的 skills 目录中创建一个 SKILL.md 文件."""
    skill_dir = repo_root / config_dir / SKILL_SUBDIR / skill_dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / SKILL_FILENAME
    if content is None:
        content = (
            "---\n"
            f"name: {skill_dir_name}\n"
            f"description: Repo skill {skill_dir_name}\n"
            "---\n\n"
            f"# {skill_dir_name}\n\nRepo body.\n"
        )
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


class TestRepoScopeDiscovery:
    """Tests for REPO scope (git root) skill discovery (ADR 0044).

    测试 REPO 范围 (git 根目录) 的技能发现 (ADR 0044)."""

    def test_repo_skill_discovered(self, tmp_path: Path) -> None:
        """A skill at the git root is discovered with REPO scope.

        验证 git 根目录的技能会以 REPO 范围发现."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        _make_repo_skill(repo_root, ".neuro", "repo-commit")
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "repo-commit"
        assert result.files[0].scope is SkillScope.REPO

    def test_intermediate_repo_ancestor_skill_is_discovered(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "myrepo"
        package_root = repo_root / "packages"
        workspace = package_root / "frontend"
        workspace.mkdir(parents=True)
        _make_repo_skill(package_root, ".neuro", "package-shared")

        result = FilesystemSkillDiscovery(git_root=repo_root).discover(workspace)

        assert result.loaded_count == 1
        skill = result.files[0]
        assert skill.scope is SkillScope.REPO
        assert skill.root == repo_root.resolve()
        assert skill.relative_path == "packages/.neuro/skills/package-shared/SKILL.md"

    def test_nearer_repo_ancestor_shadows_git_root(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "myrepo"
        package_root = repo_root / "packages"
        workspace = package_root / "frontend"
        workspace.mkdir(parents=True)
        _make_repo_skill(
            repo_root,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Git root\n---\n\nRoot",
        )
        _make_repo_skill(
            package_root,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Package\n---\n\nPackage",
        )

        result = FilesystemSkillDiscovery(git_root=repo_root).discover(workspace)

        assert result.loaded_count == 1
        assert result.files[0].description == "Package"

    def test_repo_skill_not_discovered_when_git_root_equals_workspace(self, tmp_path: Path) -> None:
        """When git root equals workspace root, REPO scan is skipped.

        验证 git 根目录等于工作区根目录时跳过 REPO 扫描."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery(git_root=workspace)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.LOCAL

    def test_repo_skill_not_discovered_when_git_root_not_ancestor(self, tmp_path: Path) -> None:
        """When git root is not an ancestor of workspace, REPO scan is skipped.

        验证 git 根目录不是工作区祖先时跳过 REPO 扫描."""
        workspace = _make_workspace(tmp_path)
        other_root = tmp_path / "other"
        other_root.mkdir()
        _make_repo_skill(other_root, ".neuro", "other-skill")
        discovery = FilesystemSkillDiscovery(git_root=other_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 0

    def test_local_shadows_repo_same_name(self, tmp_path: Path) -> None:
        """LOCAL scope shadows REPO scope for same-named skills.

        验证同名技能中 LOCAL 范围覆盖 REPO 范围."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        _make_skill(workspace, ".neuro", "shared")
        _make_repo_skill(
            repo_root,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Repo\n---\n\nRepo.\n",
        )
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.LOCAL

    def test_repo_shadows_user_same_name(self, tmp_path: Path) -> None:
        """REPO scope shadows USER scope for same-named skills.

        验证同名技能中 REPO 范围覆盖 USER 范围."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_repo_skill(repo_root, ".neuro", "shared")
        _make_user_skill(
            user_home,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: User\n---\n\nUser.\n",
        )
        discovery = FilesystemSkillDiscovery(user_home=user_home, git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].scope is SkillScope.REPO

    def test_local_repo_user_all_discovered(self, tmp_path: Path) -> None:
        """Skills at all three scopes are discovered with correct priority.

        验证三个范围的技能都能以正确优先级被发现."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(workspace, ".neuro", "local-skill")
        _make_repo_skill(repo_root, ".neuro", "repo-skill")
        _make_user_skill(user_home, ".neuro", "user-skill")
        discovery = FilesystemSkillDiscovery(user_home=user_home, git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 3
        scopes = {f.name: f.scope for f in result.files}
        assert scopes["local-skill"] is SkillScope.LOCAL
        assert scopes["repo-skill"] is SkillScope.REPO
        assert scopes["user-skill"] is SkillScope.USER

    def test_repo_skill_root_set_to_git_root(self, tmp_path: Path) -> None:
        """SkillInfo.root is set to the git root for REPO scope skills.

        验证 REPO 范围技能的 SkillInfo.root 设置为 git 根目录."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        _make_repo_skill(repo_root, ".neuro", "repo-commit")
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].root == repo_root.resolve()

    def test_repo_skill_relative_path(self, tmp_path: Path) -> None:
        """relative_path is relative to the git root, not workspace.

        验证 relative_path 相对于 git 根目录,而不是工作区."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        _make_repo_skill(repo_root, ".neuro", "repo-commit")
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].relative_path == ".neuro/skills/repo-commit/SKILL.md"

    def test_repo_skill_from_agents_dir(self, tmp_path: Path) -> None:
        """REPO scope skills from .agents/ directory are discovered.

        验证可以发现 .agents/ 目录中的 REPO 范围技能."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        _make_repo_skill(repo_root, ".agents", "agents-skill")
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace)
        assert result.loaded_count == 1
        assert result.files[0].name == "agents-skill"
        assert result.files[0].scope is SkillScope.REPO

    def test_repo_scope_with_dynamic_target(self, tmp_path: Path) -> None:
        """REPO scope skills are discovered alongside LOCAL dynamic skills.

        验证 REPO 范围技能可以与 LOCAL 动态技能同时被发现."""
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        workspace = repo_root / "frontend"
        workspace.mkdir()
        (workspace / "src" / "foo").mkdir(parents=True)
        _make_skill(workspace, ".neuro", "root-skill")
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-skill")
        _make_repo_skill(repo_root, ".neuro", "repo-skill")
        discovery = FilesystemSkillDiscovery(git_root=repo_root)
        result = discovery.discover(workspace, target=workspace / "src" / "foo")
        assert result.loaded_count == 3
        names = {f.name for f in result.files}
        assert names == {"root-skill", "nested-skill", "repo-skill"}

    def test_no_git_root_param_auto_detects(self, tmp_path: Path) -> None:
        """Default constructor auto-detects git root — verify no crash.

        验证默认构造函数自动检测 git 根目录时不会崩溃."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        # LOCAL skill should always be found; REPO depends on real git repo.
        assert result.loaded_count >= 1
        local_skills = [f for f in result.files if f.scope is SkillScope.LOCAL]
        assert any(s.name == "commit" for s in local_skills)

    def test_git_root_none_when_not_a_repo(self, tmp_path: Path) -> None:
        """When the workspace is not in a git repo, REPO scan is skipped.

        验证工作区不在 git 仓库内时跳过 REPO 扫描."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        # No git_root param → auto-detect → not a git repo → no REPO skills
        discovery = FilesystemSkillDiscovery()
        result = discovery.discover(workspace)
        repo_skills = [f for f in result.files if f.scope is SkillScope.REPO]
        assert len(repo_skills) == 0
