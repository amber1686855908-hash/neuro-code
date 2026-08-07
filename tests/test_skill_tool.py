"""Tests for the skill body loading tool and the extract_skill_body function.

Covers:
- ``extract_skill_body``: frontmatter stripping (with/without frontmatter,
  edge cases like missing closing delimiter, BOM-prefixed content).
- ``SkillTool``: successful loading, skill-not-found, no-tracker, invalid
  name, BOM stripping, bundled file listing, output format verification,
  symlink rejection (POSIX only), and read error handling.

测试技能正文加载工具及 `extract_skill_body` 函数.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.skill_tracker import SkillTracker
from neuro_code.domain.tools import ToolResult
from neuro_code.domain.workspace.skills import (
    MAX_SKILL_ARGUMENT_BYTES,
    MAX_SKILL_SUBSTITUTIONS,
    SKILL_FILENAME,
    SKILL_SUBDIR,
    SkillScope,
    apply_skill_substitutions,
    extract_skill_body,
)
from neuro_code.infrastructure.tools.skills import SkillTool
from neuro_code.infrastructure.workspace.skills import FilesystemSkillDiscovery
from neuro_code.shared.errors import ToolError

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


def _make_context(workspace: Path) -> ToolContext:
    """Build a ToolContext with a SkillTracker for the given workspace.

    为给定工作区构建带 SkillTracker 的 ToolContext."""
    discovery = FilesystemSkillDiscovery()
    tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
    return ToolContext(cwd=workspace, skill_tracker=tracker)


def _make_context_with_user_home(workspace: Path, user_home: Path) -> ToolContext:
    """Build a ToolContext with a SkillTracker that scans both workspace and user home.

    构建带 SkillTracker 的 ToolContext,同时扫描工作区和用户主目录."""
    discovery = FilesystemSkillDiscovery(user_home=user_home)
    tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
    return ToolContext(cwd=workspace, skill_tracker=tracker)


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


# ---------------------------------------------------------------------------
# Domain: extract_skill_body
# ---------------------------------------------------------------------------


class TestExtractSkillBody:
    def test_strips_frontmatter(self) -> None:
        content = "---\nname: commit\ndescription: test\n---\n\n# Skill\n\nBody."
        body = extract_skill_body(content)
        assert body == "# Skill\n\nBody."

    def test_no_frontmatter_returns_content(self) -> None:
        content = "# Just a body\n\nNo frontmatter here."
        body = extract_skill_body(content)
        assert body == "# Just a body\n\nNo frontmatter here."

    def test_no_closing_delimiter_returns_content(self) -> None:
        content = "---\nname: broken\nno closing delimiter"
        body = extract_skill_body(content)
        # Without a closing \n---, the original content is returned.
        assert "broken" in body

    def test_empty_body_after_frontmatter(self) -> None:
        content = "---\nname: empty\n---\n"
        body = extract_skill_body(content)
        assert body == ""

    def test_leading_whitespace_stripped(self) -> None:
        content = "  \n  ---\nname: test\n---\n\nBody"
        body = extract_skill_body(content)
        assert body == "Body"

    def test_frontmatter_with_when_to_use(self) -> None:
        content = (
            "---\n"
            "name: commit\n"
            "description: Create a commit\n"
            "when-to-use: User says commit\n"
            "---\n\n"
            "Follow the conventional commit format.\n"
        )
        body = extract_skill_body(content)
        assert body == "Follow the conventional commit format.\n"

    def test_closing_delimiter_at_end_without_newline(self) -> None:
        content = "---\nname: test\n---"
        body = extract_skill_body(content)
        assert body == ""

    def test_delimiters_must_be_complete_lines(self) -> None:
        content = "----\nname: not-frontmatter\n---junk\nBody"
        assert extract_skill_body(content) == content


# ---------------------------------------------------------------------------
# Tool: SkillTool basic loading
# ---------------------------------------------------------------------------


class TestSkillToolLoading:
    async def test_load_skill_with_frontmatter(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content=(
                "---\n"
                "name: commit\n"
                "description: Create a git commit\n"
                "when-to-use: User says commit\n"
                "---\n\n"
                "# Git Commit Skill\n\n"
                "Follow the conventional commit format.\n"
            ),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert '<skill_content name="commit">' in result.content
        assert "# Skill: commit" in result.content
        assert "Follow the conventional commit format." in result.content
        assert "Base directory for this skill:" in result.content
        assert result.metadata["skill_name"] == "commit"
        assert "SKILL.md" in result.metadata["path"]

    async def test_load_skill_without_frontmatter(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "simple"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# Simple Skill\n\nJust a body, no frontmatter.",
            encoding="utf-8",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "simple"}, context)
        assert not result.is_error
        assert "Just a body, no frontmatter." in result.content
        assert '<skill_content name="simple">' in result.content

    async def test_load_skill_with_bom(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "bom"
        skill_dir.mkdir(parents=True)
        content = "\ufeff---\nname: bom\ndescription: Has BOM\n---\n\nBody text."
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "bom"}, context)
        assert not result.is_error
        assert "Body text." in result.content
        # The BOM should not appear in the output.
        assert "\ufeff" not in result.content

    async def test_load_skill_strips_frontmatter_from_output(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content=(
                "---\nname: commit\ndescription: Create a git commit\n---\n\nThis is the body.\n"
            ),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        # The frontmatter should not appear in the output body.
        assert "name: commit" not in result.content
        assert "description: Create a git commit" not in result.content
        assert "This is the body." in result.content


# ---------------------------------------------------------------------------
# Tool: SkillTool error handling
# ---------------------------------------------------------------------------


class TestSkillToolErrors:
    async def test_skill_not_found(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="not found"):
            await tool.execute({"name": "nonexistent"}, context)

    async def test_skill_not_found_lists_available(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        _make_skill(workspace, ".neuro", "review")
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="commit") as exc_info:
            await tool.execute({"name": "nonexistent"}, context)
        assert "review" in str(exc_info.value)

    async def test_no_skill_tracker_in_context(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        context = ToolContext(cwd=workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="not available"):
            await tool.execute({"name": "commit"}, context)

    async def test_empty_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="non-empty"):
            await tool.execute({"name": ""}, context)

    async def test_none_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="non-empty"):
            await tool.execute({"name": None}, context)  # type: ignore[arg-type]

    async def test_whitespace_only_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="non-empty"):
            await tool.execute({"name": "   "}, context)

    async def test_no_skills_available(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="not found"):
            await tool.execute({"name": "commit"}, context)

    async def test_name_is_stripped(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "  commit  "}, context)
        assert not result.is_error
        assert result.metadata["skill_name"] == "commit"

    async def test_args_must_be_a_string(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)

        with pytest.raises(ToolError, match="args must be a string"):
            await SkillTool().execute({"name": "commit", "args": ["bad"]}, context)

    async def test_output_limit_must_be_positive(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        base_context = _make_context(workspace)
        context = ToolContext(
            cwd=workspace,
            output_byte_limit=0,
            skill_tracker=base_context.skill_tracker,
        )

        with pytest.raises(ToolError, match="must be positive"):
            await SkillTool().execute({"name": "commit"}, context)

    async def test_rendered_output_respects_context_limit(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        base_context = _make_context(workspace)
        context = ToolContext(
            cwd=workspace,
            output_byte_limit=32,
            skill_tracker=base_context.skill_tracker,
        )

        with pytest.raises(ToolError, match="output limit"):
            await SkillTool().execute({"name": "commit"}, context)


# ---------------------------------------------------------------------------
# Tool: SkillTool output format
# ---------------------------------------------------------------------------


class TestSkillToolOutputFormat:
    async def test_xml_wrapper(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert result.content.startswith('<skill_content name="commit">')
        assert result.content.endswith("</skill_content>")

    async def test_base_directory_in_output(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert "Base directory for this skill:" in result.content
        assert "file://" in result.content
        assert "skills" in result.content
        assert "commit" in result.content

    async def test_relative_paths_note_in_output(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert "relative to this base directory" in result.content

    async def test_metadata_fields(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert result.metadata is not None
        assert result.metadata["skill_name"] == "commit"
        assert result.metadata["path"] == ".neuro/skills/commit/SKILL.md"


# ---------------------------------------------------------------------------
# Tool: SkillTool bundled files listing
# ---------------------------------------------------------------------------


class TestSkillToolBundledFiles:
    async def test_bundled_files_listed(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "commit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: commit\ndescription: test\n---\n\nBody.\n",
            encoding="utf-8",
        )
        (skill_dir / "helper.sh").write_text("echo hi", encoding="utf-8")
        (skill_dir / "reference.md").write_text("# Reference", encoding="utf-8")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert "Bundled files" in result.content
        assert "helper.sh" in result.content
        assert "reference.md" in result.content
        # SKILL.md itself should not be in the bundled files list.
        assert "SKILL.md" not in result.content.split("Bundled files")[1]

    async def test_no_bundled_files(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "commit")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        # No "Bundled files" section if there are no bundled files.
        assert "Bundled files" not in result.content

    async def test_bundled_files_max_10(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "multi"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: multi\ndescription: test\n---\n\nBody.\n",
            encoding="utf-8",
        )
        for i in range(15):
            (skill_dir / f"file_{i:02d}.txt").write_text(f"file {i}", encoding="utf-8")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "multi"}, context)
        # Count the bundled file entries (lines starting with "- " after "Bundled files")
        bundled_section = result.content.split("Bundled files")[1]
        file_lines = [line for line in bundled_section.split("\n") if line.startswith("- ")]
        assert len(file_lines) == 10

    async def test_bundled_files_sorted(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "sorted"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: sorted\ndescription: test\n---\n\nBody.\n",
            encoding="utf-8",
        )
        (skill_dir / "zebra.txt").write_text("z", encoding="utf-8")
        (skill_dir / "apple.txt").write_text("a", encoding="utf-8")
        (skill_dir / "mango.txt").write_text("m", encoding="utf-8")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "sorted"}, context)
        bundled_section = result.content.split("Bundled files")[1]
        file_names = [
            line.split("- ")[1].strip()
            for line in bundled_section.split("\n")
            if line.startswith("- ")
        ]
        assert file_names == sorted(file_names)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink setup is POSIX-only")
    async def test_bundled_symlink_is_omitted(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(workspace, ".neuro", "safe")
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, skill_path.parent / "linked-secret.txt")

        result = await SkillTool().execute({"name": "safe"}, _make_context(workspace))

        assert "linked-secret.txt" not in result.content

    async def test_oversized_bundled_listing_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import neuro_code.infrastructure.tools.skills as skill_tool_module

        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(workspace, ".neuro", "bounded")
        (skill_path.parent / "a.txt").write_text("a", encoding="utf-8")
        (skill_path.parent / "b.txt").write_text("b", encoding="utf-8")
        monkeypatch.setattr(skill_tool_module, "_MAX_BUNDLED_DIRECTORY_ENTRIES", 2)

        result = await SkillTool().execute({"name": "bounded"}, _make_context(workspace))

        assert "Bundled files" not in result.content


# ---------------------------------------------------------------------------
# Tool: SkillTool multiple skills and config dirs
# ---------------------------------------------------------------------------


class TestSkillToolMultipleSkills:
    async def test_load_from_agents_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".agents", "review")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "review"}, context)
        assert not result.is_error
        assert result.metadata["skill_name"] == "review"
        assert result.metadata["path"] == ".agents/skills/review/SKILL.md"

    async def test_load_from_claude_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".claude", "deploy")
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "deploy"}, context)
        assert not result.is_error
        assert result.metadata["skill_name"] == "deploy"

    async def test_load_nested_skill(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        # Create a nested skill: .neuro/skills/category/sub-skill/SKILL.md
        skill_dir = workspace / ".neuro" / "skills" / "category" / "sub-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: sub-skill\ndescription: nested\n---\n\nNested body.\n",
            encoding="utf-8",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "sub-skill"}, context)
        assert not result.is_error
        assert "Nested body." in result.content

    async def test_dedup_uses_neuro_priority(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: From neuro\n---\n\nNeuro body.\n",
        )
        _make_skill(
            workspace,
            ".agents",
            "commit",
            content="---\nname: commit\ndescription: From agents\n---\n\nAgents body.\n",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        # .neuro has priority, so the body should be from .neuro.
        assert "Neuro body." in result.content
        assert "Agents body." not in result.content


# ---------------------------------------------------------------------------
# Tool: SkillTool definition and properties
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tool: SkillTool user-level skills
# ---------------------------------------------------------------------------


class TestSkillToolUserScope:
    async def test_load_user_level_skill(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(
            user_home,
            ".neuro",
            "global-helper",
            content=(
                "---\nname: global-helper\ndescription: A global skill\n---\n\n"
                "# Global Helper\n\nThis works across projects.\n"
            ),
        )
        context = _make_context_with_user_home(workspace, user_home)
        tool = SkillTool()
        result = await tool.execute({"name": "global-helper"}, context)
        assert not result.is_error
        assert "Global Helper" in result.content
        assert "This works across projects." in result.content
        assert result.metadata["skill_name"] == "global-helper"

    async def test_local_skill_preferred_over_user_same_name(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: Local\n---\n\nLocal body.\n",
        )
        _make_user_skill(
            user_home,
            ".neuro",
            "commit",
            content="---\nname: commit\ndescription: User\n---\n\nUser body.\n",
        )
        context = _make_context_with_user_home(workspace, user_home)
        tool = SkillTool()
        result = await tool.execute({"name": "commit"}, context)
        assert not result.is_error
        # LOCAL body should be loaded, not USER body.
        assert "Local body." in result.content
        assert "User body." not in result.content

    async def test_user_skill_from_agents_dir(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(user_home, ".agents", "review")
        context = _make_context_with_user_home(workspace, user_home)
        tool = SkillTool()
        result = await tool.execute({"name": "review"}, context)
        assert not result.is_error
        assert "User body." in result.content

    async def test_user_skill_base_dir_resolves_correctly(self, tmp_path: Path) -> None:
        """Verify that the SkillTool resolves user-level skill paths correctly.

        验证 SkillTool 能正确解析用户级技能路径."""
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_user_skill(
            user_home,
            ".neuro",
            "global",
            content="---\nname: global\ndescription: test\n---\n\nBody.\n",
        )
        context = _make_context_with_user_home(workspace, user_home)
        tool = SkillTool()
        result = await tool.execute({"name": "global"}, context)
        assert not result.is_error
        # The base directory should point to the user home, not the workspace.
        assert "file://" in result.content
        assert "home" in result.content

    async def test_both_local_and_user_skills_available(self, tmp_path: Path) -> None:
        """Verify that both LOCAL and USER skills are available for loading.

        验证 LOCAL 和 USER 技能都可以加载."""
        workspace = _make_workspace(tmp_path)
        user_home = tmp_path / "home"
        user_home.mkdir()
        _make_skill(workspace, ".neuro", "local-only")
        _make_user_skill(user_home, ".neuro", "user-only")
        context = _make_context_with_user_home(workspace, user_home)
        tool = SkillTool()

        # Load the local skill.
        result = await tool.execute({"name": "local-only"}, context)
        assert not result.is_error
        assert "Body text." in result.content

        # Load the user skill.
        result = await tool.execute({"name": "user-only"}, context)
        assert not result.is_error
        assert "User body." in result.content


class TestSkillToolDefinition:
    def test_definition_name(self) -> None:
        tool = SkillTool()
        assert tool.definition.name == "skill"

    def test_definition_has_description(self) -> None:
        tool = SkillTool()
        assert len(tool.definition.description) > 20

    def test_definition_input_schema(self) -> None:
        tool = SkillTool()
        schema = tool.definition.input_schema
        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert schema["properties"]["name"]["type"] == "string"
        assert "name" in schema["required"]
        assert schema["additionalProperties"] is False

    def test_side_effecting_false(self) -> None:
        tool = SkillTool()
        assert tool.side_effecting is False

    def test_registered_in_default_tool_registry(self) -> None:
        from neuro_code.domain.sandbox import SandboxProfile
        from neuro_code.infrastructure.tools.registry import default_tool_registry

        registry = default_tool_registry(SandboxProfile.OFF)
        assert "skill" in registry.names()


# ---------------------------------------------------------------------------
# Tool: SkillTool error branch coverage
# ---------------------------------------------------------------------------


class TestSkillToolErrorBranches:
    def test_describe_read_reason_symlink(self) -> None:
        from neuro_code.domain.instructions import InstructionRejectionReason
        from neuro_code.infrastructure.tools.skills import _describe_read_reason

        desc = _describe_read_reason(InstructionRejectionReason.SYMLINK_NOT_SUPPORTED)
        assert "symlink" in desc.lower()

    def test_describe_read_reason_not_a_file(self) -> None:
        from neuro_code.domain.instructions import InstructionRejectionReason
        from neuro_code.infrastructure.tools.skills import _describe_read_reason

        desc = _describe_read_reason(InstructionRejectionReason.NOT_A_FILE)
        assert "regular file" in desc

    def test_describe_read_reason_read_error(self) -> None:
        from neuro_code.domain.instructions import InstructionRejectionReason
        from neuro_code.infrastructure.tools.skills import _describe_read_reason

        desc = _describe_read_reason(InstructionRejectionReason.READ_ERROR)
        assert "read error" in desc.lower()

    def test_describe_read_reason_fallback(self) -> None:
        from neuro_code.domain.instructions import InstructionRejectionReason
        from neuro_code.infrastructure.tools.skills import _describe_read_reason

        # Use a reason that doesn't have a specific handler.
        desc = _describe_read_reason(InstructionRejectionReason.ESCAPES_WORKSPACE)
        assert desc == InstructionRejectionReason.ESCAPES_WORKSPACE.value

    def test_list_bundled_files_nonexistent_dir(self) -> None:
        from neuro_code.infrastructure.tools.skills import _list_bundled_files

        # A path that doesn't exist should return an empty list.
        result = _list_bundled_files(Path("/nonexistent/path/that/does/not/exist"))
        assert result == []

    async def test_read_error_file_deleted_after_discovery(self, tmp_path: Path) -> None:
        """Test that a read error is raised when the skill file is deleted
        between discovery and tool execution.

        验证技能文件在发现后被删除时会抛出读取错误."""
        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(workspace, ".neuro", "ghost")
        discovery = FilesystemSkillDiscovery()
        # Discover first to get the skill info.
        result = discovery.discover(workspace)
        assert result.loaded_count == 1

        # Delete the file so the tool's read will fail.
        skill_path.unlink()

        # Build a mock tracker that returns the stale discovery result.
        from unittest.mock import MagicMock

        tracker = MagicMock()
        tracker.current_result.return_value = result
        tracker.workspace_root = workspace.resolve()

        context = ToolContext(cwd=workspace, skill_tracker=tracker)
        tool = SkillTool()
        with pytest.raises(ToolError, match="could not read"):
            await tool.execute({"name": "ghost"}, context)

    async def test_invalid_utf8_after_discovery(self, tmp_path: Path) -> None:
        """Test that an invalid UTF-8 error is raised when the skill file
        is overwritten with invalid bytes between discovery and tool execution.

        验证技能文件在发现后被无效 UTF-8 字节覆盖时会抛出错误."""
        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(workspace, ".neuro", "broken")
        discovery = FilesystemSkillDiscovery()
        # Discover first to get the skill info.
        result = discovery.discover(workspace)
        assert result.loaded_count == 1

        # Overwrite with invalid UTF-8 bytes.
        skill_path.write_bytes(b"\xff\xfe\x00\x01invalid")

        # Build a mock tracker that returns the stale discovery result.
        from unittest.mock import MagicMock

        tracker = MagicMock()
        tracker.current_result.return_value = result
        tracker.workspace_root = workspace.resolve()

        context = ToolContext(cwd=workspace, skill_tracker=tracker)
        tool = SkillTool()
        with pytest.raises(ToolError, match="not valid UTF-8"):
            await tool.execute({"name": "broken"}, context)

    async def test_valid_file_changed_after_discovery_is_rejected(self, tmp_path: Path) -> None:
        """A stable read must still match the metadata discovery snapshot.

        验证稳定读取结果仍必须匹配发现阶段的元数据快照."""
        from unittest.mock import MagicMock

        workspace = _make_workspace(tmp_path)
        skill_path = _make_skill(workspace, ".neuro", "changed")
        discovery_result = FilesystemSkillDiscovery().discover(workspace)
        skill_path.write_text(
            "---\nname: changed\ndescription: Skill changed\n---\n\nNew body.\n",
            encoding="utf-8",
        )
        tracker = MagicMock()
        tracker.current_result.return_value = discovery_result
        tracker.workspace_root = workspace.resolve()
        context = ToolContext(cwd=workspace, skill_tracker=tracker)

        with pytest.raises(ToolError, match="changed during loading"):
            await SkillTool().execute({"name": "changed"}, context)

    def test_workspace_escape_rejected(self) -> None:
        """Traversal is rejected at the SkillInfo domain boundary.

        验证路径遍历会在 SkillInfo 领域边界被拒绝."""
        from neuro_code.domain.workspace.skills import SkillInfo

        with pytest.raises(ValueError, match="relative POSIX"):
            SkillInfo(
                name="escape",
                description="test",
                when_to_use=None,
                relative_path="../../etc/passwd",
                scope=SkillScope.LOCAL,
                depth=0,
            )


# ---------------------------------------------------------------------------
# Tool: SkillTool symlink rejection (POSIX-only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS symlinks require admin or Developer Mode",
)
class TestSkillToolSymlinkRejection:
    async def test_symlink_skill_rejected(self, tmp_path: Path) -> None:
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "linked"
        skill_dir.mkdir(parents=True)
        target = tmp_path / "outside.txt"
        target.write_text("escaped content", encoding="utf-8")
        link = skill_dir / "SKILL.md"
        os.symlink(target, link)
        context = _make_context(workspace)
        tool = SkillTool()
        discovery_result = context.skill_tracker.current_result()
        assert any(
            rejection.reason.value == "symlink-escape" for rejection in discovery_result.rejections
        )
        with pytest.raises(ToolError, match="not found"):
            await tool.execute({"name": "linked"}, context)


# ---------------------------------------------------------------------------
# Tool: SkillTool integration with real files
# ---------------------------------------------------------------------------


class TestSkillToolIntegration:
    async def test_full_workflow_discover_then_load(self, tmp_path: Path) -> None:
        """Test the full workflow: discover skills, then load one by name.

        测试完整工作流:先发现技能,再按名称加载技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "commit",
            content=(
                "---\n"
                "name: commit\n"
                "description: Create a git commit with conventional message format\n"
                "when-to-use: User says commit, save changes\n"
                "---\n\n"
                "# Git Commit Skill\n\n"
                "1. Stage changes with `git add`.\n"
                "2. Create a commit with a conventional message.\n"
                "3. Verify with `git log`.\n"
            ),
        )
        _make_skill(
            workspace,
            ".neuro",
            "review",
            content=(
                "---\n"
                "name: review\n"
                "description: Review code changes before merging\n"
                "---\n\n"
                "# Code Review Skill\n\n"
                "Check for style, correctness, and test coverage.\n"
            ),
        )

        context = _make_context(workspace)
        tool = SkillTool()

        # Load the "commit" skill.
        result = await tool.execute({"name": "commit"}, context)
        assert not result.is_error
        assert "Git Commit Skill" in result.content
        assert "git add" in result.content
        assert "conventional message" in result.content

        # Load the "review" skill.
        result = await tool.execute({"name": "review"}, context)
        assert not result.is_error
        assert "Code Review Skill" in result.content
        assert "test coverage" in result.content

    async def test_skill_file_changes_picked_up_without_restart(self, tmp_path: Path) -> None:
        """Verify that the tracker re-discovers on each call.

        验证跟踪器每次调用都会重新发现技能."""
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "dynamic"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: dynamic\ndescription: v1\n---\n\nVersion 1.\n",
            encoding="utf-8",
        )

        context = _make_context(workspace)
        tool = SkillTool()

        # Load the skill — should see v1 body.
        result = await tool.execute({"name": "dynamic"}, context)
        assert "Version 1." in result.content

        # Modify the skill file.
        (skill_dir / "SKILL.md").write_text(
            "---\nname: dynamic\ndescription: v2\n---\n\nVersion 2.\n",
            encoding="utf-8",
        )

        # Load again — should see v2 body (no restart needed).
        result = await tool.execute({"name": "dynamic"}, context)
        assert "Version 2." in result.content
        assert "Version 1." not in result.content

    async def test_skill_with_special_characters_in_body(self, tmp_path: Path) -> None:
        """Verify that skills with special characters in the body load correctly.

        验证正文包含特殊字符的技能可以正确加载."""
        workspace = _make_workspace(tmp_path)
        skill_dir = workspace / ".neuro" / "skills" / "special"
        skill_dir.mkdir(parents=True)
        body = (
            "Use `$VAR` and `${ENV}` in scripts.\n"
            "Run: `make build && make test`.\n"
            "Use quotes: \"hello\" and 'world'.\n"
        )
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: special\ndescription: test\n---\n\n{body}",
            encoding="utf-8",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "special"}, context)
        assert not result.is_error
        assert "$VAR" in result.content
        assert "${ENV}" in result.content
        assert "make build" in result.content
        assert '"hello"' in result.content
        assert "'world'" in result.content


