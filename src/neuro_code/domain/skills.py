"""Domain model for read-only skill file discovery.

Skill files (SKILL.md) are repository-provided best-practice documents that
describe how to handle specific tasks.  Like instruction files (AGENTS.md),
they are never executed, never loaded from the network, and never allowed to
impersonate system or user messages.  All discovery is deterministic, bounded,
and fail-closed.

Unlike instruction files, which are inherited along the directory chain from
workspace root to the current target, skills are discovered in dedicated
``skills/`` subdirectories inside configuration directories (``.neuro``,
``.agents``, ``.grok``, ``.claude``).  Each SKILL.md file has YAML
frontmatter with metadata (name, description, when-to-use) that is parsed
without requiring a YAML library — a simple line-based parser handles the
common case, and skills with malformed frontmatter still load using their
directory name as a fallback.

Skills are ordered by scope priority (Local > Repo > User) and deduplicated
by name (first-seen wins). The model receives a compact listing (name +
description + when-to-use); it can load a selected body through the read-only
``skill`` tool.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neuro_code.domain.messages import Message

# ---------------------------------------------------------------------------
# Discovery limits
# ---------------------------------------------------------------------------

SKILL_FILENAME = "SKILL.md"

# Configuration directories that may contain a ``skills/`` subdirectory.
# Ordered by product-specific priority: .neuro first, then generic/compat.
SKILL_CONFIG_DIRS: tuple[str, ...] = (".neuro", ".agents", ".grok", ".claude")

# The subdirectory name inside a config dir that holds skill definitions.
SKILL_SUBDIR = "skills"

MAX_SKILL_FILES = 50
MAX_SKILL_CANDIDATES = 200
MAX_SKILL_DIRECTORIES = 200
MAX_SKILL_DIRECTORY_ENTRIES = 1_000
MAX_SINGLE_SKILL_BYTES = 65_536  # 64 KiB per file (frontmatter + body)
MAX_TOTAL_SKILL_BYTES = 524_288  # 512 KiB across all files
MAX_SKILL_WALK_DEPTH = 5  # recursive depth inside skills/
MAX_SKILL_ANCESTOR_DEPTH = 64  # target-to-workspace discovery walk
MAX_SKILL_CATALOG_BYTES = 65_536
MAX_SKILL_ARGUMENT_BYTES = 8_192
MAX_SKILL_SUBSTITUTIONS = 32

# Frontmatter parsing limits.
MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
MAX_FRONTMATTER_BYTES = 4096

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)^---[ \t]*(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Scope priority
# ---------------------------------------------------------------------------


class SkillScope(IntEnum):
    """Scope/priority of a skill based on where it was discovered.

    Lower values have higher priority (Local overrides Repo overrides User).
    """

    LOCAL = 0  # target-to-workspace ancestors (highest priority)
    REPO = 1  # ancestors above the workspace through the git root
    USER = 2  # ~/.neuro/skills (user home)


# ---------------------------------------------------------------------------
# Rejection reasons
# ---------------------------------------------------------------------------


class SkillRejectionReason(StrEnum):
    """Why a candidate skill file was not loaded."""

    ESCAPES_WORKSPACE = "escapes-workspace"
    SYMLINK_ESCAPE = "symlink-escape"
    CIRCULAR_SYMLINK = "circular-symlink"
    SYMLINK_NOT_SUPPORTED = "symlink-not-supported"
    TOO_MANY_FILES = "too-many-files"
    FILE_TOO_LARGE = "file-too-large"
    TOTAL_TOO_LARGE = "total-too-large"
    TOO_DEEP = "too-deep"
    INVALID_ENCODING = "invalid-encoding"
    CONTROL_CHARACTERS = "control-characters"
    NOT_A_FILE = "not-a-file"
    READ_ERROR = "read-error"
    INVALID_NAME = "invalid-name"
    NO_FRONTMATTER = "no-frontmatter"
    TOO_MANY_ENTRIES = "too-many-entries"
    TOO_MANY_DIRECTORIES = "too-many-directories"


# ---------------------------------------------------------------------------
# Control character validation (shared with instruction domain)
# ---------------------------------------------------------------------------

# C0 control characters (0x00-0x1F) excluding common whitespace (\t \n \r),
# plus DEL (0x7F), plus C1 control characters (0x80-0x9F).
_FORBIDDEN_CONTROL_CHARS = (
    frozenset(chr(c) for c in range(0x20) if c not in (9, 10, 13))
    | {chr(0x7F)}
    | frozenset(chr(c) for c in range(0x80, 0xA0))
)


def _contains_control_characters(text: str) -> bool:
    """Return True if *text* contains forbidden control characters."""
    return any(ch in _FORBIDDEN_CONTROL_CHARS for ch in text)


# ---------------------------------------------------------------------------
# Skill name normalization
# ---------------------------------------------------------------------------


def normalize_skill_name(name: str) -> str:
    """Normalize a skill name into a slug.

    Lowercase, map any character that is not ``[a-z0-9]`` to a hyphen,
    collapse consecutive hyphens, and trim leading/trailing hyphens.
    """
    result: list[str] = []
    for ch in name.strip().lower():
        if ch.isascii() and (ch.isalpha() or ch.isdigit()):
            result.append(ch)
        else:
            result.append("-")
    # Collapse consecutive hyphens and trim.
    collapsed = re.sub(r"-+", "-", "".join(result))
    return collapsed.strip("-")


def is_valid_skill_name(name: str) -> bool:
    """Return True if *name* is a valid skill slug."""
    return (
        bool(name)
        and len(name) <= MAX_NAME_LEN
        and not name.startswith("-")
        and not name.endswith("-")
        and "--" not in name
        and all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in name)
    )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedFrontmatter:
    """Result of parsing YAML frontmatter from a SKILL.md file."""

    name: str
    description: str
    when_to_use: str | None
    has_user_specified_description: bool


def _strip_quotes(value: str) -> str:
    """Strip one surrounding matched quote pair from *value*."""
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
    ):
        return value[1:-1]
    return value


def _strip_inline_comment(value: str) -> str:
    """Strip an inline ``# comment`` from an unquoted YAML scalar.

    Only strips when `` #`` (space-hash) appears outside of quotes.
    """
    in_double = False
    in_single = False
    for i, ch in enumerate(value):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif (
            ch == "#" and not in_double and not in_single and i > 0 and value[i - 1] in (" ", "\t")
        ):
            return value[:i].rstrip()
    return value


def parse_frontmatter(
    content: str,
    fallback_name: str | None = None,
) -> ParsedFrontmatter | None:
    """Parse YAML frontmatter from SKILL.md *content*.

    Returns ``None`` if no frontmatter delimiters are found.  Uses a simple
    line-based parser that handles common ``key: value`` pairs without
    requiring PyYAML as a dependency.

    Malformed frontmatter does not raise — the skill loads with whatever
    fields were successfully parsed, falling back to *fallback_name* for the
    name and an empty string for the description.
    """
    stripped = content.lstrip()
    match = _FRONTMATTER_RE.match(stripped)
    if match is None:
        return None

    yaml_content = match.group("yaml").strip()

    # Cap frontmatter size to avoid processing huge blobs.
    encoded_frontmatter = yaml_content.encode("utf-8")
    if len(encoded_frontmatter) > MAX_FRONTMATTER_BYTES:
        yaml_content = encoded_frontmatter[:MAX_FRONTMATTER_BYTES].decode("utf-8", "ignore")

    fields: dict[str, str] = {}
    for line in yaml_content.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Must look like ``key: value`` — split on the first colon.
        colon_idx = line.find(":")
        if colon_idx <= 0:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1 :].strip()
        if not value:
            continue
        value = _strip_inline_comment(value)
        value = _strip_quotes(value).strip()
        if value:
            fields[key] = value

    # Name: frontmatter name, or fallback to directory name.
    fm_name = fields.get("name")
    name_candidates = [fm_name, fallback_name] if fm_name else [fallback_name]
    name = ""
    for candidate in name_candidates:
        if candidate is None:
            continue
        normalized = normalize_skill_name(candidate)
        if is_valid_skill_name(normalized):
            name = normalized
            break

    if not name:
        return None

    description = fields.get("description", "")
    if description:
        description = _cap_string(description, MAX_DESCRIPTION_LEN)

    when_to_use = fields.get("when-to-use") or fields.get("when_to_use")
    when_to_use = _cap_string(when_to_use, MAX_DESCRIPTION_LEN) if when_to_use else None

    return ParsedFrontmatter(
        name=name,
        description=description,
        when_to_use=when_to_use,
        has_user_specified_description=bool(description),
    )


def _cap_string(s: str, max_len: int) -> str:
    """Cap a string at *max_len* characters."""
    if len(s) > max_len:
        return s[:max_len]
    return s


def extract_skill_body(content: str) -> str:
    """Extract the body of a skill file (everything after YAML frontmatter).

    Returns the content (with leading whitespace stripped) if no frontmatter
    delimiters are found.  Uses a simple string scan — the same approach as
    the grok-build Rust baseline — to strip everything between the opening
    ``---`` and the closing ``\\n---``.

    This function does **not** parse the frontmatter; it only removes it.
    Frontmatter parsing (for name, description, when-to-use) is done
    separately at discovery time by :func:`parse_frontmatter`.
    """
    stripped = content.lstrip()
    match = _FRONTMATTER_RE.match(stripped)
    if match is None:
        return stripped
    return stripped[match.end() :].lstrip()


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

# Regex patterns for substitution tokens.
_RE_ARG_INDEXED = re.compile(r"\$ARGUMENTS\[(\d+)\]")
_RE_ARG_SHORTHAND = re.compile(r"\$(\d+)")
_RE_ARG_FULL = re.compile(r"\$ARGUMENTS(?!\[)")
_RE_SKILL_DIR = re.compile(r"\$\{SKILL_DIR\}")


def _parse_supported_index(raw_index: str, upper_bound: int) -> int | None:
    """Return a safe positional index, leaving price-like tokens untouched.

    The Rust baseline only probes indices through ``max(argv, 1) + 19``.
    Mirroring that bounded window prevents a literal such as ``$100`` from
    being interpreted as an argument when only a few arguments were passed,
    and avoids converting attacker-controlled, arbitrarily large integers.
    """
    if len(raw_index) > 6:
        return None
    index = int(raw_index)
    return index if index < upper_bound else None


def _supported_argument_token_count(body: str, upper_bound: int) -> int:
    indexed = sum(
        _parse_supported_index(match.group(1), upper_bound) is not None
        for match in _RE_ARG_INDEXED.finditer(body)
    )
    shorthand = sum(
        _parse_supported_index(match.group(1), upper_bound) is not None
        for match in _RE_ARG_SHORTHAND.finditer(body)
    )
    return indexed + shorthand + len(_RE_ARG_FULL.findall(body))


def apply_skill_substitutions(
    body: str,
    args: str | None = None,
    skill_dir: str | None = None,
) -> str:
    """Apply variable substitution to a skill body.

    Supported tokens (substituted in this order to avoid partial matches):

    1. ``$ARGUMENTS[N]`` — the Nth whitespace-split argument (0-indexed).
       Replaced high-to-low index so ``$ARGUMENTS[10]`` is not partially
       matched by ``$ARGUMENTS[1]``.
    2. ``$N`` — shorthand for ``$ARGUMENTS[N]``.  Replaced high-to-low with
       a digit-following guard so ``$100`` is not treated as ``$1`` + ``"00"``.
    3. ``$ARGUMENTS`` — the full arguments string.
    4. ``${SKILL_DIR}`` — the directory containing the SKILL.md file.

    **Suffix rule** (matching grok-build):

    - If the body contains any *argument* token (``$ARGUMENTS``,
      ``$ARGUMENTS[N]``, or ``$N``), arguments are expanded inline and **no**
      suffix is appended.
    - If the body contains only *path* tokens (``${SKILL_DIR}``) but no
      argument token, those are expanded and the arguments are still appended
      as ``\\n\\n**ARGUMENTS:** {args}`` (when *args* is non-empty).
    - If no substitution token is present at all and *args* is non-empty, the
      suffix is appended for backward compatibility.
    - Unknown ``$`` tokens (e.g. ``$100``, ``${UNKNOWN}``) are left unchanged.

    When *args* is ``None`` or empty, argument tokens expand to the empty
    string and no suffix is appended.
    """
    args_str = args.strip() if args is not None else ""
    if len(args_str.encode("utf-8")) > MAX_SKILL_ARGUMENT_BYTES:
        raise ValueError(f"skill arguments exceed {MAX_SKILL_ARGUMENT_BYTES} bytes")
    arg_list = args_str.split() if args_str else []
    index_upper_bound = max(len(arg_list), 1) + 20
    argument_token_count = _supported_argument_token_count(body, index_upper_bound)
    path_token_count = len(_RE_SKILL_DIR.findall(body))
    if argument_token_count + path_token_count > MAX_SKILL_SUBSTITUTIONS:
        raise ValueError(f"skill body exceeds {MAX_SKILL_SUBSTITUTIONS} substitutions")

    def indexed_replacement(match: re.Match[str]) -> str:
        index = _parse_supported_index(match.group(1), index_upper_bound)
        if index is None:
            return match.group(0)
        return arg_list[index] if index < len(arg_list) else ""

    def shorthand_replacement(match: re.Match[str]) -> str:
        index = _parse_supported_index(match.group(1), index_upper_bound)
        if index is None:
            return match.group(0)
        return arg_list[index] if index < len(arg_list) else ""

    # Callable replacements preserve backslashes literally and avoid regex
    # replacement-string interpretation on Windows paths.
    result = _RE_ARG_INDEXED.sub(indexed_replacement, body)
    result = _RE_ARG_SHORTHAND.sub(shorthand_replacement, result)

    # 3. $ARGUMENTS — full string.
    result = _RE_ARG_FULL.sub(lambda _match: args_str, result)

    # 4. ${SKILL_DIR} — skill directory.
    if skill_dir is not None:
        result = _RE_SKILL_DIR.sub(lambda _match: skill_dir, result)

    # Suffix rule: append **ARGUMENTS:** when the body had no argument tokens
    # but args are non-empty.
    if args_str and argument_token_count == 0:
        result = result + f"\n\n**ARGUMENTS:** {args_str}"

    return result


# ---------------------------------------------------------------------------
# Domain value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """One successfully discovered skill (metadata only, no body)."""

    name: str  # normalized slug
    description: str  # from frontmatter or body fallback
    when_to_use: str | None  # optional trigger phrase
    relative_path: str  # POSIX-style relative path from the discovery root
    scope: SkillScope
    depth: int  # depth within the skills/ directory tree
    root: Path | None = None  # base directory for resolving relative_path
    content_fingerprint: str = ""  # body-inclusive digest, never injected directly

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("skill name must not be empty")
        if not is_valid_skill_name(self.name):
            raise ValueError("skill name must be a valid normalized slug")
        if not self.relative_path:
            raise ValueError("skill relative path must not be empty")
        if "\x00" in self.relative_path:
            raise ValueError("skill path must not contain NUL")
        posix_path = PurePosixPath(self.relative_path)
        windows_path = PureWindowsPath(self.relative_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in posix_path.parts
            or "\\" in self.relative_path
        ):
            raise ValueError("skill path must be a normalized relative POSIX path")
        if self.depth < 0:
            raise ValueError("skill depth must be non-negative")
        if _contains_control_characters(self.relative_path):
            raise ValueError("skill path must not contain control characters")
        if _contains_control_characters(self.description) or (
            self.when_to_use is not None and _contains_control_characters(self.when_to_use)
        ):
            raise ValueError("skill metadata must not contain control characters")
        if self.content_fingerprint and (
            len(self.content_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.content_fingerprint)
        ):
            raise ValueError("skill content fingerprint must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class SkillRejection:
    """A candidate skill file that was not loaded, with the reason."""

    relative_path: str
    reason: SkillRejectionReason
    scope: SkillScope | None = None

    def __post_init__(self) -> None:
        if not self.relative_path:
            raise ValueError("rejection path must not be empty")
        if _contains_control_characters(self.relative_path):
            raise ValueError("rejection path must not contain control characters")


@dataclass(frozen=True, slots=True)
class SkillDiscoveryResult:
    """Outcome of one skill discovery pass over a workspace."""

    files: tuple[SkillInfo, ...]
    rejections: tuple[SkillRejection, ...]
    fingerprint: str  # SHA-256 hex digest of ordered skill metadata

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "rejections", tuple(self.rejections))

    @property
    def loaded_count(self) -> int:
        return len(self.files)

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)

    def model_context_text(self) -> str:
        """Render discovered skills as a compact listing for the model.

        Only skill metadata (name, description, when-to-use) is shown. Full
        bodies remain out of the prompt until explicitly loaded with the
        read-only ``skill`` tool.
        """
        if not self.files:
            return ""
        entries: list[str] = []
        for skill in self.files:
            entry = f"- {skill.name}"
            if skill.description:
                entry += f": {skill.description}"
            if skill.when_to_use:
                entry += f" (when to use: {skill.when_to_use})"
            entries.append(entry)
        header = (
            "The following skills are available in this workspace. Skills are "
            "read-only reference documents that describe best practices for "
            "specific tasks. They are not automatically executed. When a user "
            "asks for something that matches a skill's description or "
            "when-to-use trigger, load its full body with the `skill` tool before "
            "following its guidance. Skills are listed by scope priority (local "
            "first).\n"
        )
        rendered = header
        included = 0
        for entry in entries:
            candidate = rendered + entry + "\n"
            if len(candidate.encode("utf-8")) > MAX_SKILL_CATALOG_BYTES:
                break
            rendered = candidate
            included += 1
        if included < len(entries):
            rendered += f"- [{len(entries) - included} additional skills omitted by byte limit]\n"
        return rendered.rstrip()

    def skill_message(self) -> Message:
        """Build a synthetic User message carrying the skill listing.

        The message is tagged with ``SyntheticReason.AVAILABLE_SKILLS`` so
        it is distinguishable from genuine user input.  It is injected after
        the project-instructions message and before the first user message.

        Scope: this marker is an in-memory annotation, same as
        ``SyntheticReason.PROJECT_INSTRUCTIONS``.  See ``SyntheticReason``
        for the full scope statement.
        """
        from neuro_code.domain.messages import Message, Role, SyntheticReason

        return Message(
            role=Role.USER,
            content=self.model_context_text(),
            synthetic_reason=SyntheticReason.AVAILABLE_SKILLS,
        )


def compute_skill_fingerprint(files: tuple[SkillInfo, ...]) -> str:
    """Compute a stable fingerprint over ordered skill metadata."""
    hasher = hashlib.sha256()
    for skill in files:
        hasher.update(skill.name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(skill.description.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update((skill.when_to_use or "").encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(skill.relative_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(skill.scope.value).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(skill.content_fingerprint.encode("ascii"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def normalize_relative_path(path: PurePosixPath) -> str:
    """Return a normalized POSIX-style relative path string."""
    return str(path)


__all__ = [
    "MAX_DESCRIPTION_LEN",
    "MAX_FRONTMATTER_BYTES",
    "MAX_NAME_LEN",
    "MAX_SINGLE_SKILL_BYTES",
    "MAX_SKILL_ANCESTOR_DEPTH",
    "MAX_SKILL_ARGUMENT_BYTES",
    "MAX_SKILL_CANDIDATES",
    "MAX_SKILL_CATALOG_BYTES",
    "MAX_SKILL_DIRECTORIES",
    "MAX_SKILL_DIRECTORY_ENTRIES",
    "MAX_SKILL_FILES",
    "MAX_SKILL_SUBSTITUTIONS",
    "MAX_SKILL_WALK_DEPTH",
    "MAX_TOTAL_SKILL_BYTES",
    "SKILL_CONFIG_DIRS",
    "SKILL_FILENAME",
    "SKILL_SUBDIR",
    "ParsedFrontmatter",
    "SkillDiscoveryResult",
    "SkillInfo",
    "SkillRejection",
    "SkillRejectionReason",
    "SkillScope",
    "apply_skill_substitutions",
    "compute_skill_fingerprint",
    "extract_skill_body",
    "is_valid_skill_name",
    "normalize_relative_path",
    "normalize_skill_name",
    "parse_frontmatter",
]
