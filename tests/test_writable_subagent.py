from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.ports.checkpoints import WorkspaceCheckpointApplication
from neuro_code.application.ports.lsp import LspOperation, LspRequest
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.ports.sandbox import (
    LocalProcessLifecycleCapability,
    LocalProcessPurpose,
    LocalProcessSandbox,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.worktree import WorktreeError, WorktreeFailureKind
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseError
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.subagent_capabilities import (
    NetworkAccess,
    SubagentCapabilitySet,
    WritableSubagentCapabilityGrant,
    resolve_writable_subagent_capability,
    writable_subagent_request,
)
from neuro_code.application.workflows.writable_subagent import (
    MAX_WRITABLE_SUBAGENT_RESULT_BYTES,
    RunWritableSubagentRequest,
    WritableSubagentApplicationService,
    WritableSubagentResultProjection,
    WritableSubagentRuntimeFactory,
    WritableWorktreeApplication,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.configuration.app import AppConfig
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointId,
    CheckpointState,
    WorkspaceCheckpoint,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import Role, ToolCall
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.tools import ToolDefinition
from neuro_code.domain.worktree import (
    WorktreeCreateRequest,
    WorktreeHandle,
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
)
from neuro_code.domain.writable_subagent import (
    ManagedChildWorkspaceGrant,
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.infrastructure.tools.filesystem import ApplyPatchTool, SearchReplaceTool
from neuro_code.shared.errors import (
    ConfigurationError,
    SessionError,
    SubagentTimeoutError,
    ToolError,
)

BASE_SHA = "a" * 40
_LSP_FIXTURE = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"


class _ParentRunner:
    def __init__(self, session_id: str | None) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str | None:
        return self._session_id


def _parent_binding(
    session_id: str | None,
    capabilities: SubagentCapabilitySet | None,
) -> ConversationBinding:
    return ConversationBinding(
        cast(ConversationRunner, _ParentRunner(session_id)),
        cast(ModelProvider, object()),
        capabilities=capabilities,
    )


class _RecordingProcessSandbox:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve(strict=False)
        self._delegate = ProcessTreeLocalProcessSandbox()
        self.requests: list[SandboxedProcessRequest] = []
        self.processes: list[OwnedLocalProcess] = []

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return self._delegate.lifecycle_capability

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        self.requests.append(request)
        process = await self._delegate.spawn(request)
        self.processes.append(process)
        return process


def _run_git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result.stdout


def _make_real_repository(root: Path) -> tuple[Path, str]:
    repository = root / "real-repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository, "config", "user.name", "Neuro Code Tests")
    (repository / "tracked.txt").write_bytes(b"committed\n")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "-qm", "initial")
    return repository, _run_git(repository, "rev-parse", "HEAD").decode().strip()


def _capability(
    cwd: Path,
    *,
    tools: tuple[str, ...] = (
        "read_file",
        "read_files",
        "list_dir",
        "list_tree",
        "glob",
        "grep",
        "grep_many",
        "skill",
        "search_replace",
        "apply_patch",
    ),
    sandbox: SandboxProfile = SandboxProfile.WORKSPACE,
    max_steps: int = 8,
) -> SubagentCapabilitySet:
    return SubagentCapabilitySet.from_runtime(
        tool_names=tools,
        cwd=cwd,
        sandbox_profile=sandbox,
        enable_background_tasks=False,
        max_steps=max_steps,
    )


def _repository(root: Path) -> WorktreeRepositoryIdentity:
    return WorktreeRepositoryIdentity(
        common_dir=root / "common.git",
        source_worktree=root / "parent",
        git_dir=root / "parent" / ".git",
        head_sha=BASE_SHA,
    )


def _grant(root: Path, parent: SubagentCapabilitySet) -> ManagedChildWorkspaceGrant:
    repository = _repository(root)
    worktree_id = WorktreeId("wt-grant")
    child_root = root / "managed" / worktree_id.value
    handle = WorktreeHandle(
        worktree_id=worktree_id,
        repository=repository,
        path=child_root,
        base_commit_sha=BASE_SHA,
        branch="neuro/writable-subagent/wt-grant",
    )
    return ManagedChildWorkspaceGrant(
        grant_id="wsl-grant",
        parent_capability_fingerprint=parent.fingerprint,
        parent_workspace_root=parent.cwd,
        parent_repository=repository,
        base_commit_sha=BASE_SHA,
        worktree=handle,
        managed_worktree_id=worktree_id,
        canonical_child_root=child_root,
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        baseline_checkpoint_id=CheckpointId("cp-baseline"),
    )


def _reconciliation_lease(
    root: Path,
    parent_session_id: str,
    parent: SubagentCapabilitySet,
    *,
    worktree_id_value: str,
    state: WritableSubagentWorkspaceState,
    owner_pid: int | None,
    worktree: WorktreeHandle | None,
    baseline_checkpoint_id: CheckpointId | None,
) -> WritableSubagentWorkspaceLease:
    repository = _repository(root)
    worktree_id = WorktreeId(worktree_id_value)
    child_root = root / "managed-root" / repository.repository_id / worktree_id.value
    now = datetime(2026, 8, 23, tzinfo=UTC)
    return WritableSubagentWorkspaceLease(
        lease_id=f"wsl-{worktree_id.value}",
        parent_session_id=parent_session_id,
        parent_task_id=f"task-{worktree_id.value}",
        worktree_id=worktree_id,
        parent_capability_fingerprint=parent.fingerprint,
        parent_workspace_root=parent.cwd,
        parent_repository=repository,
        base_commit_sha=BASE_SHA,
        canonical_child_root=child_root,
        state=state,
        created_at=now,
        updated_at=now,
        worktree=worktree,
        baseline_checkpoint_id=baseline_checkpoint_id,
        owner_pid=owner_pid,
    )


def _fake_snapshot(handle: WorktreeHandle) -> WorktreeSnapshot:
    handle.path.mkdir(parents=True, exist_ok=True)
    return WorktreeSnapshot(
        worktree_id=handle.worktree_id,
        repository=handle.repository,
        canonical_path=handle.path,
        base_revision=BASE_SHA,
        base_commit_sha=BASE_SHA,
        branch=handle.branch,
        kind=WorktreeKind.MANAGED_BRANCH,
        ownership=WorktreeOwnership.MANAGED,
        state=WorktreeState.READY,
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
        status=WorktreeStatus(
            path=handle.path,
            head_sha=BASE_SHA,
            branch=handle.branch,
            dirty=False,
            changed_file_count=0,
        ),
    )


async def _insert_reconciliation_lease(
    store: SqliteSessionStore,
    lease: WritableSubagentWorkspaceLease,
) -> None:
    initial = replace(lease, state=WritableSubagentWorkspaceState.ALLOCATING, version=0)
    await store.insert_writable_subagent_lease(initial)
    if lease.state is not WritableSubagentWorkspaceState.ALLOCATING:
        await store.compare_and_transition_writable_subagent_lease(
            replace(lease, version=0),
            expected_version=0,
            expected_state=WritableSubagentWorkspaceState.ALLOCATING,
        )


class WritableCapabilityTests(unittest.TestCase):
    def test_resolver_derives_only_the_managed_child_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            global_policy = _capability(root / "global", max_steps=12)
            grant = _grant(root, parent)
            requested = writable_subagent_request(
                parent,
                global_policy=global_policy,
                max_steps=8,
            )

            resolved = resolve_writable_subagent_capability(
                parent=parent,
                requested=requested,
                global_policy=global_policy,
                workspace_grant=grant,
            )

            self.assertEqual(resolved.capabilities.cwd, grant.canonical_child_root)
            self.assertEqual(resolved.capabilities.workspace_roots, (grant.canonical_child_root,))
            self.assertEqual(
                resolved.capabilities.allowed_tool_names,
                requested.allowed_tool_names,
            )
            self.assertTrue(resolved.capabilities.filesystem_write)
            self.assertFalse(resolved.capabilities.bash)
            self.assertFalse(resolved.capabilities.terminal)
            self.assertFalse(resolved.capabilities.background_tasks)
            self.assertIs(resolved.capabilities.network_access, NetworkAccess.NONE)
            self.assertFalse(resolved.capabilities.is_subset_of(parent))
            self.assertEqual(
                resolved.workspace_grant.workspace_binding.primary_root,
                grant.canonical_child_root,
            )
            self.assertEqual(resolved.workspace_grant.workspace_binding.additional_roots, ())

    def test_lsp_requires_parent_global_and_bounded_worker_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_without_lsp = _capability(root / "parent")
            global_without_lsp = _capability(root / "global", max_steps=12)
            parent_with_lsp = _capability(
                root / "parent",
                tools=(*parent_without_lsp.allowed_tool_names, "lsp"),
            )
            global_with_lsp = _capability(
                root / "global",
                tools=(*global_without_lsp.allowed_tool_names, "lsp"),
                max_steps=12,
            )

            both = writable_subagent_request(
                parent_with_lsp,
                global_policy=global_with_lsp,
                max_steps=8,
            )
            self.assertIn("lsp", both.allowed_tool_names)
            resolved = resolve_writable_subagent_capability(
                parent=parent_with_lsp,
                requested=both,
                global_policy=global_with_lsp,
                workspace_grant=_grant(root, parent_with_lsp),
            )
            self.assertIn("lsp", resolved.capabilities.allowed_tool_names)

            parent_denied = writable_subagent_request(
                parent_without_lsp,
                global_policy=global_with_lsp,
                max_steps=8,
            )
            global_denied = writable_subagent_request(
                parent_with_lsp,
                global_policy=global_without_lsp,
                max_steps=8,
            )
            self.assertNotIn("lsp", parent_denied.allowed_tool_names)
            self.assertNotIn("lsp", global_denied.allowed_tool_names)
            resolved_parent_denied = resolve_writable_subagent_capability(
                parent=parent_without_lsp,
                requested=parent_denied,
                global_policy=global_with_lsp,
                workspace_grant=_grant(root, parent_without_lsp),
            )
            resolved_global_denied = resolve_writable_subagent_capability(
                parent=parent_with_lsp,
                requested=global_denied,
                global_policy=global_without_lsp,
                workspace_grant=_grant(root, parent_with_lsp),
            )
            self.assertNotIn("lsp", resolved_parent_denied.capabilities.allowed_tool_names)
            self.assertNotIn("lsp", resolved_global_denied.capabilities.allowed_tool_names)

            forged = SubagentCapabilitySet.from_runtime(
                tool_names=(*parent_without_lsp.allowed_tool_names, "lsp"),
                cwd=parent_without_lsp.cwd,
                sandbox_profile=parent_without_lsp.sandbox_profile,
                enable_background_tasks=False,
                max_steps=8,
            )
            with self.assertRaisesRegex(ConfigurationError, "exceeds parent"):
                resolve_writable_subagent_capability(
                    parent=parent_without_lsp,
                    requested=forged,
                    global_policy=global_with_lsp,
                    workspace_grant=_grant(root, parent_without_lsp),
                )

    def test_resolver_rejects_missing_write_authority_and_forged_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            read_only_parent = _capability(
                root / "read-only-parent",
                tools=("read_file", "grep"),
                sandbox=SandboxProfile.READ_ONLY,
            )
            global_policy = _capability(root / "global", max_steps=12)
            grant = _grant(root, parent)

            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=read_only_parent,
                    requested=writable_subagent_request(
                        read_only_parent,
                        global_policy=global_policy,
                        max_steps=8,
                    ),
                    global_policy=global_policy,
                    workspace_grant=grant,
                )

            forged = replace(grant, parent_capability_fingerprint="b" * 64)
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=writable_subagent_request(
                        parent,
                        global_policy=global_policy,
                        max_steps=8,
                    ),
                    global_policy=global_policy,
                    workspace_grant=forged,
                )

            process_request = SubagentCapabilitySet.from_runtime(
                tool_names=(*parent.allowed_tool_names, "bash"),
                cwd=parent.cwd,
                sandbox_profile=parent.sandbox_profile,
                enable_background_tasks=False,
                max_steps=8,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=process_request,
                    global_policy=global_policy,
                    workspace_grant=grant,
                )

    def test_resolver_rejects_policy_and_authority_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            global_policy = _capability(root / "global", max_steps=12)
            grant = _grant(root, parent)
            requested = writable_subagent_request(
                parent,
                global_policy=global_policy,
                max_steps=8,
            )

            invalid_inputs = (
                {"parent": object()},
                {"requested": object()},
                {"global_policy": object()},
                {"workspace_grant": object()},
            )
            for invalid in invalid_inputs:
                with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                    resolve_writable_subagent_capability(
                        parent=invalid.get("parent", parent),
                        requested=invalid.get("requested", requested),
                        global_policy=invalid.get("global_policy", global_policy),
                        workspace_grant=invalid.get("workspace_grant", grant),
                    )

            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=requested,
                    global_policy=global_policy,
                    workspace_grant=replace(
                        grant,
                        parent_workspace_root=root / "parent" / "nested",
                    ),
                )
            nested_root = root / "parent" / "nested-child"
            nested_handle = replace(grant.worktree, path=nested_root)
            nested_grant = replace(
                grant,
                canonical_child_root=nested_root,
                worktree=nested_handle,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=requested,
                    global_policy=global_policy,
                    workspace_grant=nested_grant,
                )

            mcp_request = SubagentCapabilitySet.from_runtime(
                tool_names=(*requested.allowed_tool_names, "mcp_tool"),
                cwd=parent.cwd,
                sandbox_profile=parent.sandbox_profile,
                enable_background_tasks=False,
                max_steps=8,
                mcp_tool_names=("mcp_tool",),
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=mcp_request,
                    global_policy=global_policy,
                    workspace_grant=grant,
                )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=replace(requested, network_access=NetworkAccess.INHERIT),
                    global_policy=global_policy,
                    workspace_grant=grant,
                )

            parent_without_write_flag = _capability(root / "parent", tools=("read_file",))
            parent_without_write_grant = _grant(root, parent_without_write_flag)
            read_request = SubagentCapabilitySet.from_runtime(
                tool_names=("read_file",),
                cwd=parent.cwd,
                sandbox_profile=parent.sandbox_profile,
                enable_background_tasks=False,
                max_steps=8,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent_without_write_flag,
                    requested=read_request,
                    global_policy=global_policy,
                    workspace_grant=parent_without_write_grant,
                )
            global_without_write_flag = _capability(
                root / "global",
                tools=("read_file",),
                max_steps=12,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=read_request,
                    global_policy=global_without_write_flag,
                    workspace_grant=grant,
                )
            parent_without_patch = _capability(
                root / "parent",
                tools=("read_file", "search_replace"),
            )
            parent_without_patch_grant = _grant(root, parent_without_patch)
            reduced_request = SubagentCapabilitySet.from_runtime(
                tool_names=tuple(sorted(parent_without_patch.allowed_tool_names)),
                cwd=parent_without_patch.cwd,
                sandbox_profile=parent_without_patch.sandbox_profile,
                enable_background_tasks=False,
                max_steps=8,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent_without_patch,
                    requested=reduced_request,
                    global_policy=global_policy,
                    workspace_grant=parent_without_patch_grant,
                )

            global_without_patch = _capability(
                root / "global-without-patch",
                tools=("read_file", "search_replace"),
                max_steps=12,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=reduced_request,
                    global_policy=global_without_patch,
                    workspace_grant=grant,
                )

            readonly_parent = _capability(
                root / "parent",
                sandbox=SandboxProfile.READ_ONLY,
            )
            readonly_grant = _grant(root, readonly_parent)
            readonly_request = writable_subagent_request(
                readonly_parent,
                global_policy=global_policy,
                max_steps=8,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=readonly_parent,
                    requested=readonly_request,
                    global_policy=global_policy,
                    workspace_grant=readonly_grant,
                )
            readonly_global = _capability(
                root / "readonly-global",
                sandbox=SandboxProfile.READ_ONLY,
                max_steps=12,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=readonly_parent,
                    requested=readonly_request,
                    global_policy=readonly_global,
                    workspace_grant=readonly_grant,
                )

            strict_global = _capability(
                root / "strict-global",
                sandbox=SandboxProfile.STRICT,
                max_steps=12,
            )
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=requested,
                    global_policy=strict_global,
                    workspace_grant=grant,
                )

            with self.assertRaises(ConfigurationError):
                writable_subagent_request(  # type: ignore[arg-type]
                    object(),
                    global_policy=global_policy,
                    max_steps=8,
                )

    def test_writable_capability_grant_validates_its_composed_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            global_policy = _capability(root / "global", max_steps=12)
            grant = _grant(root, parent)
            resolved = resolve_writable_subagent_capability(
                parent=parent,
                requested=writable_subagent_request(
                    parent,
                    global_policy=global_policy,
                    max_steps=8,
                ),
                global_policy=global_policy,
                workspace_grant=grant,
            )

            invalid_values = (
                {"capabilities": object()},
                {"workspace_grant": object()},
                {"fingerprint": "bad"},
                {
                    "capabilities": replace(
                        resolved.capabilities,
                        cwd=grant.parent_workspace_root,
                        workspace_roots=(grant.parent_workspace_root,),
                    )
                },
                {
                    "capabilities": replace(
                        resolved.capabilities,
                        workspace_roots=(resolved.capabilities.cwd, parent.cwd),
                    )
                },
                {
                    "capabilities": SubagentCapabilitySet.from_runtime(
                        tool_names=("read_file",),
                        cwd=grant.canonical_child_root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        enable_background_tasks=False,
                        max_steps=8,
                    )
                },
                {
                    "capabilities": SubagentCapabilitySet.from_runtime(
                        tool_names=("search_replace", "bash"),
                        cwd=grant.canonical_child_root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        enable_background_tasks=False,
                        max_steps=8,
                    )
                },
                {
                    "capabilities": SubagentCapabilitySet.from_runtime(
                        tool_names=("search_replace", "task_output"),
                        cwd=grant.canonical_child_root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        enable_background_tasks=True,
                        max_steps=8,
                    )
                },
                {
                    "capabilities": SubagentCapabilitySet.from_runtime(
                        tool_names=("search_replace", "web_search"),
                        cwd=grant.canonical_child_root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        enable_background_tasks=False,
                        max_steps=8,
                    )
                },
            )
            for invalid in invalid_values:
                capabilities = invalid.get("capabilities", resolved.capabilities)
                workspace_grant = invalid.get("workspace_grant", resolved.workspace_grant)
                fingerprint = invalid.get("fingerprint")
                if (
                    fingerprint is None
                    and isinstance(capabilities, SubagentCapabilitySet)
                    and isinstance(workspace_grant, ManagedChildWorkspaceGrant)
                ):
                    fingerprint = hashlib.sha256(
                        f"{capabilities.fingerprint}:{workspace_grant.fingerprint}".encode()
                    ).hexdigest()
                if fingerprint is None:
                    fingerprint = "bad"
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaises((TypeError, ConfigurationError)),
                ):
                    WritableSubagentCapabilityGrant(
                        capabilities,
                        workspace_grant,
                        fingerprint,
                    )

    def test_real_filesystem_write_boundary_rejects_escape_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            child = root / "managed-child"
            child.mkdir()
            outside_file = Path(outside) / "outside.txt"
            outside_file.write_text("private", encoding="utf-8")
            context = ToolContext(child, sandbox_profile=SandboxProfile.WORKSPACE)

            with self.assertRaises(ToolError):
                SearchReplaceTool().prepare_filesystem_targets(
                    {"path": "../outside.txt", "old": "private", "new": "changed"},
                    context,
                )
            with self.assertRaises(ToolError):
                ApplyPatchTool().prepare_filesystem_targets(
                    {
                        "patch": "*** Begin Patch\n*** Add File: ../outside.txt\n+changed\n*** End Patch"
                    },
                    context,
                )
            if hasattr(os, "symlink"):
                link = child / "outside-link.txt"
                try:
                    link.symlink_to(outside_file)
                except OSError as error:
                    self.skipTest(f"cannot create symlink: {error}")
                with self.assertRaises(ToolError):
                    SearchReplaceTool().prepare_filesystem_targets(
                        {"path": "outside-link.txt", "old": "private", "new": "changed"},
                        context,
                    )


