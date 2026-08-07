"""Canonical filesystem infrastructure for workspace instruction discovery.

Walks from the workspace root toward the target directory, collecting
AGENTS.md files at each level.  All paths are validated against the workspace
boundary; all symlinks and reparse points are rejected.  Encoding, size, depth,
and count limits are enforced fail-closed.

File reads are bounded and best-effort symlink-resistant:

1. ``lstat()`` rejects symlinks and Windows reparse points *before* opening.
2. ``os.open()`` with ``O_NOFOLLOW`` (POSIX) opens the file without following
   symlinks at the last path component, as defence in depth.  On Windows,
   ``O_NOFOLLOW`` is unavailable; the lstat check and the identity comparison
   below provide defence in depth.
3. ``os.fstat()`` on the handle verifies the file is a regular file and
   compares ``st_dev``/``st_ino`` with the lstat result.  If they differ,
   the path was substituted between lstat and open (e.g. the regular file
   was replaced with a symlink), and the read is rejected.
4. ``os.read()`` reads at most ``MAX_SINGLE_FILE_BYTES + 1`` bytes from the
   handle — the ``+1`` detects over-limit files without reading gigabytes
   into memory.
5. A second ``os.fstat()`` after the read verifies the handle's identity
   has not changed, catching fd recycling (rare on most platforms).

Limitations: this is *not* a fully TOCTOU-safe read.  On POSIX, ``O_NOFOLLOW``
only protects the last path component; a parent directory substituted with a
symlink between lstat and open can still redirect the read, though the
lstat-with-fstat identity comparison will usually detect the substitution (the
opened file's inode differs from the lstat file's inode).  On Windows,
``O_NOFOLLOW`` is unavailable, so a substituted symlink at the last component
would be followed by open; the lstat-with-fstat identity comparison catches
this case as well.  Despite these limitations, the combination of lstat
rejection, bounded read, and identity comparison provides strong defence
against the common TOCTOU attack vectors for instruction file discovery.

定义用于工作区指令发现的规范文件系统基础设施. 路径、符号链接、编码、大小、深度和数量限制都失败关闭.
"""

from __future__ import annotations

import os
import stat as stat_module
import sys
from pathlib import Path, PurePosixPath

from neuro_code.domain.workspace.instructions import (
    INSTRUCTION_FILENAME,
    MAX_DIRECTORY_DEPTH,
    MAX_INSTRUCTION_FILES,
    MAX_SINGLE_FILE_BYTES,
    MAX_TOTAL_BYTES,
    InstructionDiscoveryResult,
    InstructionFile,
    InstructionRejection,
    InstructionRejectionReason,
    _contains_control_characters,
    compute_instruction_fingerprint,
)

