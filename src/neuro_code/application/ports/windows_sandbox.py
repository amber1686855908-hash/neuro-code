"""Canonical application contracts for Windows sandbox setup authority.

The setup boundary is intentionally separate from local child creation.  It
may require administrator privileges while an ordinary Neuro Code session
continues to use the existing Job Object/ConPTY lifecycle without elevation.

Windows 沙箱 setup authority 的规范应用契约.

setup boundary 与本地 child creation 刻意分离.它可以需要管理员权限,而普通
Neuro Code session 继续通过现有 Job Object/ConPTY lifecycle 运行,不持续提权.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class WindowsSandboxIdentityKind(StrEnum):
    """Dedicated real local users maintained by the installation."""

    OFFLINE = "offline"
    ONLINE = "online"


class WindowsSandboxSetupState(StrEnum):
    """Persisted/setup authority state for one installation."""

    READY = "ready"
    NEEDS_SETUP = "needs-setup"
    NEEDS_REPAIR = "needs-repair"
    UNSUPPORTED = "unsupported"


# Version 3 records two simultaneous real account identities and a static
# installation-scoped Offline Firewall authority.  No mutable "active
# identity" is persisted: child identity selection is a future runtime
# concern, while setup owns the policy that protects the Offline account.
WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION = 3


def _canonical_setup_path(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute pathlib.Path")
    try:
        canonical = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} must be resolvable") from error
    if canonical == canonical.parent:
        raise ValueError(f"{label} must not be a filesystem root")
    return canonical


def _canonical_unique_paths(paths: Sequence[Path], label: str) -> tuple[Path, ...]:
    canonical = tuple(_canonical_setup_path(path, label) for path in paths)
    if len(set(canonical)) != len(canonical):
        raise ValueError(f"{label} must not contain duplicate paths")
    return canonical


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either canonical path contains the other."""

    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


@dataclass(frozen=True, slots=True)
class WindowsSandboxSetupRequest:
    """Explicit filesystem and identity inputs for one setup operation.

    ``sensitive_read_paths`` may be inside an authorized workspace, where a
    deny ACE protects that subtree.  A sensitive path may never be an
    authorized root or an ancestor of one: such a request would make the
    requested workspace impossible to traverse and therefore fails closed.
    """

    installation_root: Path
    read_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    sensitive_read_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        installation_root = _canonical_setup_path(self.installation_root, "installation_root")
        read_roots = _canonical_unique_paths(self.read_roots, "read_roots")
        writable_roots = _canonical_unique_paths(self.writable_roots, "writable_roots")
        sensitive_paths = _canonical_unique_paths(
            self.sensitive_read_paths,
            "sensitive_read_paths",
        )
        if not read_roots:
            raise ValueError("read_roots must contain at least one root")
        if not set(writable_roots).issubset(read_roots):
            raise ValueError("writable_roots must be a subset of read_roots")
        # The installation root is controller/setup state, not a sandbox file
        # authority.  Reject every ancestor/descendant overlap instead of
        # relying on a credential-file deny ACE to compensate for a writable
        # or traversable private-state parent.
        if any(_paths_overlap(installation_root, root) for root in read_roots):
            raise ValueError("installation_root must be disjoint from sandbox read/write roots")
        for sensitive in sensitive_paths:
            if any(root == sensitive or root.is_relative_to(sensitive) for root in read_roots):
                raise ValueError(
                    "a sensitive read path cannot be an authorized root or its ancestor"
                )
        object.__setattr__(self, "installation_root", installation_root)
        object.__setattr__(self, "read_roots", read_roots)
        object.__setattr__(self, "writable_roots", writable_roots)
        object.__setattr__(self, "sensitive_read_paths", sensitive_paths)


@dataclass(frozen=True, slots=True)
class WindowsSandboxPrivilegeBoundary:
    """Facts that keep setup elevation separate from runtime execution."""

    setup_requires_administrator: bool = True
    runtime_requires_administrator: bool = False


@dataclass(frozen=True, slots=True)
class WindowsSandboxSetupSnapshot:
    """Non-secret setup status safe for application/UI diagnostics."""

    state: WindowsSandboxSetupState
    schema_version: int = WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION
    offline_user_sid: str | None = None
    online_user_sid: str | None = None
    write_restricting_sid: str | None = None
    # Compatibility projection retained for callers that used the W2 draft
    # name.  It always contains the synthetic restricting SID, never a user
    # SID and never a firewall subject.
    write_sid: str | None = None
    identities: tuple[WindowsSandboxIdentityKind, ...] = ()
    managed_ace_count: int = 0
    # Compatibility/status projection: when READY this reports that the
    # persistent Offline rule is healthy.  It is not an active identity mode.
    offline_firewall_enabled: bool = False
    privilege_boundary: WindowsSandboxPrivilegeBoundary = WindowsSandboxPrivilegeBoundary()


class WindowsSandboxSetupAuthority(Protocol):
    """Privileged setup/repair/cleanup boundary for the native Windows backend."""

    @property
    def privilege_boundary(self) -> WindowsSandboxPrivilegeBoundary: ...

    def inspect(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot: ...

    def setup(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot: ...

    def repair(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot: ...

    def cleanup(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot: ...
