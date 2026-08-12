"""Shared security helpers for child-scoped sandbox adapters.

The former controller-wide Bubblewrap launcher and namespace attestation were
removed in PR5.  Child adapters still use these helpers to resolve trusted
platform executables and validate path relationships.

子进程范围沙箱适配器共享的安全辅助函数.

旧的 controller 范围 Bubblewrap 启动器和命名空间 attestation 已在 PR5 删除.
子进程适配器仍使用这些辅助函数解析受信任的平台可执行文件并验证路径关系.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from neuro_code.shared.errors import SandboxError


def _within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _trusted_system_executable(name: str, workspace: Path) -> Path:
    discovered = shutil.which(name)
    if discovered is None:
        raise SandboxError(f"sandbox profile requires the {name!r} system executable")
    path = Path(discovered).resolve()
    try:
        canonical_workspace = workspace.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise SandboxError(f"cannot resolve sandbox workspace {workspace}: {error}") from error
    try:
        details = path.stat()
    except OSError as error:
        raise SandboxError(f"cannot inspect sandbox executable {path}: {error}") from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SandboxError(f"sandbox executable is not runnable: {path}")
    if _within(path, canonical_workspace):
        raise SandboxError(f"refusing workspace-controlled sandbox executable: {path}")
    if os.name != "posix":
        raise SandboxError("trusted system executable validation is only implemented on POSIX")
    writable_by_caller = os.access(path, os.W_OK) or any(
        os.access(parent, os.W_OK) for parent in path.parents
    )
    owned_by_root_process = os.geteuid() == 0 and details.st_uid == 0
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or (
        writable_by_caller and not owned_by_root_process
    ):
        raise SandboxError(f"sandbox executable or its parent path is caller-writable: {path}")
    return path
