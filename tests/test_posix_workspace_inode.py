from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from neuro_code.application.ports.sandbox import (
    LocalProcessFilesystemPolicy,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
)
from neuro_code.infrastructure.sandbox.posix_workspace_inode import PosixWorkspaceInodeAudit
from neuro_code.shared.errors import SandboxError

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX workspace inode audit is not available on this platform",
)


def _policy(
    *roots: tuple[Path, LocalWorkspaceAccessMode],
) -> LocalProcessFilesystemPolicy:
    return LocalProcessFilesystemPolicy(
        tuple(LocalWorkspaceAccess(path, mode) for path, mode in roots)
    )


def test_single_link_file_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")

    PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_external_hardlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    private = outside / "private.txt"
    private.write_text("private", encoding="utf-8")
    os.link(private, workspace / "alias.txt")

    with pytest.raises(SandboxError, match="outside the authorized roots"):
        PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_mixed_read_only_and_read_write_alias_is_rejected(tmp_path: Path) -> None:
    read_only = tmp_path / "read-only"
    read_write = tmp_path / "read-write"
    read_only.mkdir()
    read_write.mkdir()
    source = read_only / "source.txt"
    source.write_text("content", encoding="utf-8")
    os.link(source, read_write / "alias.txt")

    with pytest.raises(SandboxError, match="both READ_ONLY and READ_WRITE"):
        PosixWorkspaceInodeAudit.audit(
            _policy(
                (read_only, LocalWorkspaceAccessMode.READ_ONLY),
                (read_write, LocalWorkspaceAccessMode.READ_WRITE),
            )
        )


@pytest.mark.parametrize(
    "mode",
    [LocalWorkspaceAccessMode.READ_ONLY, LocalWorkspaceAccessMode.READ_WRITE],
)
def test_internal_hardlinks_with_one_access_mode_are_allowed(
    tmp_path: Path,
    mode: LocalWorkspaceAccessMode,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = first / "source.txt"
    source.write_text("content", encoding="utf-8")
    os.link(source, second / "alias.txt")

    PosixWorkspaceInodeAudit.audit(_policy((first, mode), (second, mode)))


def test_nested_same_mode_roots_are_deduplicated(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (inner / "file.txt").write_text("content", encoding="utf-8")

    PosixWorkspaceInodeAudit.audit(
        _policy(
            (outer, LocalWorkspaceAccessMode.READ_WRITE),
            (inner, LocalWorkspaceAccessMode.READ_WRITE),
        )
    )


def test_nested_conflicting_mode_roots_fail_closed(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)

    with pytest.raises(SandboxError, match="conflicting access modes"):
        PosixWorkspaceInodeAudit.audit(
            _policy(
                (outer, LocalWorkspaceAccessMode.READ_WRITE),
                (inner, LocalWorkspaceAccessMode.READ_ONLY),
            )
        )


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)

    PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_unicode_and_spaces_are_scanned_without_path_aliasing(tmp_path: Path) -> None:
    workspace = tmp_path / "工作区 with spaces"
    workspace.mkdir()
    (workspace / "文件 name.txt").write_text("content", encoding="utf-8")

    PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_scan_failure_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        mock.patch(
            "neuro_code.infrastructure.sandbox.posix_workspace_inode.os.scandir",
            side_effect=PermissionError("denied"),
        ),
        pytest.raises(SandboxError, match="cannot audit authorized workspace inodes"),
    ):
        PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_stat_failure_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class _BrokenEntry:
        path = str(workspace / "broken")

        @staticmethod
        def stat(*, follow_symlinks: bool) -> os.stat_result:
            del follow_symlinks
            raise PermissionError("denied")

    class _Entries:
        def __enter__(self) -> _Entries:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> object:
            return iter((_BrokenEntry(),))

    with (
        mock.patch(
            "neuro_code.infrastructure.sandbox.posix_workspace_inode.os.scandir",
            return_value=_Entries(),
        ),
        pytest.raises(SandboxError, match="cannot audit authorized workspace inodes"),
    ):
        PosixWorkspaceInodeAudit.audit(_policy((workspace, LocalWorkspaceAccessMode.READ_WRITE)))


def test_successful_fingerprint_is_cached_per_audit_instance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    audit = PosixWorkspaceInodeAudit()
    policy = _policy((workspace, LocalWorkspaceAccessMode.READ_WRITE))

    with mock.patch.object(audit, "_audit_roots", wraps=audit._audit_roots) as scan:
        audit.ensure(policy)
        audit.ensure(policy)

    scan.assert_called_once()
