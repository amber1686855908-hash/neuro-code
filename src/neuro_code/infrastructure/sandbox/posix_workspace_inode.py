"""Fail-closed POSIX workspace inode boundary auditing.

The kernel's hardlink identity is independent of the pathname used by a
child.  Before an enabled local-process sandbox is launched, this module
checks every regular-file inode visible through the authorized workspace roots
and rejects an inode that has an entry outside those roots or spans read-only
and read-write authorization domains.

POSIX 工作区 inode 边界审计.

内核硬链接身份独立于子进程使用的路径.在启动启用的本地进程沙箱之前,本模块
检查授权工作区根目录中可见的每个常规文件 inode,并拒绝存在根目录外目录项或
同时跨越只读与读写授权域的 inode.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from neuro_code.application.ports.sandbox import (
    LocalProcessFilesystemPolicy,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
)
from neuro_code.shared.errors import SandboxError

_MAX_WORKSPACE_AUDIT_ENTRIES = 1_000_000


@dataclass(frozen=True, slots=True)
class _NormalizedRoot:
    path: Path
    mode: LocalWorkspaceAccessMode


@dataclass(slots=True)
class _InodeObservation:
    link_count: int
    authorized_entries: set[Path]
    modes: set[LocalWorkspaceAccessMode]


class PosixWorkspaceInodeAudit:
    """Audit a filesystem policy's authorized roots before child launch.

    The audit is deliberately independent of Git and does not follow symlinks.
    A successful result may be cached by one sandbox instance for the exact
    normalized root/mode fingerprint.  The cache is never shared across
    instances or sessions.

    在子进程启动前审计文件系统策略的授权根目录.

    该审计刻意独立于 Git 且不跟随符号链接.单个沙箱实例可以针对完全相同的
    规范根目录/模式指纹缓存成功结果;缓存不会跨实例或会话共享.
    """

    def __init__(self) -> None:
        self._audited_fingerprints: set[tuple[tuple[str, str], ...]] = set()
        self._lock = RLock()

    def ensure(self, policy: LocalProcessFilesystemPolicy) -> None:
        """Audit ``policy`` once for this sandbox instance.

        Scan failures and accounting inconsistencies raise ``SandboxError``;
        no partial or best-effort result is cached.

        为当前沙箱实例对 ``policy`` 执行一次审计.

        扫描失败与计数不一致会抛出 ``SandboxError``;不会缓存部分结果或尽力而为的结果.
        """

        roots = self._normalize_roots(policy.workspace_roots)
        fingerprint = tuple((str(root.path), root.mode.value) for root in roots)
        with self._lock:
            if fingerprint in self._audited_fingerprints:
                return
            self._audit_roots(roots)
            self._audited_fingerprints.add(fingerprint)

    @classmethod
    def audit(cls, policy: LocalProcessFilesystemPolicy) -> None:
        """Run an uncached audit, primarily for direct infrastructure tests.

        运行一次不缓存的审计,主要供基础设施直接测试使用.
        """

        cls().ensure(policy)

    @classmethod
    def _normalize_roots(
        cls,
        roots: tuple[LocalWorkspaceAccess, ...],
    ) -> tuple[_NormalizedRoot, ...]:
        normalized: list[_NormalizedRoot] = []
        for root in roots:
            try:
                path = root.path.expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise SandboxError(
                    f"cannot resolve authorized workspace root {root.path}: {error}"
                ) from error
            if path == Path("/"):
                raise SandboxError("authorized workspace root must not be the filesystem root")
            try:
                is_directory = path.is_dir()
            except OSError as error:
                raise SandboxError(
                    f"cannot inspect authorized workspace root {path}: {error}"
                ) from error
            if not is_directory:
                raise SandboxError(f"authorized workspace root is not a directory: {path}")
            normalized.append(_NormalizedRoot(path, root.mode))

        # Remove exact same-mode duplicates and reject any overlapping roots
        # whose effective access modes cannot be represented unambiguously.
        deduplicated: list[_NormalizedRoot] = []
        for candidate in sorted(
            normalized, key=lambda item: (len(item.path.parts), str(item.path))
        ):
            overlaps = [
                existing
                for existing in deduplicated
                if cls._paths_overlap(candidate.path, existing.path)
            ]
            if any(existing.mode is not candidate.mode for existing in overlaps):
                raise SandboxError(
                    "authorized workspace roots overlap with conflicting access modes: "
                    f"{candidate.path} ({candidate.mode.value})"
                )
            if overlaps:
                # The shortest same-mode root already covers this subtree.
                continue
            deduplicated.append(candidate)
        return tuple(deduplicated)

    @classmethod
    def _audit_roots(cls, roots: tuple[_NormalizedRoot, ...]) -> None:
        observations: dict[tuple[int, int], _InodeObservation] = {}
        pending = list(roots)
        scanned_entries = 0
        try:
            while pending:
                root = pending.pop()
                with os.scandir(root.path) as entries:
                    for entry in entries:
                        scanned_entries += 1
                        if scanned_entries > _MAX_WORKSPACE_AUDIT_ENTRIES:
                            raise SandboxError(
                                "authorized workspace exceeds the bounded inode audit"
                            )
                        metadata = entry.stat(follow_symlinks=False)
                        mode = metadata.st_mode
                        if stat.S_ISLNK(mode):
                            continue
                        if stat.S_ISDIR(mode):
                            pending.append(_NormalizedRoot(Path(entry.path), root.mode))
                            continue
                        if not stat.S_ISREG(mode):
                            continue
                        inode = (metadata.st_dev, metadata.st_ino)
                        observation = observations.get(inode)
                        if observation is None:
                            observation = _InodeObservation(
                                link_count=metadata.st_nlink,
                                authorized_entries=set(),
                                modes=set(),
                            )
                            observations[inode] = observation
                        elif observation.link_count != metadata.st_nlink:
                            raise SandboxError(
                                "workspace inode metadata changed during hardlink audit: "
                                f"{entry.path}"
                            )
                        observation.authorized_entries.add(Path(entry.path))
                        observation.modes.add(root.mode)
        except SandboxError:
            raise
        except (OSError, RuntimeError) as error:
            raise SandboxError(f"cannot audit authorized workspace inodes: {error}") from error

        mixed: list[str] = []
        external: list[str] = []
        for observation in observations.values():
            if len(observation.modes) > 1:
                mixed.append(str(sorted(observation.authorized_entries)[0]))
            if observation.link_count > len(observation.authorized_entries):
                external.append(str(sorted(observation.authorized_entries)[0]))
        if mixed:
            raise SandboxError(
                "authorized workspace inode appears in both READ_ONLY and READ_WRITE roots: "
                f"{sorted(mixed)[0]}"
            )
        if external:
            raise SandboxError(
                "authorized workspace file has a hardlink outside the authorized roots: "
                f"{sorted(external)[0]}"
            )

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        return first == second or first.is_relative_to(second) or second.is_relative_to(first)


__all__ = ["PosixWorkspaceInodeAudit"]
