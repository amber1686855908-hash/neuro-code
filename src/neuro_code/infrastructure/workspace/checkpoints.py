"""Safe Git-index-aware workspace projection and restoration adapter."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from neuro_code.application.ports.checkpoints import (
    MAX_CHECKPOINT_FILES,
    MAX_CHECKPOINT_SINGLE_FILE_BYTES,
    MAX_CHECKPOINT_TOTAL_BYTES,
    MAX_CHECKPOINT_UNTRACKED_FILES,
    CheckpointFailureKind,
    WorkspaceCheckpointError,
    WorkspaceGitPort,
    WorkspaceStatePort,
)
from neuro_code.application.ports.worktree import GitWorktreePort, WorktreeError
from neuro_code.domain.checkpoints import (
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
)
from neuro_code.domain.worktree import WorktreeHandle
from neuro_code.shared.async_utils import run_blocking


def _unsupported(message: str) -> WorkspaceCheckpointError:
    return WorkspaceCheckpointError(message, kind=CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE)


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _unsupported("Git returned an invalid workspace path")
    if "\\" in value or value.startswith("/"):
        raise _unsupported("workspace paths must be relative POSIX paths")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _unsupported("workspace path contains traversal components")
    if ":" in parts[0]:
        raise _unsupported("workspace path contains a drive prefix")
    return "/".join(parts)


def _safe_target(root: Path, relative: str) -> Path:
    normalized = _safe_relative(relative)
    target = root.joinpath(*normalized.split("/"))
    try:
        if target != root and not target.is_relative_to(root):
            raise _unsupported("workspace path escaped the managed root")
    except (OSError, RuntimeError, ValueError) as error:
        raise _unsupported("workspace path could not be bounded") from error
    return target


def _link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _assert_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceCheckpointError(
            "managed worktree root is unavailable or unsafe",
            kind=CheckpointFailureKind.IDENTITY_MISMATCH,
        )


def _assert_safe_parents(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise _unsupported("workspace target escaped the managed root") from error
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if _link_like(current):
            raise _unsupported("workspace path contains a link-like parent")
        if current.exists() and not current.is_dir():
            raise _unsupported("workspace path contains a non-directory parent")


def _expected_kind(mode: int) -> WorkspaceFileKind:
    if mode == 0o120000:
        return WorkspaceFileKind.SYMLINK
    if mode in {0o100644, 0o100755}:
        return WorkspaceFileKind.REGULAR
    raise _unsupported("workspace index contains an unsupported file mode")


def _working_mode(file_mode: int) -> int:
    return 0o100755 if file_mode & 0o111 else 0o100644


def _read_regular(path: Path, *, expected_size: int | None = None) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise _unsupported("workspace entry is not a regular file")
        if before.st_size > MAX_CHECKPOINT_SINGLE_FILE_BYTES:
            raise WorkspaceCheckpointError(
                "workspace file exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_CHECKPOINT_SINGLE_FILE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        after = path.lstat()
    except WorkspaceCheckpointError:
        raise
    except (OSError, ValueError) as error:
        raise WorkspaceCheckpointError(
            "workspace file could not be read safely",
            kind=CheckpointFailureKind.COMMAND_FAILED,
        ) from error
    if len(content) > MAX_CHECKPOINT_SINGLE_FILE_BYTES:
        raise WorkspaceCheckpointError(
            "workspace file exceeds the bounded size",
            kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
        )
    if expected_size is not None and len(content) != expected_size:
        raise _unsupported("workspace file changed during projection capture")
    if (
        before.st_ino != after.st_ino
        or before.st_dev != after.st_dev
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise WorkspaceCheckpointError(
            "workspace file changed during projection capture",
            kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
        )
    return content


def _nested_repository_marker(root: Path, target: Path) -> bool:
    current = target.parent
    while current != root:
        marker = current / ".git"
        if marker.exists() or marker.is_symlink():
            return True
        current = current.parent
    return False


class LocalWorkspaceStateAdapter(WorkspaceStatePort):
    """Capture and restore the bounded Git/source projection of a handle."""

    def __init__(self, *, git: GitWorktreePort, workspace_git: WorkspaceGitPort) -> None:
        self._git = git
        self._workspace_git = workspace_git

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection:
        if not isinstance(handle, WorktreeHandle):
            raise TypeError("workspace inspection requires a managed worktree handle")
        root = handle.path
        _assert_root(root)
        try:
            status = await self._git.inspect_status(root)
            split_index, sparse_checkout, sparse_index = await self._config_flags(root)
            if split_index or sparse_checkout or sparse_index:
                raise _unsupported("split or sparse Git index state is not supported")
            index_bytes, index_records, untracked_records = await self._read_git_projection(root)
            if self._has_intent_to_add(await self._workspace_git.status_porcelain(root)):
                raise _unsupported("intent-to-add Git index entries are not supported")
            tracked_modes = self._parse_index_records(index_records)
            untracked = self._parse_untracked_records(untracked_records)
            if len(untracked) > MAX_CHECKPOINT_UNTRACKED_FILES:
                raise WorkspaceCheckpointError(
                    "non-ignored untracked file count exceeds the bound",
                    kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                )
            if len(tracked_modes) + len(untracked) > MAX_CHECKPOINT_FILES:
                raise WorkspaceCheckpointError(
                    "workspace file count exceeds the bound",
                    kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                )
            if set(tracked_modes) & set(untracked):
                raise _unsupported("Git returned overlapping tracked and untracked paths")
            entries: list[WorkspaceFileEntry] = []
            total_bytes = len(index_bytes)
            for relative, mode in tracked_modes.items():
                target = _safe_target(root, relative)
                _assert_safe_parents(root, target)
                if _nested_repository_marker(root, target):
                    raise _unsupported("nested repositories are not supported")
                expected_kind = _expected_kind(mode)
                if not target.exists() and not target.is_symlink():
                    entries.append(
                        WorkspaceFileEntry(
                            path=relative,
                            scope=WorkspaceFileScope.TRACKED,
                            present=False,
                            kind=expected_kind,
                            mode=mode,
                        )
                    )
                    continue
                entries.append(
                    await self._capture_present(
                        target,
                        relative,
                        WorkspaceFileScope.TRACKED,
                        expected_kind=expected_kind,
                    )
                )
                captured = entries[-1]
                total_bytes += _entry_size(captured)
                if total_bytes > MAX_CHECKPOINT_TOTAL_BYTES:
                    raise WorkspaceCheckpointError(
                        "workspace source projection exceeds the bounded size",
                        kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                    )
            for relative in untracked:
                target = _safe_target(root, relative)
                _assert_safe_parents(root, target)
                if _nested_repository_marker(root, target):
                    raise _unsupported("nested repositories are not supported")
                entries.append(
                    await self._capture_present(
                        target,
                        relative,
                        WorkspaceFileScope.UNTRACKED,
                        expected_kind=None,
                    )
                )
                captured = entries[-1]
                total_bytes += _entry_size(captured)
                if total_bytes > MAX_CHECKPOINT_TOTAL_BYTES:
                    raise WorkspaceCheckpointError(
                        "workspace source projection exceeds the bounded size",
                        kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                    )
            return WorkspaceProjection(
                head_sha=status.head_sha,
                branch=status.branch,
                detached=status.detached,
                index_bytes=index_bytes,
                entries=tuple(sorted(entries, key=lambda entry: (entry.path, entry.scope.value))),
            )
        except WorkspaceCheckpointError:
            raise
        except WorktreeError as error:
            raise WorkspaceCheckpointError(
                "Git workspace projection could not be inspected",
                kind=CheckpointFailureKind.COMMAND_FAILED,
            ) from error

    async def _config_flags(self, root: Path) -> tuple[bool, bool, bool]:
        return (
            await self._workspace_git.config_bool(root, "core.splitIndex"),
            await self._workspace_git.config_bool(root, "core.sparseCheckout"),
            await self._workspace_git.config_bool(root, "index.sparse"),
        )

    async def _read_git_projection(self, root: Path) -> tuple[bytes, bytes, bytes]:
        return (
            await self._workspace_git.read_index(root),
            await self._workspace_git.index_entries(root),
            await self._workspace_git.nonignored_untracked_paths(root),
        )

    @staticmethod
    def _parse_index_records(raw: bytes) -> dict[str, int]:
        if not raw:
            return {}
        records: dict[str, int] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode_text, object_id, stage_text = os.fsdecode(header).split(" ")
                mode = int(mode_text, 8)
                stage = int(stage_text, 10)
            except (ValueError, UnicodeDecodeError) as error:
                raise _unsupported("Git index entry output is malformed") from error
            if stage != 0:
                raise _unsupported("unmerged Git index stages are not supported")
            if not object_id or set(object_id) == {"0"}:
                raise _unsupported("intent-to-add Git index entries are not supported")
            relative = _safe_relative(os.fsdecode(raw_path))
            if relative in records:
                raise _unsupported("Git index contains duplicate paths")
            if mode == 0o160000:
                raise _unsupported("submodule entries are not supported")
            _expected_kind(mode)
            records[relative] = mode
        return records

    @staticmethod
    def _parse_untracked_records(raw: bytes) -> tuple[str, ...]:
        paths: list[str] = []
        for raw_path in raw.split(b"\0"):
            if not raw_path:
                continue
            relative = _safe_relative(os.fsdecode(raw_path))
            if relative not in paths:
                paths.append(relative)
        return tuple(paths)

    @staticmethod
    def _has_intent_to_add(raw: bytes) -> bool:
        for record in raw.split(b"\0"):
            if not record or record.startswith((b"#", b"?", b"!", b"u")):
                continue
            fields = record.split(b" ", 3)
            if len(fields) >= 2 and fields[0] == b"1" and fields[1] == b".A":
                return True
        return False

    async def _capture_present(
        self,
        target: Path,
        relative: str,
        scope: WorkspaceFileScope,
        *,
        expected_kind: WorkspaceFileKind | None,
    ) -> WorkspaceFileEntry:
        try:
            metadata = await run_blocking(target.lstat)
        except OSError as error:
            raise WorkspaceCheckpointError(
                "workspace entry disappeared during projection capture",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            kind = WorkspaceFileKind.SYMLINK
            if expected_kind is not None and expected_kind is not kind:
                raise _unsupported("tracked file kind does not match the Git index")
            try:
                target_text = await run_blocking(os.readlink, target)
            except OSError as error:
                raise WorkspaceCheckpointError(
                    "workspace symlink could not be read safely",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error
            try:
                after = await run_blocking(target.lstat)
            except OSError as error:
                raise WorkspaceCheckpointError(
                    "workspace symlink changed during projection capture",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                ) from error
            if metadata.st_ino != after.st_ino or metadata.st_dev != after.st_dev:
                raise WorkspaceCheckpointError(
                    "workspace symlink changed during projection capture",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                )
            return WorkspaceFileEntry(
                path=relative,
                scope=scope,
                present=True,
                kind=kind,
                mode=0o120000,
                link_target=target_text,
            )
        if not stat.S_ISREG(metadata.st_mode):
            raise _unsupported("special workspace file types are not supported")
        kind = WorkspaceFileKind.REGULAR
        if expected_kind is not None and expected_kind is not kind:
            raise _unsupported("tracked file kind does not match the Git index")
        content = await run_blocking(_read_regular, target, expected_size=metadata.st_size)
        return WorkspaceFileEntry(
            path=relative,
            scope=scope,
            present=True,
            kind=kind,
            mode=_working_mode(metadata.st_mode),
            content=content,
        )

    async def restore(
        self,
        handle: WorktreeHandle,
        projection: WorkspaceProjection,
        /,
    ) -> None:
        if not isinstance(handle, WorktreeHandle):
            raise TypeError("workspace restoration requires a managed worktree handle")
        if not isinstance(projection, WorkspaceProjection):
            raise TypeError("workspace restoration requires a canonical projection")
        current = await self.inspect(handle)
        if current.head_sha != projection.head_sha:
            raise WorkspaceCheckpointError(
                "managed worktree HEAD moved since checkpoint capture",
                kind=CheckpointFailureKind.HEAD_MISMATCH,
            )
        root = handle.path
        target_entries = {entry.path: entry for entry in projection.entries if entry.present}
        current_entries = {entry.path: entry for entry in current.entries if entry.present}
        for relative in sorted(
            set(current_entries) - set(target_entries),
            key=lambda value: (value.count("/"), value),
            reverse=True,
        ):
            _remove_leaf(root, _safe_target(root, relative))
        for relative in sorted(target_entries, key=lambda value: (value.count("/"), value)):
            entry = target_entries[relative]
            target = _safe_target(root, relative)
            _assert_safe_parents(root, target)
            _ensure_parent_directories(root, target)
            if entry.kind is WorkspaceFileKind.REGULAR:
                assert entry.content is not None
                _write_regular(target, entry.content, entry.mode)
            else:
                assert entry.link_target is not None
                _write_symlink(target, entry.link_target)
        try:
            await self._workspace_git.replace_index(root, projection.index_bytes)
        except WorktreeError as error:
            raise WorkspaceCheckpointError(
                "worktree index could not be restored",
                kind=CheckpointFailureKind.COMMAND_FAILED,
            ) from error


def _entry_size(entry: WorkspaceFileEntry) -> int:
    if not entry.present:
        return 0
    if entry.kind is WorkspaceFileKind.REGULAR:
        assert entry.content is not None
        return len(entry.content)
    assert entry.link_target is not None
    return len(entry.link_target.encode("utf-8", "surrogateescape"))


def _ensure_parent_directories(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if _link_like(current):
            raise _unsupported("workspace restore parent is link-like")
        if current.exists():
            if not current.is_dir():
                raise _unsupported("workspace restore parent is not a directory")
        else:
            try:
                current.mkdir()
            except OSError as error:
                raise WorkspaceCheckpointError(
                    "workspace restore parent could not be created",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error


def _remove_leaf(root: Path, target: Path) -> None:
    _assert_safe_parents(root, target)
    if target == root:
        raise _unsupported("workspace restore cannot remove the managed root")
    try:
        if not target.exists() and not target.is_symlink():
            return
        if target.is_dir() and not target.is_symlink():
            raise _unsupported("workspace restore encountered a directory leaf")
        target.unlink()
    except WorkspaceCheckpointError:
        raise
    except OSError as error:
        raise WorkspaceCheckpointError(
            "workspace restore could not remove an exact leaf",
            kind=CheckpointFailureKind.COMMAND_FAILED,
        ) from error


def _write_regular(target: Path, content: bytes, mode: int) -> None:
    if target.exists() or target.is_symlink():
        _remove_leaf(target.parent, target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".neuro-restore-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755 if mode == 0o100755 else 0o644)
        os.replace(temporary, target)
    except OSError as error:
        raise WorkspaceCheckpointError(
            "workspace restore could not write a regular file",
            kind=CheckpointFailureKind.COMMAND_FAILED,
        ) from error
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _write_symlink(target: Path, link_target: str) -> None:
    if target.exists() or target.is_symlink():
        _remove_leaf(target.parent, target)
    try:
        os.symlink(link_target, target)
    except (NotImplementedError, OSError) as error:
        raise WorkspaceCheckpointError(
            "workspace symlink could not be restored on this platform",
            kind=CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE,
        ) from error


__all__ = ["LocalWorkspaceStateAdapter"]
