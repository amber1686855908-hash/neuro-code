"""Skill body loading tool.

Allows the model to load the full body of a discovered skill when it decides
to use one.  The model first sees a compact listing of available skills
(name + description + when-to-use) via the synthetic ``AVAILABLE_SKILLS``
message injected by :class:`AgentRuntime`.  When the model decides a skill
is relevant, it calls this tool with the skill name (and optional arguments)
to load the full body.

The tool follows the same bounded, symlink-resistant read pattern as
instruction and skill discovery (see
:func:`neuro_code.adapters.instruction_discovery._toctou_safe_read`).
The YAML frontmatter is stripped before returning the body — the model
receives only the skill's guidance content, not the metadata that was
already shown in the listing.

Variable substitution is performed on the loaded body: ``$ARGUMENTS``,
``$ARGUMENTS[N]``, ``$N``, and ``${SKILL_DIR}`` are expanded using the
optional ``args`` parameter and the skill's base directory.  See
:func:`neuro_code.domain.skills.apply_skill_substitutions` for the full
substitution semantics.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.adapters.instruction_discovery import (
    _is_symlink_or_reparse_point,
    _resolve_within_workspace,
    _toctou_safe_read,
)
from neuro_code.async_utils import run_blocking
from neuro_code.domain.instructions import InstructionRejectionReason
from neuro_code.domain.skills import (
    MAX_SINGLE_SKILL_BYTES,
    SkillInfo,
    _contains_control_characters,
    apply_skill_substitutions,
    extract_skill_body,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.errors import ToolError
from neuro_code.ports.tools import ToolContext

# Maximum number of bundled files to list alongside the skill body.
_MAX_BUNDLED_FILES = 10
_MAX_BUNDLED_DIRECTORY_ENTRIES = 256


class SkillTool:
    """Load the full body of a discovered skill by name.

    The tool is read-only (``side_effecting = False``).  It looks up the
    skill by name in the current discovery result, resolves the absolute
    path from the workspace root and the skill's relative path, reads the
    file using the same bounded, symlink-resistant read as discovery, strips
    the YAML frontmatter, applies variable substitution (``$ARGUMENTS``,
    ``$N``, ``${SKILL_DIR}``), and returns the body wrapped in a
    ``<skill_content>`` XML block with the base directory and a sample of
    bundled files.
    """

    definition = ToolDefinition(
        name="skill",
        description=(
            "Load the full content of a discovered skill by name. Skills are "
            "read-only reference documents (SKILL.md files) that describe "
            "best practices for specific tasks. Use this tool when a skill's "
            "description or when-to-use trigger matches the current task. "
            "Optionally pass arguments that will be substituted into the "
            "skill body via $ARGUMENTS, $N, and ${SKILL_DIR} tokens."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the skill to load.",
                },
                "args": {
                    "type": "string",
                    "description": (
                        "Optional arguments to substitute into the skill body. "
                        "Use $ARGUMENTS for the full string, $ARGUMENTS[N] or "
                        "$N for the Nth whitespace-split argument (0-indexed), "
                        "and ${SKILL_DIR} for the skill's directory."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw_name = arguments.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ToolError("name must be a non-empty string")
        skill_name = raw_name.strip()

        raw_args = arguments.get("args")
        skill_args: str | None = None
        if raw_args is not None and not isinstance(raw_args, str):
            raise ToolError("args must be a string")
        if isinstance(raw_args, str) and raw_args.strip():
            skill_args = raw_args.strip()
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")

        tracker = context.skill_tracker
        if tracker is None:
            raise ToolError("skill discovery is not available in this context")

        result = tracker.current_result()
        skill = _find_skill(skill_name, result.files)
        if skill is None:
            available = ", ".join(s.name for s in result.files) or "(none)"
            raise ToolError(f"skill '{skill_name}' not found. Available skills: {available}")

        # Resolve the absolute path from the skill's discovery root and its
        # POSIX-style relative path.  SkillInfo.root is set by the adapter:
        # workspace root for LOCAL scope, user home for USER scope.  When
        # root is None (e.g., mock SkillInfo), fall back to the tracker's
        # workspace root for backward compatibility.
        discovery_root = skill.root if skill.root is not None else tracker.workspace_root
        if _contains_control_characters(str(discovery_root)):
            raise ToolError("skill discovery root contains control characters")
        absolute_path = discovery_root / skill.relative_path

        # Defence in depth: verify the resolved path is within the
        # discovery root boundary.
        if _resolve_within_workspace(absolute_path, discovery_root) is None:
            raise ToolError(f"skill path escapes discovery root: {skill.relative_path}")

        base_dir = absolute_path.parent

        def load() -> str:
            """Read the skill file, strip frontmatter, substitute, and format."""
            raw, read_reason = _toctou_safe_read(absolute_path, MAX_SINGLE_SKILL_BYTES)
            if read_reason is not None:
                raise ToolError(
                    f"could not read skill file '{skill.relative_path}': "
                    f"{_describe_read_reason(read_reason)}"
                )
            # Strip BOM if present (consistent with discovery).
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ToolError(f"skill file '{skill.relative_path}' is not valid UTF-8") from None
            if _contains_control_characters(content):
                raise ToolError(f"skill file '{skill.relative_path}' contains control characters")
            content_fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if skill.content_fingerprint and content_fingerprint != skill.content_fingerprint:
                raise ToolError(
                    f"skill file '{skill.relative_path}' changed during loading; retry the call"
                )
            body = extract_skill_body(content)
            # Apply variable substitution ($ARGUMENTS, $N, ${SKILL_DIR}).
            try:
                body = apply_skill_substitutions(body, skill_args, str(base_dir))
            except ValueError as error:
                raise ToolError(str(error)) from error
            file_list = _list_bundled_files(base_dir)
            rendered = _format_output(skill, body, base_dir, file_list)
            if len(rendered.encode("utf-8")) > context.output_byte_limit:
                raise ToolError(
                    f"rendered skill exceeds the {context.output_byte_limit}-byte output limit"
                )
            return rendered

        output = await run_blocking(load)
        return ToolResult(
            content=output,
            metadata={
                "skill_name": skill.name,
                "path": skill.relative_path,
                "scope": skill.scope.name.lower(),
            },
        )


def _find_skill(name: str, skills: tuple[SkillInfo, ...]) -> SkillInfo | None:
    """Find a skill by exact name match.

    Skills are deduplicated by name during discovery (first-seen wins),
    so there is at most one match.
    """
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def _describe_read_reason(reason: InstructionRejectionReason) -> str:
    """Convert a read rejection reason to a human-readable description."""
    if reason is InstructionRejectionReason.SYMLINK_NOT_SUPPORTED:
        return "file is a symlink or reparse point (not supported)"
    if reason is InstructionRejectionReason.NOT_A_FILE:
        return "path is not a regular file"
    if reason is InstructionRejectionReason.READ_ERROR:
        return "read error (file may have been modified or moved)"
    return reason.value


def _list_bundled_files(skill_dir: Path) -> list[str]:
    """List up to ``_MAX_BUNDLED_FILES`` files in the skill directory.

    Excludes ``SKILL.md`` itself.  Returns an empty list if the directory
    cannot be read. Files are sorted alphabetically by name. Symlinks,
    reparse points, directories, and names containing control characters
    are omitted.
    """
    try:
        directory_stat = skill_dir.lstat()
        if _is_symlink_or_reparse_point(directory_stat):
            return []
        children: list[Path] = []
        with os.scandir(skill_dir) as iterator:
            for entry in iterator:
                children.append(Path(entry.path))
                if len(children) > _MAX_BUNDLED_DIRECTORY_ENTRIES:
                    return []
    except OSError:
        return []
    files: list[str] = []
    for child in sorted(children, key=lambda path: path.name):
        if child.name == "SKILL.md" or _contains_control_characters(child.name):
            continue
        try:
            child_stat = child.lstat()
        except OSError:
            continue
        if _is_symlink_or_reparse_point(child_stat):
            continue
        if stat_module.S_ISREG(child_stat.st_mode):
            files.append(child.name)
            if len(files) >= _MAX_BUNDLED_FILES:
                break
    return files


def _format_output(
    skill: SkillInfo,
    body: str,
    base_dir: Path,
    file_list: list[str],
) -> str:
    """Format the skill body as a ``<skill_content>`` XML block.

    The output includes:
    - The skill name as a header.
    - The skill body (after frontmatter stripping).
    - The base directory path for resolving relative paths.
    - A sample of bundled files (up to 10) in the skill directory.
    """
    lines: list[str] = []
    lines.append(f'<skill_content name="{skill.name}">')
    lines.append(f"# Skill: {skill.name}")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    lines.append(f"Base directory for this skill: file://{base_dir}")
    lines.append(
        "Relative paths in this skill (e.g., scripts/, reference/) are "
        "relative to this base directory."
    )
    if file_list:
        lines.append("")
        lines.append("Bundled files in this skill directory:")
        for f in file_list:
            lines.append(f"- {f}")
    lines.append("</skill_content>")
    return "\n".join(lines)


__all__ = ["SkillTool"]