class WritableContractValidationTests(unittest.TestCase):
    def test_request_and_result_projections_reject_unbounded_or_noncanonical_values(self) -> None:
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("", "prompt")
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "   ")
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "bad\x00prompt")
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "bad\x01prompt")
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "x" * (16 * 1024 + 1))
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "prompt", max_steps=True)
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "prompt", max_steps=0)
        with self.assertRaises(ValueError):
            RunWritableSubagentRequest("parent", "prompt", max_steps=13)

        valid = {
            "parent_session_id": "parent",
            "parent_task_id": "task",
            "child_session_id": "child",
            "status": SessionTaskStatus.COMPLETED,
            "response": "ok",
            "steps": 0,
            "outcome": None,
            "worktree_id": WorktreeId("wt-projection"),
            "baseline_checkpoint_id": "cp-projection",
            "base_commit_sha": BASE_SHA,
            "capability_fingerprint": "a" * 64,
            "grant_fingerprint": "b" * 64,
            "final_workspace_fingerprint": None,
            "workspace_changed": None,
            "changed_file_count": None,
            "truncated": False,
        }
        self.assertFalse(WritableSubagentResultProjection(**valid).truncated)
        invalid_values = (
            {"status": SessionTaskStatus.RUNNING},
            {"response": "x" * (MAX_WRITABLE_SUBAGENT_RESULT_BYTES + 1)},
            {"steps": True},
            {"steps": -1},
            {"outcome": object()},
            {"worktree_id": "wt-projection"},
            {"base_commit_sha": ""},
            {"capability_fingerprint": "not-a-digest"},
            {"grant_fingerprint": "not-a-digest"},
            {"final_workspace_fingerprint": "not-a-digest"},
            {"workspace_changed": "yes"},
            {"changed_file_count": True},
            {"changed_file_count": -1},
            {"truncated": 1},
        )
        for change in invalid_values:
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                WritableSubagentResultProjection(**(valid | change))

    def test_typed_grant_and_lease_reject_inconsistent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            grant = _grant(root, parent)
            with self.assertRaises(ValueError):
                replace(grant, grant_id="")
            with self.assertRaises(ValueError):
                replace(grant, parent_capability_fingerprint="not-a-digest")
            with self.assertRaises(TypeError):
                replace(grant, parent_repository=object())
            with self.assertRaises(TypeError):
                replace(grant, parent_workspace_root=cast(Path, "not-a-path"))
            with self.assertRaises(ValueError):
                replace(grant, parent_workspace_root=root / "outside")
            with self.assertRaises(ValueError):
                replace(grant, base_commit_sha="not-a-commit")
            with self.assertRaises(TypeError):
                replace(grant, worktree=object())
            with self.assertRaises(TypeError):
                replace(grant, managed_worktree_id=cast(WorktreeId, "not-a-worktree-id"))
            with self.assertRaises(ValueError):
                replace(grant, managed_worktree_id=WorktreeId("different"))
            with self.assertRaises(ValueError):
                replace(grant, base_commit_sha="b" * 40)
            with self.assertRaises(ValueError):
                replace(grant, canonical_child_root=root / "another-child")
            with self.assertRaises(TypeError):
                replace(grant, baseline_checkpoint_id="cp")
            with self.assertRaises(ValueError):
                replace(grant, created_at=datetime.fromisoformat("2026-08-23"))

            now = datetime(2026, 8, 23, tzinfo=UTC)
            lease = WritableSubagentWorkspaceLease(
                lease_id="wsl-validation",
                parent_session_id="parent",
                parent_task_id="task",
                worktree_id=grant.worktree.worktree_id,
                parent_capability_fingerprint=parent.fingerprint,
                parent_workspace_root=parent.cwd,
                parent_repository=grant.parent_repository,
                base_commit_sha=BASE_SHA,
                canonical_child_root=grant.canonical_child_root,
                state=WritableSubagentWorkspaceState.ALLOCATING,
                created_at=now,
                updated_at=now,
            )
            self.assertTrue(lease.state.active)
            self.assertFalse(WritableSubagentWorkspaceState.PRESERVED.active)
            self.assertFalse(lease.grant_ready)
            self.assertIsNone(lease.effective_fingerprint)
            with self.assertRaises(ValueError):
                replace(lease, lease_id="")
            with self.assertRaises(TypeError):
                replace(lease, worktree_id=cast(WorktreeId, "not-a-worktree-id"))
            with self.assertRaises(ValueError):
                replace(lease, parent_capability_fingerprint="bad")
            with self.assertRaises(TypeError):
                replace(lease, parent_repository=object())
            with self.assertRaises(ValueError):
                replace(lease, parent_workspace_root=root / "outside")
            with self.assertRaises(ValueError):
                replace(lease, base_commit_sha="bad")
            with self.assertRaises(ValueError):
                replace(
                    lease,
                    canonical_child_root=root / "other",
                    worktree=grant.worktree,
                )
            with self.assertRaises(TypeError):
                replace(lease, state="allocating")
            with self.assertRaises(ValueError):
                replace(lease, updated_at=datetime(2026, 8, 22, tzinfo=UTC))
            with self.assertRaises(ValueError):
                replace(lease, updated_at=datetime.fromisoformat("2026-08-23"))
            with self.assertRaises(TypeError):
                replace(lease, worktree=cast(WorktreeHandle, object()))
            with self.assertRaises(ValueError):
                replace(
                    lease,
                    worktree_id=WorktreeId("different"),
                    worktree=grant.worktree,
                )
            with self.assertRaises(ValueError):
                replace(lease, base_commit_sha="b" * 40, worktree=grant.worktree)
            with self.assertRaises(TypeError):
                replace(lease, baseline_checkpoint_id=cast(CheckpointId, "not-a-checkpoint"))
            with self.assertRaises(ValueError):
                replace(lease, owner_pid=0)
            with self.assertRaises(TypeError):
                replace(lease, workspace_changed="yes")
            with self.assertRaises(ValueError):
                replace(lease, changed_file_count=-1)
            with self.assertRaises(ValueError):
                replace(lease, version=-1)

            with self.assertRaises(ValueError):
                _ = lease.grant

            ready_lease = replace(
                lease,
                worktree=grant.worktree,
                baseline_checkpoint_id=grant.baseline_checkpoint_id,
            )
            derived_grant = ready_lease.grant
            self.assertEqual(derived_grant.grant_id, lease.lease_id)
            self.assertEqual(derived_grant.worktree, grant.worktree)


class WritableLeaseStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_lease_is_insert_only_and_cas_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            parent_session_id = await store.create_session(str(parent), "fixture", "model")
            repository = _repository(root)
            worktree_id = WorktreeId("wt-lease")
            child_root = root / "managed" / worktree_id.value
            now = datetime(2026, 8, 23, tzinfo=UTC)
            lease = WritableSubagentWorkspaceLease(
                lease_id="wsl-lease",
                parent_session_id=parent_session_id,
                parent_task_id="writable-subagent-lease",
                worktree_id=worktree_id,
                parent_capability_fingerprint="c" * 64,
                parent_workspace_root=parent,
                parent_repository=repository,
                base_commit_sha=BASE_SHA,
                canonical_child_root=child_root,
                state=WritableSubagentWorkspaceState.ALLOCATING,
                created_at=now,
                updated_at=now,
                owner_pid=os.getpid(),
            )
            inserted = await store.insert_writable_subagent_lease(lease)
            self.assertEqual(inserted, lease)
            self.assertEqual(await store.get_writable_subagent_lease(lease.lease_id), lease)

            duplicate_parent = replace(
                lease,
                lease_id="wsl-duplicate",
                parent_task_id="writable-subagent-duplicate",
                worktree_id=WorktreeId("wt-duplicate"),
                canonical_child_root=root / "managed" / "wt-duplicate",
            )
            with self.assertRaises(WritableSubagentLeaseError):
                await store.insert_writable_subagent_lease(duplicate_parent)

            handle = WorktreeHandle(
                worktree_id=worktree_id,
                repository=repository,
                path=child_root,
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-lease",
            )
            ready = replace(
                lease,
                state=WritableSubagentWorkspaceState.WORKTREE_READY,
                worktree=handle,
                updated_at=datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC),
            )
            transitioned = await store.compare_and_transition_writable_subagent_lease(
                ready,
                expected_version=0,
                expected_state=WritableSubagentWorkspaceState.ALLOCATING,
            )
            self.assertEqual(transitioned.version, 1)
            self.assertEqual(
                (await store.get_writable_subagent_lease(lease.lease_id)).state,
                WritableSubagentWorkspaceState.WORKTREE_READY,
            )
            with self.assertRaises(WritableSubagentLeaseError):
                await store.compare_and_transition_writable_subagent_lease(
                    replace(transitioned, state=WritableSubagentWorkspaceState.BASELINE_READY),
                    expected_version=0,
                    expected_state=WritableSubagentWorkspaceState.ALLOCATING,
                )


class _FakeWorktrees:
    def __init__(self, root: Path, repository: WorktreeRepositoryIdentity) -> None:
        self.managed_root = root / "managed-root"
        self.repository = repository
        self.initialized = False
        self.changed = False
        self.snapshot: WorktreeSnapshot | None = None
        self.inspect_error: WorktreeError | None = None

    async def initialize(self) -> None:
        self.initialized = True

    async def repository_identity(self, path: Path, /) -> WorktreeRepositoryIdentity:
        if not self.initialized or path != self.repository.source_worktree:
            raise AssertionError("unexpected repository identity request")
        return self.repository

    def planned_managed_path(
        self,
        repository: WorktreeRepositoryIdentity,
        worktree_id: WorktreeId,
    ) -> Path:
        if repository != self.repository:
            raise AssertionError("unexpected repository")
        return self.managed_root / repository.repository_id / worktree_id.value

    async def create(self, request: WorktreeCreateRequest) -> WorktreeSnapshot:
        if request.worktree_id is None or request.branch is None:
            raise AssertionError("writable branch request is incomplete")
        path = self.planned_managed_path(self.repository, request.worktree_id)
        path.mkdir(parents=True, exist_ok=True)
        status = WorktreeStatus(
            path=path,
            head_sha=BASE_SHA,
            branch=request.branch,
            dirty=False,
            changed_file_count=0,
        )
        self.snapshot = WorktreeSnapshot(
            worktree_id=request.worktree_id,
            repository=self.repository,
            canonical_path=path,
            base_revision=BASE_SHA,
            base_commit_sha=BASE_SHA,
            branch=request.branch,
            kind=WorktreeKind.MANAGED_BRANCH,
            ownership=WorktreeOwnership.MANAGED,
            state=WorktreeState.READY,
            created_at=datetime(2026, 8, 23, tzinfo=UTC),
            created_by_session_id=request.created_by_session_id,
            status=status,
        )
        return self.snapshot

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot:
        if self.inspect_error is not None:
            raise self.inspect_error
        if self.snapshot is None or self.snapshot.worktree_id.value != worktree_id:
            raise AssertionError("unknown fake worktree")
        return self.snapshot

    async def status(self, worktree_id: str, /) -> WorktreeStatus:
        snapshot = await self.inspect(worktree_id)
        return WorktreeStatus(
            path=snapshot.canonical_path,
            head_sha=BASE_SHA,
            branch=snapshot.branch,
            dirty=self.changed,
            changed_file_count=1 if self.changed else 0,
        )


class _FakeCheckpoints:
    def __init__(
        self,
        root: Path,
        worktrees: _FakeWorktrees,
        *,
        checkpoint_state: CheckpointState = CheckpointState.READY,
    ) -> None:
        self.root = root
        self.worktrees = worktrees
        self.checkpoint_state = checkpoint_state
        self.initialized = False
        self.checkpoints: dict[str, WorkspaceCheckpoint] = {}

    async def initialize(self) -> None:
        self.initialized = True

    def _projection(self, handle: WorktreeHandle) -> WorkspaceProjection:
        content = b"after" if self.worktrees.changed else b"before"
        return WorkspaceProjection(
            head_sha=BASE_SHA,
            branch=handle.branch,
            detached=False,
            index_bytes=content,
            entries=(
                WorkspaceFileEntry(
                    path="README.md",
                    scope=WorkspaceFileScope.TRACKED,
                    present=True,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                    content=content,
                ),
            ),
        )

    async def create(self, request: CheckpointCreateRequest) -> WorkspaceCheckpoint:
        if not self.initialized:
            raise AssertionError("checkpoint service was not initialized")
        projection = self._projection(request.worktree)
        checkpoint_id = request.checkpoint_id or CheckpointId.new()
        checkpoint = WorkspaceCheckpoint(
            checkpoint_id=checkpoint_id,
            worktree_id=request.worktree.worktree_id,
            repository=request.worktree.repository,
            canonical_path=request.worktree.path,
            head_sha=BASE_SHA,
            branch=request.worktree.branch,
            detached=False,
            created_at=datetime(2026, 8, 23, tzinfo=UTC),
            source_fingerprint=workspace_projection_fingerprint(request.worktree, projection),
            artifact_path=self.root / "artifacts" / checkpoint_id.value,
            artifact_sha256="d" * 64,
            artifact_bytes=len(projection.index_bytes),
            artifact_file_count=1,
            state=self.checkpoint_state,
        )
        self.checkpoints[checkpoint_id.value] = checkpoint
        return checkpoint

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection:
        return self._projection(handle)

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None:
        return self.checkpoints.get(checkpoint_id.value)