# Windows file attribute for reparse points (junctions, mount points, etc.).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _relative_posix(path: Path, root: Path) -> str:
    """Return a POSIX-style relative path string from *root* to *path*.

    返回从 *root* 到 *path* 的 POSIX 风格相对路径字符串."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.name
    return PurePosixPath(rel).as_posix()


def _safe_relative_posix(path: Path, root: Path) -> str:
    """Return a display-safe relative path with controls escaped.

    返回已转义控制字符且适合显示的相对路径."""
    relative = _relative_posix(path, root)
    return "".join(
        f"\\u{ord(character):04X}" if _contains_control_characters(character) else character
        for character in relative
    )


def _resolve_within_workspace(candidate: Path, workspace_root: Path) -> Path | None:
    """Resolve *candidate* and return the resolved path if within workspace.

    Returns ``None`` if the path escapes the workspace or cannot be resolved.

    解析 *candidate*,若仍在工作区内则返回解析后的路径,否则返回 ``None``.
    """
    try:
        resolved = candidate.resolve(strict=False)
        root_resolved = workspace_root.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root_resolved)
        return resolved
    except ValueError:
        return None


def _is_symlink_or_reparse_point(lstat_result: os.stat_result) -> bool:
    """Return True if the lstat result indicates a symlink or reparse point.

    On POSIX, ``S_ISLNK`` covers symbolic links.  On Windows, ``S_ISLNK``
    covers NTFS symlinks; reparse points (junctions, mount points) are
    detected via ``st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT``.

    当 lstat 结果表示符号链接或重解析点时返回 True.
    """
    if stat_module.S_ISLNK(lstat_result.st_mode):
        return True
    if sys.platform == "win32":
        attrs = getattr(lstat_result, "st_file_attributes", 0)
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    return False


def _classify_symlink(
    candidate: Path,
    workspace_root: Path,
) -> InstructionRejectionReason:
    """Classify a symlink for the rejection reason.

    All symlinks are rejected, but the reason helps distinguish escape
    (target outside workspace) from circular (target unresolvable) from
    a safe symlink (target within workspace but still not followed).

    根据拒绝原因分类符号链接. 所有符号链接都拒绝,分类用于区分越界、循环和工作区内但仍不可跟随的安全链接.
    """
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return InstructionRejectionReason.CIRCULAR_SYMLINK

    try:
        root_resolved = workspace_root.resolve(strict=False)
        resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError):
        return InstructionRejectionReason.SYMLINK_ESCAPE

    # Symlink is "safe" (target within workspace) but we still reject it
    # because the bounded read uses O_NOFOLLOW and does not follow links.
    return InstructionRejectionReason.SYMLINK_NOT_SUPPORTED


def _toctou_safe_read(
    candidate: Path,
    max_bytes: int,
) -> tuple[bytes, InstructionRejectionReason | None]:
    """Read at most ``max_bytes + 1`` bytes from *candidate* using a safe handle.

    Returns ``(bytes_read, None)`` on success, or ``(b"", reason)`` on failure.

    The function:

    1. ``lstat()`` the path — rejects symlinks and reparse points.
    2. ``os.open()`` with ``O_NOFOLLOW`` (POSIX) or plain open (Windows).
    3. ``os.fstat()`` on the handle — verifies regular file and compares
       ``st_dev``/``st_ino`` with the lstat result to detect path
       substitution between lstat and open.
    4. ``os.read(fd, max_bytes + 1)`` — bounded read; the ``+1`` byte detects
       files that exceed the limit without reading gigabytes into memory.
    5. Post-read ``os.fstat()`` — verifies handle identity unchanged
       (defence in depth against fd recycling).

    通过安全句柄从 *candidate* 最多读取 ``max_bytes + 1`` 字节. 成功时返回读取字节数,失败时返回错误原因,并通过身份比较防止路径替换.
    """
    # Step 1: lstat to detect symlinks and reparse points (no-follow).
    # Save st_dev/st_ino for the identity comparison after open.
    try:
        lst = candidate.lstat()
    except OSError:
        return b"", InstructionRejectionReason.READ_ERROR

    if _is_symlink_or_reparse_point(lst):
        # Don't classify here — the caller classifies for a richer reason.
        return b"", InstructionRejectionReason.SYMLINK_NOT_SUPPORTED

    # Step 2: open with O_NOFOLLOW on POSIX (defence in depth).  On Windows,
    # O_NOFOLLOW is not available; the lstat-with-fstat identity comparison
    # below catches path substitution.
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if sys.platform == "win32":
        flags |= os.O_BINARY

    try:
        fd = os.open(candidate, flags)
    except OSError:
        return b"", InstructionRejectionReason.READ_ERROR

    try:
        # Step 3: fstat the handle for regular-file verification AND
        # identity comparison with the lstat result.  If st_dev/st_ino
        # differ between lstat and fstat, the path was substituted between
        # the lstat and the open (e.g. the regular file was replaced with a
        # symlink, or a parent directory was replaced).  Reject.
        try:
            fst = os.fstat(fd)
        except OSError:
            return b"", InstructionRejectionReason.READ_ERROR

        if not stat_module.S_ISREG(fst.st_mode):
            return b"", InstructionRejectionReason.NOT_A_FILE

        # Identity check: lstat vs fstat.  This is the primary TOCTOU
        # defence — it detects substitution between lstat and open.
        if lst.st_dev != fst.st_dev or lst.st_ino != fst.st_ino:
            return b"", InstructionRejectionReason.READ_ERROR

        # Step 4: bounded read — at most max_bytes + 1 bytes.  A regular-file
        # read can legally return a short chunk, so loop until EOF or the
        # bound instead of assuming one os.read() consumes the whole file.
        try:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            return b"", InstructionRejectionReason.READ_ERROR

        # Step 5: post-read identity verification (defence in depth).
        # On most platforms the fd continues to reference the same file even
        # if the path is deleted or replaced, so this check rarely fires.
        # It is kept as defence in depth against fd recycling edge cases.
        try:
            fst2 = os.fstat(fd)
        except OSError:
            return b"", InstructionRejectionReason.READ_ERROR

        if (
            fst.st_ino != fst2.st_ino
            or fst.st_dev != fst2.st_dev
            or fst.st_size != fst2.st_size
            or getattr(fst, "st_mtime_ns", None) != getattr(fst2, "st_mtime_ns", None)
        ):
            return b"", InstructionRejectionReason.READ_ERROR

        return raw, None
    finally:
        os.close(fd)


class FilesystemInstructionDiscovery:
    """Discover AGENTS.md files by walking the directory tree.

    Discovery is deterministic: files are returned in root-first order (by
    increasing directory depth, then lexicographic path within the same depth).

    遍历目录树发现 AGENTS.md 文件. 结果按根目录优先并保持确定性排序.
    """

    def discover(
        self,
        workspace_root: Path,
        target: Path | None = None,
    ) -> InstructionDiscoveryResult:
        try:
            root = workspace_root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return InstructionDiscoveryResult(
                files=(),
                rejections=(InstructionRejection(".", InstructionRejectionReason.READ_ERROR),),
                fingerprint=compute_instruction_fingerprint(()),
            )

        # Resolve the target; reject if it escapes the workspace.
        try:
            raw_target = (target or workspace_root).expanduser().resolve(strict=False)
            if raw_target.is_file():
                raw_target = raw_target.parent
        except (OSError, RuntimeError):
            return InstructionDiscoveryResult(
                files=(),
                rejections=(InstructionRejection(".", InstructionRejectionReason.READ_ERROR),),
                fingerprint=compute_instruction_fingerprint(()),
            )
        effective_target = _resolve_within_workspace(raw_target, root)
        if effective_target is None:
            return InstructionDiscoveryResult(
                files=(),
                rejections=(
                    InstructionRejection(
                        _safe_relative_posix(raw_target, root) or str(raw_target),
                        InstructionRejectionReason.ESCAPES_WORKSPACE,
                    ),
                ),
                fingerprint=compute_instruction_fingerprint(()),
            )

        # Build the chain of directories from root to target (inclusive).
        directories = self._directory_chain(root, effective_target)

        files: list[InstructionFile] = []
        rejections: list[InstructionRejection] = []
        total_bytes = 0

        # Bound filesystem operations as well as accepted results. Deeper
        # components are not statted one-by-one; one safe audit entry records
        # that the target exceeded the supported inheritance depth.
        if len(directories) > MAX_DIRECTORY_DEPTH + 1:
            rejections.append(
                InstructionRejection(
                    _safe_relative_posix(effective_target / INSTRUCTION_FILENAME, root),
                    InstructionRejectionReason.TOO_DEEP,
                )
            )
            directories = directories[: MAX_DIRECTORY_DEPTH + 1]

        for depth, directory in enumerate(directories):
            candidate = directory / INSTRUCTION_FILENAME

            # lstat once for all early checks (existence, symlink, file type).
            # lstat is no-follow, so symlinks and reparse points are detected
            # here rather than being followed.
            try:
                lst = candidate.lstat()
            except FileNotFoundError:
                # File does not exist at this directory level.
                continue
            except OSError:
                rejections.append(
                    InstructionRejection(
                        _safe_relative_posix(candidate, root),
                        InstructionRejectionReason.READ_ERROR,
                    )
                )
                continue

            rel_path = _relative_posix(candidate, root)
            if _contains_control_characters(rel_path):
                rejections.append(
                    InstructionRejection(
                        _safe_relative_posix(candidate, root),
                        InstructionRejectionReason.CONTROL_CHARACTERS,
                    )
                )
                continue

            # File count check.
            if len(files) >= MAX_INSTRUCTION_FILES:
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.TOO_MANY_FILES)
                )
                continue

            # Symlink / reparse point classification.  This runs BEFORE the
            # workspace boundary check so that an escaped symlink is reported
            # as SYMLINK_ESCAPE (or CIRCULAR_SYMLINK) rather than the less
            # specific ESCAPES_WORKSPACE.
            if _is_symlink_or_reparse_point(lst):
                symlink_reason = _classify_symlink(candidate, root)
                rejections.append(InstructionRejection(rel_path, symlink_reason))
                continue

            # Reject directories and other non-regular files before opening.
            # On Windows, os.open on a directory fails with EACCES; detecting
            # directories via lstat avoids that error path and gives the more
            # informative NOT_A_FILE reason.
            if not stat_module.S_ISREG(lst.st_mode):
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.NOT_A_FILE)
                )
                continue

            # Workspace boundary check on the resolved path (defence in depth
            # alongside the symlink classification above).  This catches
            # escaped middle-path junctions where the final AGENTS.md is a
            # regular file living outside the workspace.
            if _resolve_within_workspace(candidate, root) is None:
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.ESCAPES_WORKSPACE)
                )
                continue

            # Bounded, best-effort symlink-resistant read.  This single call:
            #   - re-lstats to reject any symlink substituted between the
            #     lstat above and the open
            #   - opens with O_NOFOLLOW (POSIX) so the handle cannot follow a
            #     substituted symlink
            #   - fstats the handle to verify it is a regular file (defence
            #     in depth against non-regular substitution)
            #   - reads at most MAX_SINGLE_FILE_BYTES + 1 bytes
            #   - post-read fstats to verify handle identity unchanged
            raw, rejection = _toctou_safe_read(candidate, MAX_SINGLE_FILE_BYTES)

            if rejection is not None:
                # If it's a symlink, classify for a richer rejection reason.
                if rejection is InstructionRejectionReason.SYMLINK_NOT_SUPPORTED:
                    symlink_reason = _classify_symlink(candidate, root)
                    rejections.append(InstructionRejection(rel_path, symlink_reason))
                else:
                    rejections.append(InstructionRejection(rel_path, rejection))
                continue

            actual_size = len(raw)

            # Size check on the actual bytes read.
            if actual_size > MAX_SINGLE_FILE_BYTES:
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.FILE_TOO_LARGE)
                )
                continue

            if total_bytes + actual_size > MAX_TOTAL_BYTES:
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.TOTAL_TOO_LARGE)
                )
                continue

            # Encoding validation.
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.INVALID_ENCODING)
                )
                continue

            # Control character check (C0 excluding \t\n\r, DEL, C1).
            if _contains_control_characters(content):
                rejections.append(
                    InstructionRejection(rel_path, InstructionRejectionReason.CONTROL_CHARACTERS)
                )
                continue

            # Strip BOM if present.
            if content.startswith("\ufeff"):
                content = content[1:]

            files.append(InstructionFile(relative_path=rel_path, content=content, depth=depth))
            total_bytes += actual_size

        fingerprint = compute_instruction_fingerprint(tuple(files))
        return InstructionDiscoveryResult(
            files=tuple(files),
            rejections=tuple(rejections),
            fingerprint=fingerprint,
        )

    @staticmethod
    def _directory_chain(root: Path, target: Path) -> list[Path]:
        """Return directories from *root* to *target* inclusive, root-first.

        按根目录优先返回从 *root* 到 *target* 的全部目录."""
        try:
            rel = target.relative_to(root)
        except ValueError:
            return [root]

        parts = rel.parts
        chain = [root]
        current = root
        for part in parts:
            current = current / part
            chain.append(current)
        return chain


__all__ = ["FilesystemInstructionDiscovery"]
