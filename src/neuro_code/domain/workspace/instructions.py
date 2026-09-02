"""Domain model for repository instruction file discovery.

Instruction files (AGENTS.md) are repository-provided, non-system directives
that guide agent behaviour within a workspace.  They are never executed, never
loaded from the network, and never allowed to impersonate system or user
messages.  All discovery is deterministic, bounded, and fail-closed.

定义仓库指令文件发现的领域模型. AGENTS.md 由仓库提供,不执行、不从网络加载,也不能冒充 system 或 user 消息;发现过程确定性、有界且失败关闭.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from neuro_code.domain.workspace.paths import normalize_relative_path

if TYPE_CHECKING:
    from neuro_code.domain.conversation.messages import Message

# ---------------------------------------------------------------------------
# Discovery limits
# ---------------------------------------------------------------------------

INSTRUCTION_FILENAME = "AGENTS.md"

MAX_INSTRUCTION_FILES = 10
MAX_SINGLE_FILE_BYTES = 65_536  # 64 KiB per file
MAX_TOTAL_BYTES = 262_144  # 256 KiB across all files
MAX_DIRECTORY_DEPTH = 20  # relative depth from workspace root


# ---------------------------------------------------------------------------
# Rejection reasons
# ---------------------------------------------------------------------------


class InstructionRejectionReason(StrEnum):
    """Why a candidate instruction file was not loaded.

    表示候选指令文件未被加载的原因."""

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


# ---------------------------------------------------------------------------
# Control character validation
# ---------------------------------------------------------------------------

# C0 control characters (0x00-0x1F) excluding common whitespace (\t \n \r),
# plus DEL (0x7F), plus C1 control characters (0x80-0x9F).
_FORBIDDEN_CONTROL_CHARS = (
    frozenset(chr(c) for c in range(0x20) if c not in (9, 10, 13))
    | {chr(0x7F)}
    | frozenset(chr(c) for c in range(0x80, 0xA0))
)


def _validate_no_control_characters(text: str, label: str) -> None:
    """Raise ValueError if *text* contains forbidden control characters.

    当 *text* 包含禁止的控制字符时抛出 ValueError."""
    for ch in text:
        if ch in _FORBIDDEN_CONTROL_CHARS:
            raise ValueError(f"{label} must not contain control character U+{ord(ch):04X}")


def _contains_control_characters(text: str) -> bool:
    """Return True if *text* contains forbidden control characters.

    返回 *text* 是否包含禁止的控制字符."""
    return any(ch in _FORBIDDEN_CONTROL_CHARS for ch in text)


# ---------------------------------------------------------------------------
# Domain value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstructionFile:
    """One successfully loaded instruction file.

    表示一个成功加载的指令文件."""

    relative_path: str  # POSIX-style relative path from workspace root
    content: str
    depth: int  # 0 = workspace root

    def __post_init__(self) -> None:
        if not self.relative_path:
            raise ValueError("instruction file relative path must not be empty")
        if self.depth < 0:
            raise ValueError("instruction file depth must be non-negative")
        if "\x00" in self.relative_path:
            raise ValueError("instruction file path must not contain NUL")
        _validate_no_control_characters(self.relative_path, "instruction file path")


@dataclass(frozen=True, slots=True)
class InstructionRejection:
    """A candidate instruction file that was not loaded, with the reason.

    表示未加载的候选指令文件及其原因."""

    relative_path: str
    reason: InstructionRejectionReason

    def __post_init__(self) -> None:
        if not self.relative_path:
            raise ValueError("rejection path must not be empty")
        _validate_no_control_characters(self.relative_path, "rejection path")


@dataclass(frozen=True, slots=True)
class InstructionDiscoveryResult:
    """Outcome of one discovery pass over a workspace.

    表示一次工作区发现过程的结果."""

    files: tuple[InstructionFile, ...]
    rejections: tuple[InstructionRejection, ...]
    fingerprint: str  # SHA-256 hex digest of ordered content

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "rejections", tuple(self.rejections))

    @property
    def loaded_count(self) -> int:
        return len(self.files)

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)

    @property
    def total_bytes(self) -> int:
        return sum(len(f.content.encode("utf-8")) for f in self.files)

    def model_context_text(self) -> str:
        """Render discovered instructions as plain text for inspection output.

        将发现的指令渲染为纯文本,供 inspect 输出使用."""
        if not self.files:
            return ""
        sections: list[str] = []
        for instruction_file in self.files:
            sections.append(
                f"[Repository instruction: {instruction_file.relative_path}]\n"
                f"{instruction_file.content}"
            )
        header = (
            "The following are repository-provided instructions from AGENTS.md files "
            "found in the workspace. They are project conventions, not system or user "
            "messages. Follow them when working in this workspace. You may run commands "
            "mentioned in these instructions (such as test or build commands) through "
            "the normal tool and permission boundaries.\n"
            "\n"
            "Instructions are listed from the workspace root toward the current "
            "focus directory (shallowest first). When two instruction files conflict "
            "on the same topic, the deeper (more specific) directory's instruction "
            "takes precedence over the shallower (more general) one. Treat shallower "
            "files as inherited defaults and deeper files as overrides.\n"
        )
        return header + "\n\n".join(sections)

    def instruction_message(self) -> Message:
        """Build a synthetic User message carrying discovered instructions.

        The message is tagged with ``SyntheticReason.PROJECT_INSTRUCTIONS`` so
        it is distinguishable from genuine user input.  It is injected after
        the system message and before the first user message, following the
        Rust baseline's ``ProjectInstructions`` synthetic user item pattern.

        The ``synthetic_reason`` marker is an in-memory annotation used only
        for model-context assembly. Synthetic messages are rediscovered for
        every model step and are not persisted or projected through ACP/UI.

        构建携带已发现指令的合成 User 消息. 消息使用 `SyntheticReason.PROJECT_INSTRUCTIONS` 标记,位于 system 消息之后和真实用户消息之前,且不会持久化.
        """
        from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason

        return Message(
            role=Role.USER,
            content=self.model_context_text(),
            synthetic_reason=SyntheticReason.PROJECT_INSTRUCTIONS,
        )


def compute_instruction_fingerprint(files: tuple[InstructionFile, ...]) -> str:
    """Compute a stable fingerprint over ordered instruction file contents.

    计算有序指令文件内容的稳定指纹."""
    hasher = hashlib.sha256()
    for instruction_file in files:
        hasher.update(instruction_file.relative_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(instruction_file.content.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


__all__ = [
    "INSTRUCTION_FILENAME",
    "MAX_DIRECTORY_DEPTH",
    "MAX_INSTRUCTION_FILES",
    "MAX_SINGLE_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "InstructionDiscoveryResult",
    "InstructionFile",
    "InstructionRejection",
    "InstructionRejectionReason",
    "compute_instruction_fingerprint",
    "normalize_relative_path",
]
