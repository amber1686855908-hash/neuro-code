"""Canonical capability boundary for child subagent construction.

This module owns the capability *grant* used by subagent composition.  It is
deliberately independent from permission decisions, workspace path policy, and
the operating-system sandbox implementation: those layers still enforce their
own contracts after this boundary has selected the maximum child capability.

该模块定义子代理构造使用的规范 capability grant.它不取代 Permission、Workspace
或操作系统 Sandbox 层;这些层仍在 child construction 之后执行各自的约束.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.writable_subagent import ManagedChildWorkspaceGrant
from neuro_code.shared.errors import ConfigurationError

MAX_SUBAGENT_STEPS = 12
MAX_SUBAGENT_CAPABILITY_STEPS = 96
MAX_SUBAGENT_CAPABILITY_TOOLS = 256
MAX_SUBAGENT_CAPABILITY_ROOTS = 32
MAX_SUBAGENT_CAPABILITY_MCP_SERVERS = 32

_READ_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_files",
        "list_dir",
        "list_tree",
        "glob",
        "grep",
        "grep_many",
        "workspace_diff",
        "skill",
        "lsp",
    }
)
_WRITE_TOOL_NAMES = frozenset({"search_replace", "apply_patch"})
_BASH_TOOL_NAMES = frozenset({"bash"})
_TERMINAL_TOOL_NAMES = frozenset(
    {
        "terminal_exec",
        "create_terminal",
        "terminal_output",
        "terminal_wait",
        "terminal_kill",
        "terminal_start",
    }
)
_BACKGROUND_TOOL_NAMES = frozenset({"bash", "task_output", "wait_tasks", "kill_task"})
_NETWORK_TOOL_NAMES = frozenset(
    {
        "web_fetch",
        "web_search",
        "google_search",
        "url_context",
        "x_search",
        "code_interpreter",
    }
)

WRITABLE_SUBAGENT_READ_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_files",
        "list_dir",
        "list_tree",
        "glob",
        "grep",
        "grep_many",
        "skill",
    }
)
WRITABLE_SUBAGENT_WRITE_TOOL_NAMES = frozenset({"search_replace", "apply_patch"})
WRITABLE_SUBAGENT_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "bash",
        "terminal_exec",
        "create_terminal",
        "terminal_output",
        "terminal_wait",
        "terminal_kill",
        "terminal_start",
        "task_output",
        "wait_tasks",
        "kill_task",
        "web_fetch",
        "web_search",
        "google_search",
        "url_context",
        "x_search",
        "code_interpreter",
        "subagent",
        "lsp",
    }
)


class NetworkAccess(StrEnum):
    """Network authority exposed to child action tools.

    ``NONE`` means the child has no network-capable action tool. ``ISOLATED``
    describes a network-capable local child whose OS process request is
    isolated. ``INHERIT`` is the broadest action-plane network authority.
    Provider transport needed to invoke a model is not represented here.
    """

    NONE = "none"
    ISOLATED = "isolated"
    INHERIT = "inherit"


_NETWORK_STRENGTH = {
    NetworkAccess.NONE: 0,
    NetworkAccess.ISOLATED: 1,
    NetworkAccess.INHERIT: 2,
}


def _canonical_path(value: Path, *, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a pathlib.Path")
    try:
        result = value.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"{field_name} cannot be resolved") from error
    if not result.is_absolute():
        raise ConfigurationError(f"{field_name} must be absolute")
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_names(
    values: Collection[str],
    *,
    field_name: str,
    limit: int,
) -> frozenset[str]:
    normalized = frozenset(values)
    if len(normalized) > limit:
        raise ConfigurationError(f"{field_name} exceeds its bounded size")
    if any(
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 256
        for value in normalized
    ):
        raise ConfigurationError(f"{field_name} contains an invalid name")
    return normalized


def _sandbox_satisfies(parent: SandboxProfile, child: SandboxProfile) -> bool:
    """Return whether ``child`` is no weaker than ``parent``.

    Sandbox profiles are not ordered by enum spelling.  The comparison uses
    the actual security axes exposed by the domain model: enabled state,
    workspace write authority, and child-network restriction.
    """

    if not isinstance(parent, SandboxProfile) or not isinstance(child, SandboxProfile):
        raise TypeError("sandbox profiles must be canonical")
    if parent.enabled and not child.enabled:
        return False
    if not parent.workspace_writable and child.workspace_writable:
        return False
    return not (parent.restricts_child_network and not child.restricts_child_network)


def _intersect_roots(left: tuple[Path, ...], right: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for left_root in left:
        for right_root in right:
            if _is_within(left_root, right_root):
                candidate = left_root
            elif _is_within(right_root, left_root):
                candidate = right_root
            else:
                continue
            if candidate not in roots:
                roots.append(candidate)
    if not roots:
        raise ConfigurationError("subagent capability workspace intersection is empty")
    return tuple(roots)


def _intersect_sandbox(left: SandboxProfile, right: SandboxProfile) -> SandboxProfile:
    if _sandbox_satisfies(left, right):
        return right
    if _sandbox_satisfies(right, left):
        return left
    raise ConfigurationError("subagent capability sandbox profiles are incomparable")


def _infer_network_access(
    tool_names: frozenset[str],
    mcp_tool_names: frozenset[str],
    sandbox_profile: SandboxProfile,
) -> NetworkAccess:
    if tool_names.intersection(_NETWORK_TOOL_NAMES) or mcp_tool_names:
        # Hosted/remote tool routes are not governed by the local process
        # sandbox.  Keep them explicit as inherited action-plane network.
        return NetworkAccess.INHERIT
    if tool_names.intersection(_BASH_TOOL_NAMES | _TERMINAL_TOOL_NAMES):
        return (
            NetworkAccess.ISOLATED
            if sandbox_profile.restricts_child_network
            else NetworkAccess.INHERIT
        )
    return NetworkAccess.NONE


@dataclass(frozen=True, slots=True)
class SubagentCapabilitySet:
    """One immutable, explicit capability grant or child request.

    The same typed value is used for a parent grant, a requested child grant,
    and the resolved effective grant.  This avoids parallel ``allow_writes``
    or runtime self-report authorities.  A request that is not a subset of
    its parent or global policy is rejected instead of silently clipped.
    """

    allowed_tool_names: frozenset[str]
    filesystem_read: bool
    filesystem_write: bool
    bash: bool
    terminal: bool
    background_tasks: bool
    mcp_tool_names: frozenset[str]
    mcp_server_names: frozenset[str]
    network_access: NetworkAccess
    cwd: Path
    workspace_roots: tuple[Path, ...]
    sandbox_profile: SandboxProfile
    max_steps: int

    def __post_init__(self) -> None:
        tools = _validate_names(
            self.allowed_tool_names,
            field_name="subagent capability tools",
            limit=MAX_SUBAGENT_CAPABILITY_TOOLS,
        )
        mcp_tools = _validate_names(
            self.mcp_tool_names,
            field_name="subagent capability MCP tools",
            limit=MAX_SUBAGENT_CAPABILITY_TOOLS,
        )
        mcp_servers = _validate_names(
            self.mcp_server_names,
            field_name="subagent capability MCP servers",
            limit=MAX_SUBAGENT_CAPABILITY_MCP_SERVERS,
        )
        object.__setattr__(self, "allowed_tool_names", tools)
        object.__setattr__(self, "mcp_tool_names", mcp_tools)
        object.__setattr__(self, "mcp_server_names", mcp_servers)

        for field_name in (
            "filesystem_read",
            "filesystem_write",
            "bash",
            "terminal",
            "background_tasks",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"subagent capability {field_name} must be a bool")
        if self.filesystem_read != bool(tools.intersection(_READ_TOOL_NAMES)):
            raise ConfigurationError("subagent read capability does not match its tools")
        if self.filesystem_write != bool(tools.intersection(_WRITE_TOOL_NAMES)):
            raise ConfigurationError("subagent write capability does not match its tools")
        if self.bash != bool(tools.intersection(_BASH_TOOL_NAMES)):
            raise ConfigurationError("subagent bash capability does not match its tools")
        if self.terminal != bool(tools.intersection(_TERMINAL_TOOL_NAMES)):
            raise ConfigurationError("subagent terminal capability does not match its tools")
        if self.background_tasks and not tools.intersection(_BACKGROUND_TOOL_NAMES):
            raise ConfigurationError("subagent background capability has no background tool")
        if not mcp_tools.issubset(tools):
            raise ConfigurationError("subagent MCP tools must be in the allowed tool set")
        if mcp_servers and not mcp_tools:
            raise ConfigurationError("subagent MCP servers require MCP tools")
        if not isinstance(self.network_access, NetworkAccess):
            raise TypeError("subagent capability network access must be canonical")
        if self.network_access is NetworkAccess.NONE and (
            tools.intersection(_NETWORK_TOOL_NAMES) or mcp_tools
        ):
            raise ConfigurationError("network-capable subagent tools require network access")

        canonical_cwd = _canonical_path(self.cwd, field_name="subagent capability cwd")
        roots = tuple(self.workspace_roots)
        if not roots or len(roots) > MAX_SUBAGENT_CAPABILITY_ROOTS:
            raise ConfigurationError("subagent capability workspace roots are invalid")
        canonical_roots: list[Path] = []
        for root in roots:
            canonical_root = _canonical_path(root, field_name="subagent capability workspace root")
            if canonical_root not in canonical_roots:
                canonical_roots.append(canonical_root)
        if canonical_roots[0] != canonical_cwd:
            raise ConfigurationError("subagent capability workspace roots must start with cwd")
        object.__setattr__(self, "cwd", canonical_cwd)
        object.__setattr__(self, "workspace_roots", tuple(canonical_roots))

        if not isinstance(self.sandbox_profile, SandboxProfile):
            raise TypeError("subagent capability sandbox profile must be canonical")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_SUBAGENT_CAPABILITY_STEPS
        ):
            raise ValueError("subagent capability max_steps is out of bounds")

    @classmethod
    def from_runtime(
        cls,
        *,
        tool_names: Collection[str],
        cwd: Path,
        additional_workspace_roots: Collection[Path] = (),
        sandbox_profile: SandboxProfile,
        enable_background_tasks: bool,
        max_steps: int,
        mcp_tool_names: Collection[str] = (),
        mcp_server_names: Collection[str] = (),
        provider_tool_names: Collection[str] = (),
    ) -> SubagentCapabilitySet:
        """Derive a manifest from the concrete binding inputs.

        This is used after the registry/context have been assembled as an
        exact consistency check.  It is not a runtime self-reporting hook.
        """

        if not isinstance(enable_background_tasks, bool):
            raise TypeError("enable_background_tasks must be a bool")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("subagent capability max_steps is out of bounds")
        tools = frozenset((*tool_names, *provider_tool_names))
        mcp_tools = frozenset(mcp_tool_names)
        return cls(
            allowed_tool_names=tools,
            filesystem_read=bool(tools.intersection(_READ_TOOL_NAMES)),
            filesystem_write=bool(tools.intersection(_WRITE_TOOL_NAMES)),
            bash=bool(tools.intersection(_BASH_TOOL_NAMES)),
            terminal=bool(tools.intersection(_TERMINAL_TOOL_NAMES)),
            background_tasks=enable_background_tasks,
            mcp_tool_names=mcp_tools,
            mcp_server_names=frozenset(mcp_server_names),
            network_access=_infer_network_access(tools, mcp_tools, sandbox_profile),
            cwd=cwd,
            workspace_roots=(cwd, *additional_workspace_roots),
            sandbox_profile=sandbox_profile,
            max_steps=max_steps,
        )

    def is_subset_of(self, parent: SubagentCapabilitySet) -> bool:
        """Return whether this grant is no broader than ``parent``."""

        if not isinstance(parent, SubagentCapabilitySet):
            raise TypeError("parent capability must be canonical")
        return (
            self.allowed_tool_names.issubset(parent.allowed_tool_names)
            and (not self.filesystem_read or parent.filesystem_read)
            and (not self.filesystem_write or parent.filesystem_write)
            and (not self.bash or parent.bash)
            and (not self.terminal or parent.terminal)
            and (not self.background_tasks or parent.background_tasks)
            and self.mcp_tool_names.issubset(parent.mcp_tool_names)
            and self.mcp_server_names.issubset(parent.mcp_server_names)
            and _NETWORK_STRENGTH[self.network_access] <= _NETWORK_STRENGTH[parent.network_access]
            and _sandbox_satisfies(parent.sandbox_profile, self.sandbox_profile)
            and all(
                any(_is_within(root, parent_root) for parent_root in parent.workspace_roots)
                for root in self.workspace_roots
            )
            and any(_is_within(self.cwd, parent_root) for parent_root in parent.workspace_roots)
            and self.max_steps <= parent.max_steps
        )

    def __le__(self, parent: SubagentCapabilitySet) -> bool:
        return self.is_subset_of(parent)

    def intersection(self, other: SubagentCapabilitySet) -> SubagentCapabilitySet:
        """Return the safe intersection of two explicit capability grants."""

        if not isinstance(other, SubagentCapabilitySet):
            raise TypeError("capability intersection requires canonical values")
        tools = self.allowed_tool_names.intersection(other.allowed_tool_names)
        mcp_tools = self.mcp_tool_names.intersection(other.mcp_tool_names)
        if any(_is_within(self.cwd, root) for root in other.workspace_roots):
            cwd = self.cwd
        elif any(_is_within(other.cwd, root) for root in self.workspace_roots):
            cwd = other.cwd
        else:
            raise ConfigurationError("subagent capability cwd intersection is empty")
        roots = _intersect_roots(self.workspace_roots, other.workspace_roots)
        return SubagentCapabilitySet(
            allowed_tool_names=frozenset(tools),
            filesystem_read=bool(tools.intersection(_READ_TOOL_NAMES))
            and self.filesystem_read
            and other.filesystem_read,
            filesystem_write=bool(tools.intersection(_WRITE_TOOL_NAMES))
            and self.filesystem_write
            and other.filesystem_write,
            bash="bash" in tools and self.bash and other.bash,
            terminal=bool(tools.intersection(_TERMINAL_TOOL_NAMES))
            and self.terminal
            and other.terminal,
            background_tasks=bool(tools.intersection(_BACKGROUND_TOOL_NAMES))
            and self.background_tasks
            and other.background_tasks,
            mcp_tool_names=mcp_tools,
            mcp_server_names=self.mcp_server_names.intersection(other.mcp_server_names),
            network_access=min(
                (self.network_access, other.network_access),
                key=_NETWORK_STRENGTH.__getitem__,
            ),
            cwd=cwd,
            workspace_roots=roots,
            sandbox_profile=_intersect_sandbox(self.sandbox_profile, other.sandbox_profile),
            max_steps=min(self.max_steps, other.max_steps),
        )

    @classmethod
    def resolve_child(
        cls,
        *,
        parent: SubagentCapabilitySet,
        requested: SubagentCapabilitySet,
        global_policy: SubagentCapabilitySet,
    ) -> SubagentCapabilitySet:
        """Resolve one child request without implicit permission clipping."""

        if not isinstance(parent, cls):
            raise TypeError("parent capability must be canonical")
        if not isinstance(requested, cls):
            raise TypeError("requested capability must be canonical")
        if not isinstance(global_policy, cls):
            raise ConfigurationError("global capability policy metadata is missing")
        if not requested.is_subset_of(parent):
            raise ConfigurationError("requested subagent capability exceeds parent capability")
        if not requested.is_subset_of(global_policy):
            raise ConfigurationError("requested subagent capability exceeds global policy")
        return parent.intersection(requested).intersection(global_policy)

    @property
    def fingerprint(self) -> str:
        """Return a stable, non-secret identity for this exact manifest."""

        payload = {
            "allowed_tool_names": sorted(self.allowed_tool_names),
            "filesystem_read": self.filesystem_read,
            "filesystem_write": self.filesystem_write,
            "bash": self.bash,
            "terminal": self.terminal,
            "background_tasks": self.background_tasks,
            "mcp_tool_names": sorted(self.mcp_tool_names),
            "mcp_server_names": sorted(self.mcp_server_names),
            "network_access": self.network_access.value,
            "cwd": str(self.cwd),
            "workspace_roots": sorted(str(root) for root in self.workspace_roots),
            "sandbox_profile": self.sandbox_profile.value,
            "max_steps": self.max_steps,
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _non_workspace_axes_subset(
    child: SubagentCapabilitySet,
    parent: SubagentCapabilitySet,
) -> bool:
    """Compare every generic capability axis except filesystem roots.

    This deliberately does not change ``SubagentCapabilitySet.is_subset_of``.
    It is the narrow exception used only after a typed managed-worktree grant
    has replaced the ordinary inherited workspace-root relation.
    """

    return (
        child.allowed_tool_names.issubset(parent.allowed_tool_names)
        and (not child.filesystem_read or parent.filesystem_read)
        and (not child.filesystem_write or parent.filesystem_write)
        and (not child.bash or parent.bash)
        and (not child.terminal or parent.terminal)
        and (not child.background_tasks or parent.background_tasks)
        and child.mcp_tool_names.issubset(parent.mcp_tool_names)
        and child.mcp_server_names.issubset(parent.mcp_server_names)
        and _NETWORK_STRENGTH[child.network_access] <= _NETWORK_STRENGTH[parent.network_access]
        and _sandbox_satisfies(parent.sandbox_profile, child.sandbox_profile)
        and child.max_steps <= parent.max_steps
    )


@dataclass(frozen=True, slots=True)
class WritableSubagentCapabilityGrant:
    """Effective child capability paired with its derived workspace authority."""

    capabilities: SubagentCapabilitySet
    workspace_grant: ManagedChildWorkspaceGrant
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, SubagentCapabilitySet):
            raise TypeError("writable child capabilities must be canonical")
        if not isinstance(self.workspace_grant, ManagedChildWorkspaceGrant):
            raise TypeError("writable child workspace grant must be canonical")
        expected = hashlib.sha256(
            f"{self.capabilities.fingerprint}:{self.workspace_grant.fingerprint}".encode()
        ).hexdigest()
        if self.fingerprint != expected:
            raise ConfigurationError("writable child capability fingerprint is inconsistent")
        if self.capabilities.cwd != self.workspace_grant.canonical_child_root:
            raise ConfigurationError("writable child cwd is outside its derived workspace grant")
        if self.capabilities.workspace_roots != (self.workspace_grant.canonical_child_root,):
            raise ConfigurationError("writable child capability carries additional workspace roots")
        if not self.capabilities.filesystem_write:
            raise ConfigurationError("writable child capability has no write authority")
        if self.capabilities.bash or self.capabilities.terminal:
            raise ConfigurationError("writable child capability includes an unproven process tool")
        if self.capabilities.background_tasks:
            raise ConfigurationError("writable child capability includes background tasks")
        if self.capabilities.network_access is not NetworkAccess.NONE:
            raise ConfigurationError("writable child capability includes network authority")
        if self.capabilities.mcp_tool_names or self.capabilities.mcp_server_names:
            raise ConfigurationError("writable child capability includes MCP authority")


def writable_subagent_request(
    parent: SubagentCapabilitySet,
    *,
    max_steps: int,
) -> SubagentCapabilitySet:
    """Build the fixed, internal writable request without inheriting roots."""

    if not isinstance(parent, SubagentCapabilitySet):
        raise ConfigurationError("parent subagent capability metadata is required")
    read_tools = WRITABLE_SUBAGENT_READ_TOOL_NAMES.intersection(parent.allowed_tool_names)
    return SubagentCapabilitySet.from_runtime(
        tool_names=tuple(sorted((*read_tools, *WRITABLE_SUBAGENT_WRITE_TOOL_NAMES))),
        cwd=parent.cwd,
        sandbox_profile=parent.sandbox_profile,
        enable_background_tasks=False,
        max_steps=min(max_steps, parent.max_steps),
    )


def resolve_writable_subagent_capability(
    *,
    parent: SubagentCapabilitySet,
    requested: SubagentCapabilitySet,
    global_policy: SubagentCapabilitySet,
    workspace_grant: ManagedChildWorkspaceGrant,
) -> WritableSubagentCapabilityGrant:
    """Resolve a writable child only through a typed managed-worktree grant."""

    if not isinstance(parent, SubagentCapabilitySet):
        raise ConfigurationError("parent subagent capability metadata is required")
    if not isinstance(requested, SubagentCapabilitySet):
        raise ConfigurationError("writable subagent capability request is not canonical")
    if not isinstance(global_policy, SubagentCapabilitySet):
        raise ConfigurationError("global subagent capability policy is required")
    if not isinstance(workspace_grant, ManagedChildWorkspaceGrant):
        raise ConfigurationError("managed child workspace grant is required")
    if workspace_grant.parent_capability_fingerprint != parent.fingerprint:
        raise ConfigurationError("managed child workspace grant is bound to another parent")
    if workspace_grant.parent_workspace_root != parent.cwd:
        raise ConfigurationError("managed child workspace grant parent root is inconsistent")
    if any(
        workspace_grant.canonical_child_root == root
        or workspace_grant.canonical_child_root.is_relative_to(root)
        for root in parent.workspace_roots
    ):
        raise ConfigurationError("managed child workspace must be outside parent workspace roots")
    if requested.allowed_tool_names & WRITABLE_SUBAGENT_FORBIDDEN_TOOL_NAMES:
        raise ConfigurationError("writable subagent request contains an out-of-scope tool")
    if requested.mcp_tool_names or requested.mcp_server_names:
        raise ConfigurationError("writable subagent request cannot include MCP tools")
    if requested.network_access is not NetworkAccess.NONE:
        raise ConfigurationError("writable subagent request cannot include network access")
    if not _non_workspace_axes_subset(requested, parent):
        raise ConfigurationError("writable subagent capability exceeds parent capability")
    if not _non_workspace_axes_subset(requested, global_policy):
        raise ConfigurationError("writable subagent capability exceeds global policy")
    if not parent.filesystem_write or not global_policy.filesystem_write:
        raise ConfigurationError("writable subagent requires parent and global write authority")
    if (
        not parent.sandbox_profile.workspace_writable
        or not global_policy.sandbox_profile.workspace_writable
    ):
        raise ConfigurationError("writable subagent requires writable parent and global sandboxes")
    if not WRITABLE_SUBAGENT_WRITE_TOOL_NAMES.issubset(parent.allowed_tool_names):
        raise ConfigurationError("parent binding does not expose all writable child tools")
    if not WRITABLE_SUBAGENT_WRITE_TOOL_NAMES.issubset(global_policy.allowed_tool_names):
        raise ConfigurationError("global policy does not expose all writable child tools")
    if not _sandbox_satisfies(global_policy.sandbox_profile, parent.sandbox_profile):
        raise ConfigurationError("parent sandbox profile exceeds the global child policy")

    allowed = requested.allowed_tool_names.intersection(
        parent.allowed_tool_names,
        global_policy.allowed_tool_names,
    )
    allowed -= WRITABLE_SUBAGENT_FORBIDDEN_TOOL_NAMES
    capabilities = SubagentCapabilitySet.from_runtime(
        tool_names=tuple(sorted(allowed)),
        cwd=workspace_grant.canonical_child_root,
        sandbox_profile=parent.sandbox_profile,
        enable_background_tasks=False,
        max_steps=min(requested.max_steps, parent.max_steps, global_policy.max_steps),
    )
    if not _non_workspace_axes_subset(capabilities, parent):
        raise ConfigurationError("effective writable child exceeds parent capability")
    if not _non_workspace_axes_subset(capabilities, global_policy):
        raise ConfigurationError("effective writable child exceeds global policy")
    if not capabilities.filesystem_write:
        raise ConfigurationError("effective writable child lost write authority")
    fingerprint = hashlib.sha256(
        f"{capabilities.fingerprint}:{workspace_grant.fingerprint}".encode()
    ).hexdigest()
    return WritableSubagentCapabilityGrant(capabilities, workspace_grant, fingerprint)


__all__ = [
    "MAX_SUBAGENT_CAPABILITY_MCP_SERVERS",
    "MAX_SUBAGENT_CAPABILITY_ROOTS",
    "MAX_SUBAGENT_CAPABILITY_STEPS",
    "MAX_SUBAGENT_CAPABILITY_TOOLS",
    "MAX_SUBAGENT_STEPS",
    "WRITABLE_SUBAGENT_FORBIDDEN_TOOL_NAMES",
    "WRITABLE_SUBAGENT_READ_TOOL_NAMES",
    "WRITABLE_SUBAGENT_WRITE_TOOL_NAMES",
    "NetworkAccess",
    "SubagentCapabilitySet",
    "WritableSubagentCapabilityGrant",
    "resolve_writable_subagent_capability",
    "writable_subagent_request",
]