# ---------------------------------------------------------------------------
# Dynamic discovery: loading nested skills (ADR 0043)
# ---------------------------------------------------------------------------


def _make_nested_skill(
    workspace: Path,
    subpath: str,
    config_dir: str,
    skill_dir_name: str,
    content: str | None = None,
) -> Path:
    """Create a SKILL.md file at a nested path within the workspace.

    在工作区内的嵌套路径创建一个 SKILL.md 文件."""
    skill_dir = workspace / subpath / config_dir / SKILL_SUBDIR / skill_dir_name
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


def _make_context_with_target(workspace: Path, target: Path) -> ToolContext:
    """Build a ToolContext with a SkillTracker whose target has been moved.

    构建带 SkillTracker 的 ToolContext,其目标已被移动到指定位置."""
    discovery = FilesystemSkillDiscovery()
    tracker = SkillTracker(discovery=discovery, workspace_root=workspace)
    tracker.check_path(target)
    return ToolContext(cwd=workspace, skill_tracker=tracker)


class TestSkillToolDynamicDiscovery:
    """Tests for loading nested skills discovered via dynamic walk-up.

    测试通过动态向上遍历发现并加载嵌套技能."""

    async def test_load_nested_skill(self, tmp_path: Path) -> None:
        """A skill in a subdirectory is loadable when the tracker target
        has been moved there.

        验证跟踪目标移动到子目录后,该目录中的技能可以加载."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-commit")
        target = workspace / "src" / "foo"
        context = _make_context_with_target(workspace, target)
        tool = SkillTool()
        result = await tool.execute({"name": "nested-commit"}, context)
        assert not result.is_error
        assert "Nested body" in result.content

    async def test_nested_skill_base_dir_in_output(self, tmp_path: Path) -> None:
        """The base_dir in the output should be the nested directory.

        验证输出中的 base_dir 应为嵌套目录."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested")
        target = workspace / "src" / "foo"
        context = _make_context_with_target(workspace, target)
        tool = SkillTool()
        result = await tool.execute({"name": "nested"}, context)
        assert not result.is_error
        assert str((workspace / "src" / "foo").resolve()) in result.content

    async def test_deeper_skill_shadows_root_in_tool(self, tmp_path: Path) -> None:
        """When a skill name exists at both nested and root levels, the
        SkillTool loads the deeper (nested) one.

        验证同名技能同时存在时 SkillTool 会加载更深的嵌套技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Root\n---\n\nRoot body.\n",
        )
        _make_nested_skill(
            workspace,
            "src/foo",
            ".neuro",
            "shared",
            content="---\nname: shared\ndescription: Nested\n---\n\nNested body.\n",
        )
        target = workspace / "src" / "foo"
        context = _make_context_with_target(workspace, target)
        tool = SkillTool()
        result = await tool.execute({"name": "shared"}, context)
        assert not result.is_error
        assert "Nested body" in result.content
        assert "Root body" not in result.content

    async def test_nested_skill_not_found_without_target_move(self, tmp_path: Path) -> None:
        """Without moving the target, a nested skill is not in the catalog.

        验证不移动 target 时,嵌套技能不会出现在目录中."""
        workspace = _make_workspace(tmp_path)
        _make_nested_skill(workspace, "src/foo", ".neuro", "hidden")
        context = _make_context(workspace)
        tool = SkillTool()
        with pytest.raises(ToolError, match="not found"):
            await tool.execute({"name": "hidden"}, context)

    async def test_root_skill_still_loadable_with_nested_target(self, tmp_path: Path) -> None:
        """Root-level skills are still loadable when the target is nested.

        验证 target 位于嵌套目录时仍可加载根目录技能."""
        workspace = _make_workspace(tmp_path)
        _make_skill(workspace, ".neuro", "root-skill")
        _make_nested_skill(workspace, "src/foo", ".neuro", "nested-skill")
        target = workspace / "src" / "foo"
        context = _make_context_with_target(workspace, target)
        tool = SkillTool()
        # Both root and nested skills should be loadable
        result = await tool.execute({"name": "root-skill"}, context)
        assert not result.is_error
        assert "Body text" in result.content
        result = await tool.execute({"name": "nested-skill"}, context)
        assert not result.is_error
        assert "Nested body" in result.content


# ---------------------------------------------------------------------------
# apply_skill_substitutions domain function tests
# ---------------------------------------------------------------------------


class TestApplySkillSubstitutions:
    """Tests for the apply_skill_substitutions domain function.

    测试用于该应用_技能_substitutions 领域函数."""

    def test_no_tokens_no_args(self) -> None:
        """Body with no tokens and no args is returned unchanged.

        验证正文没有 token 且没有参数时原样返回."""
        body = "Just a plain skill body."
        assert apply_skill_substitutions(body) == body

    def test_no_tokens_with_args_appends_suffix(self) -> None:
        """Body with no tokens but non-empty args gets **ARGUMENTS:** suffix.

        验证正文没有 token 但参数非空时会追加 **ARGUMENTS:** 后缀."""
        body = "Just a plain skill body."
        result = apply_skill_substitutions(body, args="fix typo")
        assert result == "Just a plain skill body.\n\n**ARGUMENTS:** fix typo"

    def test_no_tokens_with_empty_args_no_suffix(self) -> None:
        """Body with no tokens and empty args gets no suffix.

        验证正文没有 token 且参数为空时不追加后缀."""
        body = "Just a plain skill body."
        assert apply_skill_substitutions(body, args="") == body
        assert apply_skill_substitutions(body, args=None) == body

    def test_arguments_full_substitution(self) -> None:
        """$ARGUMENTS is replaced with the full args string.

        验证 $ARGUMENTS 会替换为完整参数字符串."""
        body = "Run: $ARGUMENTS"
        result = apply_skill_substitutions(body, args="npm test --watch")
        assert result == "Run: npm test --watch"

    def test_arguments_full_no_args(self) -> None:
        """$ARGUMENTS expands to empty string when no args.

        验证没有参数时 $ARGUMENTS 展开为空字符串."""
        body = "Run: $ARGUMENTS"
        assert apply_skill_substitutions(body) == "Run: "

    def test_arguments_indexed(self) -> None:
        """$ARGUMENTS[N] is replaced with the Nth whitespace-split arg.

        验证 $ARGUMENTS[N] 会替换为按空白分割后的第 N 个参数."""
        body = "First: $ARGUMENTS[0], Second: $ARGUMENTS[1]"
        result = apply_skill_substitutions(body, args="hello world")
        assert result == "First: hello, Second: world"

    def test_arguments_indexed_out_of_range(self) -> None:
        """$ARGUMENTS[N] expands to empty string when N is out of range.

        验证 N 超出范围时 $ARGUMENTS[N] 展开为空字符串."""
        body = "Third: $ARGUMENTS[2]"
        result = apply_skill_substitutions(body, args="only one")
        assert result == "Third: "

    def test_arguments_indexed_high_to_low(self) -> None:
        """$ARGUMENTS[10] is not partially matched by $ARGUMENTS[1].

        验证 $ARGUMENTS[10] 不会被部分匹配为 $ARGUMENTS[1]."""
        body = "A: $ARGUMENTS[1], B: $ARGUMENTS[10]"
        args = " ".join(f"arg{i}" for i in range(11))
        result = apply_skill_substitutions(body, args=args)
        assert result == "A: arg1, B: arg10"

    def test_shorthand_dollar_n(self) -> None:
        """$N is shorthand for $ARGUMENTS[N].

        验证 $N 是 $ARGUMENTS[N] 的简写."""
        body = "First: $0, Second: $1"
        result = apply_skill_substitutions(body, args="hello world")
        assert result == "First: hello, Second: world"

    def test_shorthand_digit_guard(self) -> None:
        """$100 is not treated as $1 + '00'.

        验证 $100 不会被当作 $1 加上 '00'."""
        body = "Value: $100"
        result = apply_skill_substitutions(body, args="a b c")
        # The Rust baseline only probes a bounded positional-index window;
        # unsupported tokens stay literal and therefore do not consume args.
        assert result == "Value: $100\n\n**ARGUMENTS:** a b c"

    def test_shorthand_high_to_low(self) -> None:
        """$10 is replaced before $1 to avoid partial matches.

        验证替换 $10 先于 $1,以避免部分匹配."""
        body = "A: $1, B: $10"
        args = " ".join(f"v{i}" for i in range(11))
        result = apply_skill_substitutions(body, args=args)
        assert result == "A: v1, B: v10"

    def test_skill_dir_substitution(self) -> None:
        """${SKILL_DIR} is replaced with the skill directory path.

        验证 ${SKILL_DIR} 会替换为技能目录路径."""
        body = "Scripts are in ${SKILL_DIR}/scripts"
        result = apply_skill_substitutions(body, skill_dir="/home/user/.neuro/skills/deploy")
        assert result == "Scripts are in /home/user/.neuro/skills/deploy/scripts"

    def test_skill_dir_no_value(self) -> None:
        """${SKILL_DIR} is left unchanged when skill_dir is None.

        验证 skill_dir 为 None 时 ${SKILL_DIR} 保持不变."""
        body = "Scripts are in ${SKILL_DIR}/scripts"
        result = apply_skill_substitutions(body, skill_dir=None)
        assert result == "Scripts are in ${SKILL_DIR}/scripts"

    def test_skill_dir_with_args_appends_suffix(self) -> None:
        """Body with only ${SKILL_DIR} (no arg tokens) still gets suffix.

        验证正文只有 ${SKILL_DIR} 且没有参数 token 时仍会追加后缀."""
        body = "Dir: ${SKILL_DIR}"
        result = apply_skill_substitutions(body, args="extra", skill_dir="/skills/test")
        assert result == "Dir: /skills/test\n\n**ARGUMENTS:** extra"

    def test_arg_tokens_prevent_suffix(self) -> None:
        """Body with argument tokens does NOT get **ARGUMENTS:** suffix.

        验证正文包含参数 token 时不会追加 **ARGUMENTS:** 后缀."""
        body = "Run: $ARGUMENTS"
        result = apply_skill_substitutions(body, args="npm test")
        assert "**ARGUMENTS:**" not in result
        assert result == "Run: npm test"

    def test_mixed_tokens(self) -> None:
        """Body with both arg and path tokens: args expand inline, no suffix.

        验证正文同时包含参数和路径 token 时参数内联展开且不追加后缀."""
        body = "Dir: ${SKILL_DIR}, Args: $ARGUMENTS"
        result = apply_skill_substitutions(body, args="test", skill_dir="/skills/x")
        assert result == "Dir: /skills/x, Args: test"
        assert "**ARGUMENTS:**" not in result

    def test_unknown_tokens_unchanged(self) -> None:
        """Unknown $ tokens like ${UNKNOWN} are left unchanged.

        验证未知 $ token (例如 ${UNKNOWN}) 保持不变."""
        body = "Value: ${UNKNOWN} and $FOO"
        result = apply_skill_substitutions(body, args="test")
        assert "${UNKNOWN}" in result
        assert "$FOO" in result

    def test_backslash_in_args_escaped(self) -> None:
        """Backslashes in args are escaped for regex replacement safety.

        验证参数中的反斜杠会被转义,保证正则替换安全."""
        body = "Path: $ARGUMENTS"
        result = apply_skill_substitutions(body, args=r"C:\Users\test")
        assert r"C:\Users\test" in result

    def test_backslash_in_skill_dir_escaped(self) -> None:
        """Backslashes in skill_dir are escaped for regex replacement safety.

        验证 skill_dir 中的反斜杠会被转义,保证正则替换安全."""
        body = "Dir: ${SKILL_DIR}"
        result = apply_skill_substitutions(body, skill_dir=r"C:\skills\test")
        assert r"C:\skills\test" in result

    def test_multiple_occurrences(self) -> None:
        """Multiple occurrences of the same token are all replaced.

        验证同一 token 的多次出现都会被替换."""
        body = "$0 and $0 again"
        result = apply_skill_substitutions(body, args="hello")
        assert result == "hello and hello again"

    def test_empty_body(self) -> None:
        """Empty body with args gets suffix.

        验证空正文带参数时会追加后缀."""
        result = apply_skill_substitutions("", args="test")
        assert result == "\n\n**ARGUMENTS:** test"

    def test_args_whitespace_only(self) -> None:
        """Whitespace-only args are treated as empty (no suffix).

        验证仅包含空白的参数被视为空值,不追加后缀."""
        body = "Plain body."
        result = apply_skill_substitutions(body, args="   ")
        assert result == body

    def test_argument_byte_limit(self) -> None:
        args = "x" * (MAX_SKILL_ARGUMENT_BYTES + 1)
        with pytest.raises(ValueError, match="arguments exceed"):
            apply_skill_substitutions("$ARGUMENTS", args=args)

    def test_substitution_count_limit(self) -> None:
        body = " ".join("$0" for _ in range(MAX_SKILL_SUBSTITUTIONS + 1))
        with pytest.raises(ValueError, match="substitutions"):
            apply_skill_substitutions(body, args="value")

    def test_extremely_large_index_stays_literal(self) -> None:
        token = "$999999999999999999999999999999"
        result = apply_skill_substitutions(f"Value: {token}", args="value")
        assert result == f"Value: {token}\n\n**ARGUMENTS:** value"


# ---------------------------------------------------------------------------
# SkillTool substitution integration tests
# ---------------------------------------------------------------------------


class TestSkillToolSubstitution:
    """Tests for SkillTool variable substitution integration.

    测试 SkillTool 的变量替换集成."""

    async def test_args_substituted_in_body(self, tmp_path: Path) -> None:
        """$ARGUMENTS in the skill body is replaced with the args value.

        验证技能正文中的 $ARGUMENTS 会替换为参数值."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "deploy",
            content=("---\nname: deploy\ndescription: Deploy\n---\n\nRun: $ARGUMENTS\n"),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "deploy", "args": "npm run deploy"}, context)
        assert not result.is_error
        assert "Run: npm run deploy" in result.content
        assert "$ARGUMENTS" not in result.content

    async def test_skill_dir_substituted_in_body(self, tmp_path: Path) -> None:
        """${SKILL_DIR} in the skill body is replaced with the skill directory.

        验证技能正文中的 ${SKILL_DIR} 会替换为技能目录."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "deploy",
            content=(
                "---\nname: deploy\ndescription: Deploy\n---\n\nScripts: ${SKILL_DIR}/scripts\n"
            ),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "deploy"}, context)
        assert not result.is_error
        expected_dir = str((workspace / ".neuro" / "skills" / "deploy").resolve())
        assert f"Scripts: {expected_dir}/scripts" in result.content
        assert "${SKILL_DIR}" not in result.content

    async def test_no_args_no_substitution(self, tmp_path: Path) -> None:
        """Without args, $ARGUMENTS expands to empty and no suffix is added.

        验证没有参数时 $ARGUMENTS 展开为空且不追加后缀."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "deploy",
            content=("---\nname: deploy\ndescription: Deploy\n---\n\nRun: $ARGUMENTS\n"),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "deploy"}, context)
        assert not result.is_error
        # $ARGUMENTS expands to empty; body.strip() removes trailing space.
        assert "Run:" in result.content
        assert "$ARGUMENTS" not in result.content
        assert "**ARGUMENTS:**" not in result.content

    async def test_args_with_no_tokens_appends_suffix(self, tmp_path: Path) -> None:
        """When body has no tokens, args are appended as **ARGUMENTS:** suffix.

        验证正文没有 token 时参数会作为 **ARGUMENTS:** 后缀追加."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "plain",
            content="---\nname: plain\ndescription: Plain\n---\n\nJust a body.\n",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "plain", "args": "extra info"}, context)
        assert not result.is_error
        assert "**ARGUMENTS:** extra info" in result.content

    async def test_args_empty_string_ignored(self, tmp_path: Path) -> None:
        """Empty string args are treated as no args.

        验证空字符串参数被视为没有参数."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "plain",
            content="---\nname: plain\ndescription: Plain\n---\n\nJust a body.\n",
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "plain", "args": ""}, context)
        assert not result.is_error
        assert "**ARGUMENTS:**" not in result.content

    async def test_indexed_args_in_tool(self, tmp_path: Path) -> None:
        """$0 and $1 shorthand work through the SkillTool.

        验证通过 SkillTool 可以使用 $0 和 $1 简写."""
        workspace = _make_workspace(tmp_path)
        _make_skill(
            workspace,
            ".neuro",
            "greet",
            content=("---\nname: greet\ndescription: Greet\n---\n\nHello $0 from $1!\n"),
        )
        context = _make_context(workspace)
        tool = SkillTool()
        result = await tool.execute({"name": "greet", "args": "World Earth"}, context)
        assert not result.is_error
        assert "Hello World from Earth!" in result.content