class _FakeRuntime:
    def __init__(
        self,
        session_id: str,
        capabilities_fingerprint: str,
        worktrees: _FakeWorktrees,
        *,
        error: BaseException | None = None,
        block: bool = False,
        fingerprint_override: str | None = None,
        child_session_id: str | None = None,
        result_session_id: str | None = None,
        response: str = "completed",
        close_error: BaseException | None = None,
    ) -> None:
        self.child_session_id = child_session_id or session_id
        self.capability_fingerprint = fingerprint_override or capabilities_fingerprint
        self.worktrees = worktrees
        self.error = error
        self.block = block
        self.result_session_id = result_session_id
        self.response = response
        self.close_error = close_error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        self.worktrees.changed = True
        return AgentRunResult(
            self.result_session_id or self.child_session_id,
            self.response or f"completed: {prompt}",
            (),
            (),
            (),
            1,
        )

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeRuntimeFactory:
    def __init__(
        self,
        store: SqliteSessionStore,
        worktrees: _FakeWorktrees,
        *,
        error: BaseException | None = None,
        block: bool = False,
        runtime_fingerprint: str | None = None,
        runtime_child_session_id: str | None = None,
        result_session_id: str | None = None,
        created_session_id: str | None = None,
        response: str = "completed",
        close_error: BaseException | None = None,
    ) -> None:
        self.store = store
        self.worktrees = worktrees
        self.error = error
        self.block = block
        self.runtime_fingerprint = runtime_fingerprint
        self.runtime_child_session_id = runtime_child_session_id
        self.result_session_id = result_session_id
        self.created_session_id = created_session_id
        self.response = response
        self.close_error = close_error
        self.runtime: _FakeRuntime | None = None

    async def create_session(self, request: RunWritableSubagentRequest, *, capabilities):
        del request
        session_id = await self.store.create_session(
            str(capabilities.capabilities.cwd), "fixture-child", "model"
        )
        return self.created_session_id or session_id

    async def create(
        self,
        request: RunWritableSubagentRequest,
        *,
        parent_task_id: str,
        child_session_id: str,
        capabilities,
    ) -> _FakeRuntime:
        del request, parent_task_id
        self.runtime = _FakeRuntime(
            child_session_id,
            capabilities.fingerprint,
            self.worktrees,
            error=self.error,
            block=self.block,
            fingerprint_override=self.runtime_fingerprint,
            child_session_id=self.runtime_child_session_id,
            result_session_id=self.result_session_id,
            response=self.response,
            close_error=self.close_error,
        )
        return self.runtime


class _ProductionWritableProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "fixture-v1"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(
        self,
        *,
        cwd: Path,
        case_name: str,
        parent: Path,
        state_dir: Path,
        store: SqliteSessionStore,
        checkpoints: WorkspaceCheckpointApplication,
        application: ApplicationComposition,
        trace: list[str],
    ) -> None:
        self.cwd = cwd
        self.case_name = case_name
        self.parent = parent
        self.state_dir = state_dir
        self.store = store
        self.checkpoints = checkpoints
        self.application = application
        self.trace = trace
        self.calls: list[tuple[ToolDefinition, ...]] = []
        self.target_path: Path | None = None
        self.lsp_manager: LanguageServerManager | None = None
        self.lsp_client: object | None = None
        self.lsp_document_paths: tuple[Path, ...] = ()
        self.lsp_process_alive_during_run = False
        self.lsp_hover_payload: dict[str, object] | None = None
        self.lsp_definition_payload: dict[str, object] | None = None
        self.lsp_configuration_error: dict[str, object] | None = None
        self.blocking_started = asyncio.Event()
        self.blocking_release = asyncio.Event()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        self.calls.append(tuple(tools))
        if len(self.calls) == 1:
            leases = await self.store.list_writable_subagent_leases(include_terminal=False)
            active = [
                lease for lease in leases if lease.state is WritableSubagentWorkspaceState.ACTIVE
            ]
            if len(active) != 1 or active[0].baseline_checkpoint_id is None:
                raise AssertionError("write-capable child started before active baseline state")
            checkpoint = await self.checkpoints.get(active[0].baseline_checkpoint_id)
            if checkpoint is None or checkpoint.state is not CheckpointState.READY:
                raise AssertionError("first write tool was requested before READY baseline")
            self.trace.append("baseline_ready_before_first_tool")
            if "search_replace" not in {tool.name for tool in tools}:
                raise AssertionError("production writable child did not receive search_replace")
            if self._uses_lsp():
                names = {tool.name for tool in tools}
                if "lsp" not in names:
                    raise AssertionError("production writable child did not receive lsp")
                model_context = "\n".join(message.content for message in context.messages)
                if "WORKER_COMMITTED_INSTRUCTION" not in model_context:
                    raise AssertionError("worker did not discover child-root instructions")
                if "PARENT_DIRTY_INSTRUCTION" in model_context:
                    raise AssertionError("worker instruction context leaked from dirty parent")
                if "worker-committed-skill" not in model_context:
                    raise AssertionError("worker did not discover child-root skill metadata")
                if "parent-dirty-skill" in model_context:
                    raise AssertionError("worker skill context leaked from dirty parent")
            path = self._prepare_target()
            target = Path(path)
            self.target_path = target if target.is_absolute() else self.cwd / target
            yield ModelToolCall(
                ToolCall(
                    "writable-tool-1",
                    "search_replace",
                    {"path": path, "old": "committed\n", "new": self._replacement_text()},
                )
            )
            yield ModelCompleted("tool_calls")
            return
        if self._uses_lsp() and len(self.calls) == 2:
            managers = [
                manager
                for manager in self.application._lsp_services
                if manager.workspace_root == self.cwd
            ]
            if len(managers) != 1:
                raise AssertionError("worker binding did not own exactly one child LSP manager")
            self.lsp_manager = managers[0]
            if self.lsp_manager.additional_workspace_roots:
                raise AssertionError("worker LSP inherited additional workspace roots")
            if self.case_name == "lsp-config-failure":
                yield ModelToolCall(
                    ToolCall(
                        "writable-lsp-config-failure",
                        "lsp",
                        {
                            "operation": "hover",
                            "path": "tracked.txt",
                            "line": 1,
                            "column": 1,
                            "profile": "missing",
                        },
                    )
                )
                yield ModelCompleted("tool_calls")
                return
            yield ModelToolCall(
                ToolCall(
                    "writable-lsp-hover",
                    "lsp",
                    {"operation": "hover", "path": "tracked.txt", "line": 1, "column": 1},
                )
            )
            yield ModelToolCall(
                ToolCall(
                    "writable-lsp-definition",
                    "lsp",
                    {
                        "operation": "definition",
                        "path": "tracked.txt",
                        "line": 1,
                        "column": 1,
                    },
                )
            )
            yield ModelCompleted("tool_calls")
            return
        if self._uses_lsp():
            tool_results = {
                message.tool_call_id: message.content
                for message in context.messages
                if message.role is Role.TOOL and message.tool_call_id is not None
            }
            if self.case_name == "lsp-config-failure":
                failure = json.loads(tool_results["writable-lsp-config-failure"])
                error = failure.get("error")
                if not isinstance(error, dict) or error.get("kind") != "profile_not_found":
                    raise AssertionError("worker LSP configuration failure was not typed")
                self.lsp_configuration_error = error
                yield ModelTextDelta("scripted writable child observed typed LSP failure")
                yield ModelCompleted("stop")
                return
            hover = json.loads(tool_results["writable-lsp-hover"])
            definition = json.loads(tool_results["writable-lsp-definition"])
            if self._replacement_text().strip() not in hover.get("hover", ""):
                raise AssertionError("worker LSP did not observe post-write child bytes")
            self.lsp_hover_payload = hover
            self.lsp_definition_payload = definition
            if self.lsp_manager is None:
                raise AssertionError("worker LSP manager identity was not captured")
            route = self.lsp_manager._routes.get("fake")
            if route is None or route.client is None:
                raise AssertionError("worker LSP route was not alive during the run")
            self.lsp_client = route.client
            self.lsp_document_paths = tuple(route.documents)
            self.lsp_process_alive_during_run = route.client._process.returncode is None
            if self.case_name == "lsp-provider-failure":
                raise RuntimeError("scripted provider failure after LSP startup")
            if self.case_name in {"lsp-cancel", "lsp-timeout"}:
                self.blocking_started.set()
                await self.blocking_release.wait()
        yield ModelTextDelta("scripted writable child completed")
        yield ModelCompleted("stop")

    def _uses_lsp(self) -> bool:
        return self.case_name in {
            "lsp-success-a",
            "lsp-success-b",
            "lsp-provider-failure",
            "lsp-config-failure",
            "lsp-cancel",
            "lsp-timeout",
        }

    def _replacement_text(self) -> str:
        if self.case_name == "lsp-success-a":
            return "child-edited-a\n"
        if self.case_name == "lsp-success-b":
            return "child-edited-b\n"
        if self.case_name.startswith("lsp-"):
            return f"child-edited-{self.case_name.removeprefix('lsp-')}\n"
        return "child-edited\n"

    def _prepare_target(self) -> str:
        if self.case_name == "success" or self._uses_lsp():
            return "tracked.txt"
        if self.case_name == "relative-parent":
            return os.path.relpath(self.parent / "tracked.txt", self.cwd)
        if self.case_name == "absolute-parent":
            return str(self.parent / "tracked.txt")
        if self.case_name == "sibling-worktree":
            sibling = self.cwd.parent / "sibling-worktree"
            sibling.mkdir(parents=True, exist_ok=True)
            (sibling / "tracked.txt").write_bytes(b"committed\n")
            return str(sibling / "tracked.txt")
        if self.case_name == "symlink-parent":
            link = self.cwd / "parent-link.txt"
            try:
                link.symlink_to(self.parent / "tracked.txt")
            except OSError:
                self.trace.append("symlink_unavailable")
                return str(self.parent / "tracked.txt")
            return "parent-link.txt"
        if self.case_name == "state-dir":
            self.state_dir.mkdir(parents=True, exist_ok=True)
            state_file = self.state_dir / "state-target.txt"
            state_file.write_bytes(b"committed\n")
            return str(state_file)
        raise AssertionError(f"unknown writable provider case: {self.case_name}")


class _ProductionWritableProviderFactory:
    def __init__(
        self,
        *,
        parent: Path,
        state_dir: Path,
        store: SqliteSessionStore | None,
        checkpoints: WorkspaceCheckpointApplication | None,
        case: list[str],
        trace: list[str],
    ) -> None:
        self.parent = parent
        self.state_dir = state_dir
        self.store = store
        self.checkpoints = checkpoints
        self.application: ApplicationComposition | None = None
        self.case = case
        self.trace = trace
        self.providers: list[_ProductionWritableProvider] = []

    def bind_runtime_services(
        self,
        store: SqliteSessionStore,
        checkpoints: WorkspaceCheckpointApplication,
        application: ApplicationComposition,
    ) -> None:
        self.store = store
        self.checkpoints = checkpoints
        self.application = application

    def __call__(self, config: AppConfig, failover: bool) -> ModelProvider:
        del failover
        if self.store is None or self.checkpoints is None or self.application is None:
            raise AssertionError("production provider factory was used before composition binding")
        provider = _ProductionWritableProvider(
            cwd=config.cwd,
            case_name=self.case[0],
            parent=self.parent,
            state_dir=self.state_dir,
            store=self.store,
            checkpoints=self.checkpoints,
            application=self.application,
            trace=self.trace,
        )
        self.providers.append(provider)
        return cast(ModelProvider, provider)


class WritableApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_constructor_rejects_invalid_collaborators_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            store = SqliteSessionStore(root / "sessions.db")
            worktrees = _FakeWorktrees(root, _repository(root))
            checkpoints = _FakeCheckpoints(root, worktrees)
            factory = _FakeRuntimeFactory(store, worktrees)
            parent_binding = _parent_binding("parent", _capability(parent))
            global_policy = _capability(root / "global", max_steps=12)

            with self.assertRaisesRegex(ConfigurationError, "global.*policy"):
                WritableSubagentApplicationService(
                    store,
                    store,
                    worktrees,
                    checkpoints,
                    factory,
                    parent_binding=parent_binding,
                    global_policy=cast(SubagentCapabilitySet, object()),
                )
            with self.assertRaisesRegex(ValueError, "timeout.*bounds"):
                WritableSubagentApplicationService(
                    store,
                    store,
                    worktrees,
                    checkpoints,
                    factory,
                    parent_binding=parent_binding,
                    global_policy=global_policy,
                    timeout_seconds=True,
                )
            with self.assertRaisesRegex(ConfigurationError, "worktree application"):
                WritableSubagentApplicationService(
                    store,
                    store,
                    cast(WritableWorktreeApplication, object()),
                    checkpoints,
                    factory,
                    parent_binding=parent_binding,
                    global_policy=global_policy,
                )
            with self.assertRaisesRegex(ConfigurationError, "runtime factory"):
                WritableSubagentApplicationService(
                    store,
                    store,
                    worktrees,
                    checkpoints,
                    cast(WritableSubagentRuntimeFactory, object()),
                    parent_binding=parent_binding,
                    global_policy=global_policy,
                )

    async def test_parent_binding_validation_is_canonical_at_both_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            global_policy = _capability(root / "global", max_steps=12)
            worktrees = _FakeWorktrees(root, _repository(root))
            checkpoints = _FakeCheckpoints(root, worktrees)
            store = SqliteSessionStore(root / "sessions.db")
            factory = _FakeRuntimeFactory(store, worktrees)
            parent_capabilities = _capability(parent)
            invalid_bindings = (
                (cast(ConversationBinding, object()), "parent binding is required"),
                (_parent_binding("parent", None), "capability metadata is missing"),
                (_parent_binding(None, parent_capabilities), "session identity is missing"),
            )
            for binding, message in invalid_bindings:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ConfigurationError, message):
                        WritableSubagentApplicationService(
                            store,
                            store,
                            worktrees,
                            checkpoints,
                            factory,
                            parent_binding=binding,
                            global_policy=global_policy,
                        )
                    with self.assertRaisesRegex(ConfigurationError, message):
                        ApplicationComposition.create_writable_subagent_service(
                            cast(ApplicationComposition, object()),
                            parent_binding=binding,
                        )

    @unittest.skipUnless(os.name != "nt", "POSIX owner probe")
    def test_posix_owner_probe_is_conservative_on_permission_error(self) -> None:
        with patch(
            "neuro_code.application.runtime.process_liveness.os.kill",
            side_effect=PermissionError,
        ):
            self.assertTrue(owner_is_alive(1234))

    async def _service(
        self,
        root: Path,
        *,
        error: BaseException | None = None,
        block: bool = False,
        checkpoint_state: CheckpointState = CheckpointState.READY,
        runtime_fingerprint: str | None = None,
        runtime_child_session_id: str | None = None,
        result_session_id: str | None = None,
        created_session_id: str | None = None,
        response: str = "completed",
        close_error: BaseException | None = None,
        bound_capabilities: SubagentCapabilitySet | None = None,
    ) -> tuple[
        WritableSubagentApplicationService,
        SqliteSessionStore,
        Path,
        _FakeWorktrees,
        _FakeCheckpoints,
        _FakeRuntimeFactory,
        SubagentCapabilitySet,
        str,
    ]:
        parent = root / "parent"
        parent.mkdir(parents=True)
        (parent / "parent.txt").write_bytes(b"dirty parent state")
        repository = _repository(root)
        worktrees = _FakeWorktrees(root, repository)
        checkpoints = _FakeCheckpoints(root, worktrees, checkpoint_state=checkpoint_state)
        store = SqliteSessionStore(root / "sessions.db")
        await store.initialize()
        parent_session_id = await store.create_session(str(parent), "fixture", "model")
        parent_capabilities = bound_capabilities or _capability(parent)
        factory = _FakeRuntimeFactory(
            store,
            worktrees,
            error=error,
            block=block,
            runtime_fingerprint=runtime_fingerprint,
            runtime_child_session_id=runtime_child_session_id,
            result_session_id=result_session_id,
            created_session_id=created_session_id,
            response=response,
            close_error=close_error,
        )
        service = WritableSubagentApplicationService(
            store,
            store,
            worktrees,
            checkpoints,
            factory,
            parent_binding=_parent_binding(parent_session_id, parent_capabilities),
            global_policy=_capability(root / "global", max_steps=12),
        )
        await service.initialize()
        return (
            service,
            store,
            parent,
            worktrees,
            checkpoints,
            factory,
            parent_capabilities,
            parent_session_id,
        )

    async def test_production_binding_runs_real_write_tool_and_denies_escape_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _initial_head = _make_real_repository(root)
            (repository / "AGENTS.md").write_text(
                "WORKER_COMMITTED_INSTRUCTION\n",
                encoding="utf-8",
            )
            skill_file = repository / ".agents" / "skills" / "worker" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(
                "---\nname: worker\ndescription: worker-committed-skill\n---\n",
                encoding="utf-8",
            )
            with suppress(OSError, NotImplementedError):
                (repository / "outside-link.py").symlink_to(repository / "tracked.txt")
            _run_git(repository, "add", "-A")
            _run_git(repository, "commit", "-qm", "worker integration fixtures")
            head = _run_git(repository, "rev-parse", "HEAD").decode().strip()
            (repository / "AGENTS.md").write_text(
                "PARENT_DIRTY_INSTRUCTION\n",
                encoding="utf-8",
            )
            skill_file.write_text(
                "---\nname: worker\ndescription: parent-dirty-skill\n---\n",
                encoding="utf-8",
            )
            (repository / "parent-dirty.txt").write_bytes(b"parent dirty bytes\n")
            (repository / "untracked.txt").write_bytes(b"untracked parent bytes\n")
            parent_status = _run_git(repository, "status", "--porcelain=v2", "-z")
            parent_index = (repository / ".git" / "index").read_bytes()
            parent_branch = _run_git(repository, "symbolic-ref", "--short", "HEAD")
            parent_tracked = (repository / "tracked.txt").read_bytes()
            state_dir = root / "state"
            state_dir.mkdir()
            state_target = state_dir / "state-target.py"
            state_target.write_bytes(b"state bytes\n")
            lsp_command = ", ".join(
                json.dumps(value)
                for value in (
                    sys.executable,
                    str(_LSP_FIXTURE),
                    "--mode",
                    "worker-integration",
                    "--outside-uri",
                    (repository / "tracked.txt").as_uri(),
                    "--state-uri",
                    state_target.as_uri(),
                )
            )
            (state_dir / "config.toml").write_text(
                f"""
[web_search]
mode = "disabled"

[web_fetch]
mode = "disabled"

[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 131072

[lsp.servers.fake]
language = "python"
command = [{lsp_command}]
extensions = [".py", ".txt"]
""",
                encoding="utf-8",
            )
            case = ["lsp-success-a"]
            trace: list[str] = []
            process_sandboxes: list[_RecordingProcessSandbox] = []

            def process_sandbox_factory(
                profile: SandboxProfile,
                workspace: Path,
                configured_state_dir: Path,
            ) -> LocalProcessSandbox:
                self.assertIs(profile, SandboxProfile.OFF)
                self.assertEqual(configured_state_dir, state_dir)
                sandbox = _RecordingProcessSandbox(workspace)
                process_sandboxes.append(sandbox)
                return cast(LocalProcessSandbox, sandbox)

            provider_factory = _ProductionWritableProviderFactory(
                parent=repository,
                state_dir=state_dir,
                store=None,
                checkpoints=None,
                case=case,
                trace=trace,
            )
            runtime_environment = {
                "PATH": os.environ.get("PATH") or os.defpath,
            }
            if os.name == "nt":
                for name in ("SystemRoot", "SystemDrive", "PATHEXT"):
                    value = os.environ.get(name)
                    if value:
                        runtime_environment[name] = value
            with patch.dict(
                os.environ,
                {
                    **runtime_environment,
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state_dir),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(
                        cwd=repository,
                        sandbox="off",
                        permission_mode=PermissionMode.BYPASS,
                        max_steps=8,
                    ),
                    provider_factory=provider_factory,
                    local_process_sandbox_factory=process_sandbox_factory,
                )
                try:
                    checkpoint_service = application.create_workspace_checkpoint_service()
                    await checkpoint_service.initialize()
                    provider_factory.bind_runtime_services(
                        application.store,
                        checkpoint_service,
                        application,
                    )
                    parent_session_id = await application.store.create_session(
                        str(repository),
                        "fixture",
                        "fixture-model",
                        sandbox_profile=SandboxProfile.OFF,
                    )
                    parent_base_capabilities = _capability(
                        repository,
                        sandbox=SandboxProfile.OFF,
                    )
                    parent_capabilities = _capability(
                        repository,
                        tools=(*parent_base_capabilities.allowed_tool_names, "lsp"),
                        sandbox=SandboxProfile.OFF,
                    )
                    parent_config = await application.config_for_session_resume(parent_session_id)
                    parent_binding = await application.create_binding(
                        config=parent_config,
                        resume_id=parent_session_id,
                        capabilities=parent_capabilities,
                    )
                    parent_lsp = next(
                        manager
                        for manager in application._lsp_services
                        if manager.workspace_root == repository
                    )
                    parent_hover = await parent_lsp.execute(
                        LspRequest(
                            LspOperation.HOVER,
                            path=repository / "tracked.txt",
                            line=1,
                            column=1,
                        )
                    )
                    self.assertIn("committed", parent_hover.payload["hover"])
                    self.assertNotIn("child-edited", parent_hover.payload["hover"])
                    service = application.create_writable_subagent_service(
                        parent_binding=parent_binding,
                    )
                    await service.initialize()
                    result = await service.run_subagent(
                        RunWritableSubagentRequest(parent_session_id, "edit the child file"),
                    )
                    self.assertEqual(result.base_commit_sha, head)
                    self.assertTrue(result.workspace_changed)
                    self.assertIn("baseline_ready_before_first_tool", trace)
                    child_provider = provider_factory.providers[-1]
                    child_base_capabilities = _capability(
                        child_provider.cwd,
                        sandbox=SandboxProfile.OFF,
                    )
                    expected_tool_names = set(
                        _capability(
                            child_provider.cwd,
                            tools=(*child_base_capabilities.allowed_tool_names, "lsp"),
                            sandbox=SandboxProfile.OFF,
                        ).allowed_tool_names
                    )
                    actual_tool_names = {tool.name for tool in child_provider.calls[0]}
                    self.assertEqual(actual_tool_names, expected_tool_names)
                    self.assertIn("lsp", actual_tool_names)
                    self.assertNotIn("bash", actual_tool_names)
                    self.assertNotIn("terminal_exec", actual_tool_names)
                    self.assertFalse(
                        actual_tool_names.intersection(
                            {
                                "web_search",
                                "web_fetch",
                                "mcp",
                                "git",
                                "worktree",
                                "checkpoint",
                                "rollback",
                                "subagent",
                            }
                        )
                    )
                    self.assertEqual(len(child_provider.calls), 3)
                    self.assertIn(
                        "search_replace",
                        {tool.name for tool in child_provider.calls[0]},
                    )
                    worktrees = application.create_worktree_service()
                    await worktrees.initialize()
                    snapshot = await worktrees.inspect(result.worktree_id.value)
                    self.assertEqual(child_provider.cwd, snapshot.canonical_path)
                    self.assertEqual(
                        (snapshot.canonical_path / "tracked.txt").read_bytes(),
                        ("child-edited-a" + os.linesep).encode(),
                    )
                    self.assertTrue(child_provider.lsp_process_alive_during_run)
                    self.assertEqual(
                        child_provider.lsp_document_paths,
                        (snapshot.canonical_path / "tracked.txt",),
                    )
                    self.assertIsNotNone(child_provider.lsp_manager)
                    manager_a = child_provider.lsp_manager
                    assert manager_a is not None
                    self.assertEqual(manager_a.workspace_root, snapshot.canonical_path)
                    self.assertEqual(manager_a.additional_workspace_roots, ())
                    self.assertIsNot(manager_a, parent_lsp)
                    parent_route = parent_lsp._routes["fake"]
                    self.assertIsNot(child_provider.lsp_client, parent_route.client)
                    self.assertEqual(
                        tuple(parent_route.documents),
                        (repository / "tracked.txt",),
                    )
                    self.assertTrue(manager_a._closed)
                    self.assertEqual(manager_a._routes, {})
                    self.assertNotIn(manager_a, application._lsp_services)
                    self.assertIn(parent_lsp, application._lsp_services)
                    definition_a = child_provider.lsp_definition_payload
                    assert definition_a is not None
                    locations_a = cast(list[dict[str, object]], definition_a["locations"])
                    self.assertEqual([item["path"] for item in locations_a], ["tracked.txt"])
                    self.assertEqual(definition_a["omitted_count"], 5)

                    child_sandbox_a = next(
                        sandbox
                        for sandbox in process_sandboxes
                        if sandbox.workspace_root == snapshot.canonical_path
                    )
                    lsp_requests_a = [
                        request
                        for request in child_sandbox_a.requests
                        if request.purpose is LocalProcessPurpose.LSP_SERVER
                    ]
                    self.assertEqual(len(lsp_requests_a), 1)
                    lsp_request_a = lsp_requests_a[0]
                    self.assertEqual(lsp_request_a.cwd, snapshot.canonical_path)
                    self.assertIs(lsp_request_a.sandbox_profile, SandboxProfile.OFF)
                    self.assertFalse(lsp_request_a.uses_shell)
                    self.assertEqual(
                        tuple(
                            root.path for root in lsp_request_a.filesystem_policy.workspace_roots
                        ),
                        (snapshot.canonical_path,),
                    )
                    self.assertTrue(
                        all(process.returncode is not None for process in child_sandbox_a.processes)
                    )
                    lease = (
                        await application.store.list_writable_subagent_leases(
                            parent_session_id=parent_session_id
                        )
                    )[0]
                    self.assertIs(lease.state, WritableSubagentWorkspaceState.PRESERVED)
                    self.assertEqual(lease.canonical_child_root, snapshot.canonical_path)

                    case[0] = "lsp-success-b"
                    trace.clear()
                    result_b = await service.run_subagent(
                        RunWritableSubagentRequest(parent_session_id, "edit worker B"),
                    )
                    provider_b = provider_factory.providers[-1]
                    snapshot_b = await worktrees.inspect(result_b.worktree_id.value)
                    self.assertNotEqual(snapshot_b.canonical_path, snapshot.canonical_path)
                    self.assertEqual(
                        (snapshot_b.canonical_path / "tracked.txt").read_bytes(),
                        ("child-edited-b" + os.linesep).encode(),
                    )
                    self.assertEqual(
                        provider_b.lsp_document_paths,
                        (snapshot_b.canonical_path / "tracked.txt",),
                    )
                    manager_b = provider_b.lsp_manager
                    assert manager_b is not None
                    self.assertIsNot(manager_b, manager_a)
                    self.assertIsNot(provider_b.lsp_client, child_provider.lsp_client)
                    self.assertTrue(manager_b._closed)
                    self.assertEqual(manager_b._routes, {})
                    self.assertTrue(provider_b.lsp_process_alive_during_run)
                    definition_b = provider_b.lsp_definition_payload
                    assert definition_b is not None
                    locations_b = cast(list[dict[str, object]], definition_b["locations"])
                    self.assertEqual([item["path"] for item in locations_b], ["tracked.txt"])
                    self.assertEqual(definition_b["omitted_count"], 5)
                    self.assertEqual(
                        (repository / "tracked.txt").read_bytes(),
                        parent_tracked,
                    )
                    parent_hover_after = await parent_lsp.execute(
                        LspRequest(
                            LspOperation.HOVER,
                            path=repository / "tracked.txt",
                            line=1,
                            column=1,
                        )
                    )
                    self.assertIn("committed", parent_hover_after.payload["hover"])
                    self.assertNotIn("child-edited", parent_hover_after.payload["hover"])
                    parent_sandbox = next(
                        sandbox
                        for sandbox in process_sandboxes
                        if sandbox.workspace_root == repository
                    )
                    self.assertTrue(
                        any(
                            request.purpose is LocalProcessPurpose.LSP_SERVER
                            for request in parent_sandbox.requests
                        )
                    )
                    self.assertTrue(
                        any(process.returncode is None for process in parent_sandbox.processes)
                    )
                    leases = await application.store.list_writable_subagent_leases(
                        parent_session_id=parent_session_id
                    )
                    self.assertTrue(
                        all(
                            item.state is WritableSubagentWorkspaceState.PRESERVED
                            for item in leases[:2]
                        )
                    )

                    case[0] = "lsp-config-failure"
                    config_failure_result = await service.run_subagent(
                        RunWritableSubagentRequest(parent_session_id, "typed LSP config failure"),
                    )
                    config_failure_provider = provider_factory.providers[-1]
                    config_failure_manager = config_failure_provider.lsp_manager
                    assert config_failure_manager is not None
                    self.assertEqual(
                        config_failure_provider.lsp_configuration_error,
                        {
                            "kind": "profile_not_found",
                            "phase": "configuration",
                            "message": "LSP profile is unavailable: missing",
                            "retryable": False,
                        },
                    )
                    self.assertTrue(config_failure_manager._closed)
                    self.assertEqual(config_failure_manager._routes, {})
                    config_failure_snapshot = await worktrees.inspect(
                        config_failure_result.worktree_id.value
                    )
                    self.assertTrue(config_failure_snapshot.canonical_path.is_dir())
                    config_failure_lease = (
                        await application.store.list_writable_subagent_leases(
                            parent_session_id=parent_session_id
                        )
                    )[-1]
                    self.assertIs(
                        config_failure_lease.state,
                        WritableSubagentWorkspaceState.PRESERVED,
                    )

                    case[0] = "lsp-provider-failure"
                    with self.assertRaisesRegex(RuntimeError, "after LSP startup"):
                        await service.run_subagent(
                            RunWritableSubagentRequest(parent_session_id, "fail after LSP"),
                        )
                    failure_provider = provider_factory.providers[-1]
                    failure_manager = failure_provider.lsp_manager
                    assert failure_manager is not None
                    self.assertTrue(failure_provider.lsp_process_alive_during_run)
                    self.assertTrue(failure_manager._closed)
                    self.assertEqual(failure_manager._routes, {})
                    failure_lease = (
                        await application.store.list_writable_subagent_leases(
                            parent_session_id=parent_session_id
                        )
                    )[-1]
                    self.assertIs(
                        failure_lease.state,
                        WritableSubagentWorkspaceState.PRESERVED,
                    )
                    self.assertEqual(failure_lease.error_kind, "RuntimeError")

                    case[0] = "lsp-cancel"
                    cancelling = asyncio.create_task(
                        service.run_subagent(
                            RunWritableSubagentRequest(parent_session_id, "cancel after LSP"),
                        )
                    )
                    for _ in range(3_000):
                        cancel_provider = provider_factory.providers[-1]
                        if cancel_provider.case_name == "lsp-cancel":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("cancellation worker provider was not created")
                    await asyncio.wait_for(cancel_provider.blocking_started.wait(), timeout=20)
                    cancelling.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await cancelling
                    cancel_manager = cancel_provider.lsp_manager
                    assert cancel_manager is not None
                    self.assertTrue(cancel_manager._closed)
                    self.assertEqual(cancel_manager._routes, {})
                    cancel_lease = (
                        await application.store.list_writable_subagent_leases(
                            parent_session_id=parent_session_id
                        )
                    )[-1]
                    self.assertIs(
                        cancel_lease.state,
                        WritableSubagentWorkspaceState.PRESERVED,
                    )

                    case[0] = "lsp-timeout"
                    service._timeout_seconds = 3.0
                    with self.assertRaises(SubagentTimeoutError):
                        await service.run_subagent(
                            RunWritableSubagentRequest(parent_session_id, "timeout after LSP"),
                        )
                    timeout_provider = provider_factory.providers[-1]
                    timeout_manager = timeout_provider.lsp_manager
                    assert timeout_manager is not None
                    self.assertTrue(timeout_provider.blocking_started.is_set())
                    self.assertTrue(timeout_manager._closed)
                    self.assertEqual(timeout_manager._routes, {})
                    timeout_lease = (
                        await application.store.list_writable_subagent_leases(
                            parent_session_id=parent_session_id
                        )
                    )[-1]
                    self.assertIs(
                        timeout_lease.state,
                        WritableSubagentWorkspaceState.PRESERVED,
                    )
                    self.assertEqual(timeout_lease.error_kind, "SubagentTimeoutError")
                    for lifecycle_provider in (
                        failure_provider,
                        cancel_provider,
                        timeout_provider,
                    ):
                        lifecycle_sandbox = next(
                            sandbox
                            for sandbox in process_sandboxes
                            if sandbox.workspace_root == lifecycle_provider.cwd
                        )
                        self.assertTrue(lifecycle_sandbox.processes)
                        self.assertTrue(
                            all(
                                process.returncode is not None
                                for process in lifecycle_sandbox.processes
                            )
                        )
                    service._timeout_seconds = 120.0

                    for escape_case in (
                        "relative-parent",
                        "absolute-parent",
                        "sibling-worktree",
                        "symlink-parent",
                        "state-dir",
                    ):
                        case[0] = escape_case
                        trace.clear()
                        escaped = await service.run_subagent(
                            RunWritableSubagentRequest(
                                parent_session_id,
                                f"attempt {escape_case}",
                            ),
                        )
                        child_provider = provider_factory.providers[-1]
                        if escape_case != "symlink-parent":
                            self.assertFalse(escaped.workspace_changed, escape_case)
                        self.assertIsNotNone(child_provider.target_path, escape_case)
                        if escape_case in {"sibling-worktree", "state-dir"}:
                            assert child_provider.target_path is not None
                            self.assertEqual(
                                child_provider.target_path.read_bytes(),
                                b"committed\n",
                                escape_case,
                            )
                        else:
                            self.assertEqual(
                                (repository / "tracked.txt").read_bytes(),
                                parent_tracked,
                                escape_case,
                            )
                        escape_lease = (
                            await application.store.list_writable_subagent_leases(
                                parent_session_id=parent_session_id
                            )
                        )[-1]
                        self.assertIs(
                            escape_lease.state,
                            WritableSubagentWorkspaceState.PRESERVED,
                        )
                        if escape_case == "symlink-parent" and "symlink_unavailable" in trace:
                            continue

                    self.assertEqual(
                        _run_git(repository, "rev-parse", "HEAD"), head.encode() + b"\n"
                    )
                    self.assertEqual(
                        _run_git(repository, "status", "--porcelain=v2", "-z"),
                        parent_status,
                    )
                    self.assertEqual((repository / ".git" / "index").read_bytes(), parent_index)
                    self.assertEqual(
                        _run_git(repository, "symbolic-ref", "--short", "HEAD"), parent_branch
                    )
                    self.assertEqual((repository / "tracked.txt").read_bytes(), parent_tracked)
                    self.assertEqual(
                        (repository / "parent-dirty.txt").read_bytes(),
                        b"parent dirty bytes\n",
                    )
                    self.assertEqual(
                        (repository / "AGENTS.md").read_text(encoding="utf-8"),
                        "PARENT_DIRTY_INSTRUCTION\n",
                    )
                    self.assertIn(
                        "parent-dirty-skill",
                        skill_file.read_text(encoding="utf-8"),
                    )
                    self.assertEqual(state_target.read_bytes(), b"state bytes\n")
                finally:
                    await application.close()

    async def test_run_starts_from_committed_parent_and_preserves_child_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                parent,
                worktrees,
                checkpoints,
                factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            parent_before = hashlib.sha256((parent / "parent.txt").read_bytes()).hexdigest()
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "make the child change"),
            )

            self.assertIs(result.status, SessionTaskStatus.COMPLETED)
            self.assertEqual(result.base_commit_sha, BASE_SHA)
            self.assertTrue(result.workspace_changed)
            self.assertEqual(result.changed_file_count, 1)
            self.assertIsNotNone(result.final_workspace_fingerprint)
            self.assertIsNotNone(worktrees.snapshot)
            self.assertTrue((worktrees.snapshot.canonical_path).is_dir())
            self.assertEqual(
                hashlib.sha256((parent / "parent.txt").read_bytes()).hexdigest(), parent_before
            )
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(len(leases), 1)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)
            self.assertIsNotNone(await checkpoints.get(CheckpointId(result.baseline_checkpoint_id)))
            task = await store.get_session_task(parent_session_id, result.parent_task_id)
            self.assertIsNotNone(task)
            self.assertIs(task.status, SessionTaskStatus.COMPLETED)
            self.assertIsNotNone(factory.runtime)
            self.assertTrue(factory.runtime.closed)
            lease = (
                await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            )[0]
            self.assertIsNotNone(lease.child_session_id)
            assert lease.child_session_id is not None
            with self.assertRaisesRegex(SessionError, "writable workspace"):
                await store.delete_session(parent_session_id)
            with self.assertRaisesRegex(SessionError, "writable workspace"):
                await store.delete_session(lease.child_session_id)
            self.assertIsNotNone(await store.get_session(parent_session_id))
            self.assertIsNotNone(await store.get_session(lease.child_session_id))
            self.assertTrue(worktrees.snapshot.canonical_path.is_dir())
            baseline = await checkpoints.get(CheckpointId(result.baseline_checkpoint_id))
            self.assertIsNotNone(baseline)
            assert baseline is not None
            self.assertIs(baseline.state, CheckpointState.READY)

    async def test_provider_failure_preserves_workspace_and_durable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                _factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root, error=RuntimeError("provider failed"))
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "fail"),
                )
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(len(leases), 1)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)
            self.assertEqual(leases[0].error_kind, "RuntimeError")

    async def test_populated_schema_15_lease_migrates_to_schema_16_and_keeps_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                _factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "populate the lease"),
            )
            before = (
                await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            )[0]
            self.assertEqual(before.child_session_id, result.child_session_id)
            connection = sqlite3.connect(store.database_path)
            connection.execute("UPDATE schema_meta SET version = 15 WHERE singleton = 1")
            connection.commit()
            connection.close()

            migrated = SqliteSessionStore(store.database_path)
            await migrated.initialize()
            connection = sqlite3.connect(store.database_path)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (16,),
            )
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(writable_subagent_leases)"
            ).fetchall()
            connection.close()
            self.assertEqual({row[6] for row in foreign_keys}, {"RESTRICT"})
            after = (
                await migrated.list_writable_subagent_leases(parent_session_id=parent_session_id)
            )[0]
            self.assertEqual(after, before)
            transitioned = await migrated.compare_and_transition_writable_subagent_lease(
                replace(after, error_kind="post-migration-cas"),
                expected_version=after.version,
                expected_state=after.state,
            )
            self.assertEqual(transitioned.version, before.version + 1)
            self.assertEqual(transitioned.error_kind, "post-migration-cas")

    async def test_cancellation_preserves_workspace_and_marks_task_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root, block=True)
            running = asyncio.create_task(
                service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "cancel"),
                )
            )
            for _ in range(3000):
                if factory.runtime is not None:
                    break
                if running.done():
                    await running
                await asyncio.sleep(0.01)
            self.assertIsNotNone(factory.runtime)
            await factory.runtime.started.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(len(leases), 1)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)
            task = await store.get_session_task(parent_session_id, leases[0].parent_task_id)
            self.assertIsNotNone(task)
            self.assertIs(task.status, SessionTaskStatus.CANCELLED)
            self.assertTrue(factory.runtime.closed)

    async def test_service_validates_entry_points_and_initializes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                _store,
                _parent,
                _worktrees,
                _checkpoints,
                _factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            await service.initialize()
            service._initialized = False
            with self.assertRaises(ConfigurationError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "not initialized"),
                )
            service._initialized = True
            with self.assertRaises(ValueError):
                await service.run_subagent(
                    object(),  # type: ignore[arg-type]
                )
            with self.assertRaises(ConfigurationError):
                await service.run_subagent(
                    RunWritableSubagentRequest("different-parent", "invalid parent"),
                )

    async def test_request_parent_session_mismatch_rejects_before_resource_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                worktrees,
                checkpoints,
                factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            with self.assertRaisesRegex(ConfigurationError, "parent session"):
                await service.run_subagent(
                    RunWritableSubagentRequest("different-parent", "must reject"),
                )
            self.assertIsNone(worktrees.snapshot)
            self.assertEqual(checkpoints.checkpoints, {})
            self.assertIsNone(factory.runtime)
            self.assertEqual(
                await store.list_writable_subagent_leases(parent_session_id=parent_session_id),
                (),
            )
            self.assertEqual(await store.list_session_tasks(parent_session_id), [])

    async def test_read_only_parent_binding_cannot_be_forged_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read_only = _capability(
                root / "parent",
                tools=("read_file", "grep"),
                sandbox=SandboxProfile.READ_ONLY,
            )
            with self.assertRaisesRegex(ConfigurationError, "writable authority"):
                await self._service(root, bound_capabilities=read_only)

    @unittest.skipUnless(os.name == "nt", "Windows process-handle acceptance")
    async def test_windows_real_dead_owner_is_reconciled_without_cleanup(self) -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(owner_is_alive(child.pid))
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    service,
                    store,
                    root_parent,
                    worktrees,
                    checkpoints,
                    _factory,
                    parent_capabilities,
                    parent_session_id,
                ) = await self._service(root)
                worktree_id = WorktreeId("wt-windows-dead-owner")
                handle = WorktreeHandle(
                    worktree_id=worktree_id,
                    repository=worktrees.repository,
                    path=worktrees.planned_managed_path(worktrees.repository, worktree_id),
                    base_commit_sha=BASE_SHA,
                    branch="neuro/writable-subagent/wt-windows-dead-owner",
                )
                worktrees.snapshot = _fake_snapshot(handle)
                checkpoint = await checkpoints.create(CheckpointCreateRequest(handle))
                lease = _reconciliation_lease(
                    root_parent.parent,
                    parent_session_id,
                    parent_capabilities,
                    worktree_id_value=worktree_id.value,
                    state=WritableSubagentWorkspaceState.ACTIVE,
                    owner_pid=child.pid,
                    worktree=handle,
                    baseline_checkpoint_id=checkpoint.checkpoint_id,
                )
                await _insert_reconciliation_lease(store, lease)
                child.terminate()
                await asyncio.wait_for(child.wait(), timeout=20)
                self.assertFalse(owner_is_alive(child.pid))
                reconciled = await service.reconcile_writable_subagent_workspaces()
                self.assertEqual(len(reconciled), 1)
                self.assertIs(reconciled[0].state, WritableSubagentWorkspaceState.ORPHANED)
                self.assertEqual(reconciled[0].error_kind, "dead_writable_subagent_owner")
                self.assertTrue(handle.path.is_dir())
                self.assertIsNotNone(await checkpoints.get(checkpoint.checkpoint_id))
        finally:
            if child.returncode is None:
                child.kill()
                await asyncio.wait_for(child.wait(), timeout=20)

    async def test_non_ready_baseline_fails_before_child_authority_and_preserves_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                worktrees,
                _checkpoints,
                _factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root, checkpoint_state=CheckpointState.CAPTURING)
            with self.assertRaisesRegex(ConfigurationError, "baseline checkpoint"):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "baseline must be ready"),
                )
            self.assertIsNotNone(worktrees.snapshot)
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(len(leases), 1)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.FAILED)
            task = await store.get_session_task(parent_session_id, leases[0].parent_task_id)
            self.assertIsNotNone(task)
            self.assertIs(task.status, SessionTaskStatus.FAILED)

    async def test_timeout_preserves_workspace_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root, block=True)
            service._timeout_seconds = 0.01
            with self.assertRaises(SubagentTimeoutError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "timeout"),
                )
            self.assertIsNotNone(factory.runtime)
            self.assertTrue(factory.runtime.closed)
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)
            self.assertEqual(leases[0].error_kind, "SubagentTimeoutError")
            task = await store.get_session_task(parent_session_id, leases[0].parent_task_id)
            self.assertIsNotNone(task)
            self.assertIs(task.status, SessionTaskStatus.FAILED)

    async def test_runtime_identity_and_result_identity_are_verified(self) -> None:
        cases = (
            {"runtime_fingerprint": "e" * 64, "message": "capability metadata"},
            {"runtime_child_session_id": "unexpected-child", "message": "session identity"},
            {"result_session_id": "unexpected-result", "message": "different child session"},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    service,
                    store,
                    _parent,
                    _worktrees,
                    _checkpoints,
                    _factory,
                    _parent_capabilities,
                    parent_session_id,
                ) = await self._service(
                    root, **{key: value for key, value in case.items() if key != "message"}
                )
                with self.assertRaisesRegex(ConfigurationError, case["message"]):
                    await service.run_subagent(
                        RunWritableSubagentRequest(parent_session_id, f"identity {index}"),
                    )
                leases = await store.list_writable_subagent_leases(
                    parent_session_id=parent_session_id
                )
                self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)

    async def test_close_failure_returns_bounded_failed_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                _factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(
                root,
                response="x" * (MAX_WRITABLE_SUBAGENT_RESULT_BYTES + 100),
                close_error=RuntimeError("close failed"),
            )
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "bounded result"),
            )
            self.assertIs(result.status, SessionTaskStatus.FAILED)
            self.assertTrue(result.truncated)
            self.assertLessEqual(len(result.response.encode()), MAX_WRITABLE_SUBAGENT_RESULT_BYTES)
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(leases[0].error_kind, "RuntimeError")

    async def test_invalid_child_session_id_fails_before_runtime_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                _worktrees,
                _checkpoints,
                factory,
                _parent_capabilities,
                parent_session_id,
            ) = await self._service(root, created_session_id="bad\x00child")
            with self.assertRaises(ValueError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "invalid child id"),
                )
            self.assertIsNone(factory.runtime)
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)

    async def test_reconciliation_marks_missing_baseline_as_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                parent,
                worktrees,
                _checkpoints,
                _factory,
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            repository = _repository(root)
            worktree_id = WorktreeId("wt-reconcile")
            child_root = worktrees.planned_managed_path(repository, worktree_id)
            child_root.mkdir(parents=True)
            handle = WorktreeHandle(
                worktree_id=worktree_id,
                repository=repository,
                path=child_root,
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-reconcile",
            )
            worktrees.snapshot = WorktreeSnapshot(
                worktree_id=worktree_id,
                repository=repository,
                canonical_path=child_root,
                base_revision=BASE_SHA,
                base_commit_sha=BASE_SHA,
                branch=handle.branch,
                kind=WorktreeKind.MANAGED_BRANCH,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.READY,
                created_at=datetime(2026, 8, 23, tzinfo=UTC),
            )
            now = datetime(2026, 8, 23, tzinfo=UTC)
            lease = WritableSubagentWorkspaceLease(
                lease_id="wsl-reconcile",
                parent_session_id=parent_session_id,
                parent_task_id="writable-subagent-reconcile",
                worktree_id=worktree_id,
                parent_capability_fingerprint=parent_capabilities.fingerprint,
                parent_workspace_root=parent,
                parent_repository=repository,
                base_commit_sha=BASE_SHA,
                canonical_child_root=child_root,
                state=WritableSubagentWorkspaceState.ALLOCATING,
                created_at=now,
                updated_at=now,
                worktree=handle,
                baseline_checkpoint_id=CheckpointId("cp-missing"),
                owner_pid=os.getpid(),
            )
            await store.insert_writable_subagent_lease(lease)
            await store.compare_and_transition_writable_subagent_lease(
                replace(lease, state=WritableSubagentWorkspaceState.BASELINE_READY),
                expected_version=0,
                expected_state=WritableSubagentWorkspaceState.ALLOCATING,
            )
            reconciled = await service.reconcile_writable_subagent_workspaces()
            self.assertEqual(len(reconciled), 1)
            self.assertIs(reconciled[0].state, WritableSubagentWorkspaceState.ORPHANED)
            self.assertEqual(reconciled[0].error_kind, "baseline_checkpoint_unavailable")

    async def test_reconciliation_classifies_missing_worktree_by_owner_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                worktrees,
                _checkpoints,
                _factory,
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            worktrees.inspect_error = WorktreeError(
                "worktree missing",
                kind=WorktreeFailureKind.REPOSITORY_MISSING,
            )
            dead = _reconciliation_lease(
                root,
                parent_session_id,
                parent_capabilities,
                worktree_id_value="wt-dead-allocation",
                state=WritableSubagentWorkspaceState.ALLOCATING,
                owner_pid=None,
                worktree=None,
                baseline_checkpoint_id=None,
            )
            alive = _reconciliation_lease(
                root,
                await store.create_session(str(root / "alive-parent"), "fixture", "model"),
                parent_capabilities,
                worktree_id_value="wt-alive-allocation",
                state=WritableSubagentWorkspaceState.ALLOCATING,
                owner_pid=os.getpid(),
                worktree=None,
                baseline_checkpoint_id=None,
            )
            await store.insert_writable_subagent_lease(dead)
            await store.insert_writable_subagent_lease(alive)
            reconciled = await service.reconcile_writable_subagent_workspaces()
            states = {lease.worktree_id.value: lease for lease in reconciled}
            self.assertIs(
                states[dead.worktree_id.value].state, WritableSubagentWorkspaceState.FAILED
            )
            self.assertEqual(
                states[dead.worktree_id.value].error_kind,
                "allocation_intent_without_worktree",
            )
            self.assertIs(
                states[alive.worktree_id.value].state, WritableSubagentWorkspaceState.ORPHANED
            )
            self.assertEqual(
                states[alive.worktree_id.value].error_kind,
                "managed_worktree_unavailable",
            )

    async def test_reconciliation_recovers_handle_and_classifies_identity_and_dead_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                worktrees,
                checkpoints,
                _factory,
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            repository = _repository(root)
            recovering_id = WorktreeId("wt-recover-handle")
            recovering_handle = WorktreeHandle(
                worktree_id=recovering_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, recovering_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-recover-handle",
            )
            worktrees.snapshot = _fake_snapshot(recovering_handle)
            recovering = _reconciliation_lease(
                root,
                parent_session_id,
                parent_capabilities,
                worktree_id_value=recovering_id.value,
                state=WritableSubagentWorkspaceState.WORKTREE_READY,
                owner_pid=os.getpid(),
                worktree=None,
                baseline_checkpoint_id=None,
            )
            await _insert_reconciliation_lease(store, recovering)
            recovered = await service.reconcile_writable_subagent_workspaces()
            self.assertIs(recovered[0].state, WritableSubagentWorkspaceState.WORKTREE_READY)
            self.assertIsNotNone(recovered[0].worktree)

            mismatched_id = WorktreeId("wt-identity-mismatch")
            mismatched_handle = WorktreeHandle(
                worktree_id=mismatched_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, mismatched_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-identity-mismatch",
            )
            mismatched_snapshot = _fake_snapshot(mismatched_handle)
            worktrees.snapshot = replace(mismatched_snapshot, ownership=WorktreeOwnership.EXTERNAL)
            mismatched = _reconciliation_lease(
                root,
                await store.create_session(str(root / "mismatch-parent"), "fixture", "model"),
                parent_capabilities,
                worktree_id_value=mismatched_id.value,
                state=WritableSubagentWorkspaceState.WORKTREE_READY,
                owner_pid=os.getpid(),
                worktree=mismatched_handle,
                baseline_checkpoint_id=None,
            )
            await _insert_reconciliation_lease(store, mismatched)

            snapshots = {
                recovering_id.value: _fake_snapshot(recovering_handle),
                mismatched_id.value: replace(
                    mismatched_snapshot,
                    ownership=WorktreeOwnership.EXTERNAL,
                ),
            }

            async def inspect_by_id(worktree_id: str, /) -> WorktreeSnapshot:
                return snapshots[worktree_id]

            worktrees.inspect = inspect_by_id  # type: ignore[method-assign]
            reconciled = await service.reconcile_writable_subagent_workspaces()
            by_id = {lease.worktree_id.value: lease for lease in reconciled}
            self.assertIs(by_id[mismatched_id.value].state, WritableSubagentWorkspaceState.ORPHANED)
            self.assertEqual(
                by_id[mismatched_id.value].error_kind, "worktree_identity_or_state_mismatch"
            )

            dead_id = WorktreeId("wt-dead-owner")
            dead_handle = WorktreeHandle(
                worktree_id=dead_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, dead_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-dead-owner",
            )
            worktrees.snapshot = _fake_snapshot(dead_handle)
            checkpoint = await checkpoints.create(CheckpointCreateRequest(dead_handle))
            dead = _reconciliation_lease(
                root,
                await store.create_session(str(root / "dead-parent"), "fixture", "model"),
                parent_capabilities,
                worktree_id_value=dead_id.value,
                state=WritableSubagentWorkspaceState.ACTIVE,
                owner_pid=None,
                worktree=dead_handle,
                baseline_checkpoint_id=checkpoint.checkpoint_id,
            )
            await _insert_reconciliation_lease(store, dead)
            snapshots[dead_id.value] = _fake_snapshot(dead_handle)

            ready_dead_id = WorktreeId("wt-ready-dead-owner")
            ready_dead_handle = WorktreeHandle(
                worktree_id=ready_dead_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, ready_dead_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-ready-dead-owner",
            )
            ready_dead = _reconciliation_lease(
                root,
                await store.create_session(str(root / "ready-dead-parent"), "fixture", "model"),
                parent_capabilities,
                worktree_id_value=ready_dead_id.value,
                state=WritableSubagentWorkspaceState.WORKTREE_READY,
                owner_pid=None,
                worktree=ready_dead_handle,
                baseline_checkpoint_id=None,
            )
            await _insert_reconciliation_lease(store, ready_dead)
            snapshots[ready_dead_id.value] = _fake_snapshot(ready_dead_handle)
            reconciled = await service.reconcile_writable_subagent_workspaces()
            by_id = {lease.worktree_id.value: lease for lease in reconciled}
            self.assertIs(by_id[dead_id.value].state, WritableSubagentWorkspaceState.ORPHANED)
            self.assertEqual(by_id[dead_id.value].error_kind, "dead_writable_subagent_owner")
            self.assertIs(
                by_id[ready_dead_id.value].state,
                WritableSubagentWorkspaceState.ORPHANED,
            )
            self.assertEqual(
                by_id[ready_dead_id.value].error_kind,
                "worktree_ready_without_baseline",
            )

    async def test_reconciliation_detects_missing_handle_and_baseline_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                service,
                store,
                _parent,
                worktrees,
                _checkpoints,
                _factory,
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            repository = _repository(root)
            missing_handle_id = WorktreeId("wt-missing-handle")
            missing_handle = WorktreeHandle(
                worktree_id=missing_handle_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, missing_handle_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-missing-handle",
            )
            worktrees.snapshot = _fake_snapshot(missing_handle)
            missing_handle_lease = _reconciliation_lease(
                root,
                parent_session_id,
                parent_capabilities,
                worktree_id_value=missing_handle_id.value,
                state=WritableSubagentWorkspaceState.BASELINE_READY,
                owner_pid=os.getpid(),
                worktree=None,
                baseline_checkpoint_id=CheckpointId("cp-link"),
            )
            missing_link_id = WorktreeId("wt-missing-link")
            missing_link = WorktreeHandle(
                worktree_id=missing_link_id,
                repository=repository,
                path=worktrees.planned_managed_path(repository, missing_link_id),
                base_commit_sha=BASE_SHA,
                branch="neuro/writable-subagent/wt-missing-link",
            )
            worktrees.snapshot = _fake_snapshot(missing_link)
            missing_link_lease = _reconciliation_lease(
                root,
                await store.create_session(str(root / "missing-link-parent"), "fixture", "model"),
                parent_capabilities,
                worktree_id_value=missing_link_id.value,
                state=WritableSubagentWorkspaceState.BASELINE_READY,
                owner_pid=os.getpid(),
                worktree=missing_link,
                baseline_checkpoint_id=None,
            )
            await _insert_reconciliation_lease(store, missing_handle_lease)
            await _insert_reconciliation_lease(store, missing_link_lease)

            original_snapshot = worktrees.snapshot

            async def inspect_by_id(worktree_id: str, /) -> WorktreeSnapshot:
                if worktree_id == missing_handle_id.value:
                    return _fake_snapshot(missing_handle)
                if worktree_id == missing_link_id.value:
                    return original_snapshot
                raise AssertionError("unexpected reconciliation worktree")

            worktrees.inspect = inspect_by_id  # type: ignore[method-assign]
            reconciled = await service.reconcile_writable_subagent_workspaces()
            by_id = {lease.worktree_id.value: lease for lease in reconciled}
            self.assertEqual(
                by_id[missing_handle_id.value].error_kind, "persisted_worktree_handle_missing"
            )
            self.assertEqual(
                by_id[missing_link_id.value].error_kind, "baseline_checkpoint_link_missing"
            )


if __name__ == "__main__":
    unittest.main()
