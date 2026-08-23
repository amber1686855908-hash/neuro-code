from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.checkpoints import WorkspaceCheckpointApplicationService
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.worktree import WorktreeError, WorktreeFailureKind
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseError
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
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
)
from neuro_code.application.worktrees import WorktreeApplicationService
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
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import SessionTaskStatus
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
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.persistence.checkpoint_artifacts import LocalCheckpointArtifactStore
from neuro_code.infrastructure.persistence.managed_worktrees import SqliteManagedWorktreeStore
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.workspace_checkpoints import (
    SqliteWorkspaceCheckpointStore,
)
from neuro_code.infrastructure.tools.filesystem import ApplyPatchTool, SearchReplaceTool
from neuro_code.infrastructure.workspace.checkpoints import LocalWorkspaceStateAdapter
from neuro_code.shared.errors import ConfigurationError, SubagentTimeoutError, ToolError

BASE_SHA = "a" * 40


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
            requested = writable_subagent_request(parent, max_steps=8)

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
                    requested=writable_subagent_request(read_only_parent, max_steps=8),
                    global_policy=global_policy,
                    workspace_grant=grant,
                )

            forged = replace(grant, parent_capability_fingerprint="b" * 64)
            with self.assertRaises(ConfigurationError):
                resolve_writable_subagent_capability(
                    parent=parent,
                    requested=writable_subagent_request(parent, max_steps=8),
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
            requested = writable_subagent_request(parent, max_steps=8)

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
            readonly_request = writable_subagent_request(readonly_parent, max_steps=8)
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
                writable_subagent_request(object(), max_steps=8)  # type: ignore[arg-type]

    def test_writable_capability_grant_validates_its_composed_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = _capability(root / "parent")
            global_policy = _capability(root / "global", max_steps=12)
            grant = _grant(root, parent)
            resolved = resolve_writable_subagent_capability(
                parent=parent,
                requested=writable_subagent_request(parent, max_steps=8),
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
            with self.assertRaises(ValueError):
                replace(grant, parent_workspace_root=root / "outside")
            with self.assertRaises(ValueError):
                replace(grant, base_commit_sha="not-a-commit")
            with self.assertRaises(TypeError):
                replace(grant, worktree=object())
            with self.assertRaises(ValueError):
                replace(grant, managed_worktree_id=WorktreeId("different"))
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
            self.assertFalse(lease.grant_ready)
            self.assertIsNone(lease.effective_fingerprint)
            with self.assertRaises(ValueError):
                replace(lease, lease_id="")
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
                replace(lease, owner_pid=0)
            with self.assertRaises(TypeError):
                replace(lease, workspace_changed="yes")
            with self.assertRaises(ValueError):
                replace(lease, changed_file_count=-1)
            with self.assertRaises(ValueError):
                replace(lease, version=-1)

            with self.assertRaises(ValueError):
                _ = lease.grant


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


class _RealRuntime:
    def __init__(self, session_id: str, capabilities) -> None:
        self.child_session_id = session_id
        self.capability_fingerprint = capabilities.fingerprint
        self.cwd = capabilities.capabilities.cwd
        self.closed = False

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        (self.cwd / "child.txt").write_text(prompt, encoding="utf-8")
        return AgentRunResult(self.child_session_id, "real child completed", (), (), (), 1)

    async def close(self) -> None:
        self.closed = True


class _RealRuntimeFactory:
    def __init__(self, store: SqliteSessionStore) -> None:
        self.store = store
        self.runtime: _RealRuntime | None = None

    async def create_session(self, request: RunWritableSubagentRequest, *, capabilities):
        del request
        return await self.store.create_session(
            str(capabilities.capabilities.cwd),
            "fixture-child",
            "model",
            sandbox_profile=capabilities.capabilities.sandbox_profile,
        )

    async def create(
        self,
        request: RunWritableSubagentRequest,
        *,
        parent_task_id: str,
        child_session_id: str,
        capabilities,
    ) -> _RealRuntime:
        del request, parent_task_id
        self.runtime = _RealRuntime(child_session_id, capabilities)
        return self.runtime


class WritableApplicationTests(unittest.IsolatedAsyncioTestCase):
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
        parent_capabilities = _capability(parent)
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            parent_before = hashlib.sha256((parent / "parent.txt").read_bytes()).hexdigest()
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "make the child change"),
                parent_capabilities=parent_capabilities,
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root, error=RuntimeError("provider failed"))
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "fail"),
                    parent_capabilities=parent_capabilities,
                )
            leases = await store.list_writable_subagent_leases(parent_session_id=parent_session_id)
            self.assertEqual(len(leases), 1)
            self.assertIs(leases[0].state, WritableSubagentWorkspaceState.PRESERVED)
            self.assertEqual(leases[0].error_kind, "RuntimeError")

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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root, block=True)
            running = asyncio.create_task(
                service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "cancel"),
                    parent_capabilities=parent_capabilities,
                )
            )
            for _ in range(500):
                if factory.runtime is not None:
                    break
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root)
            await service.initialize()
            service._initialized = False
            with self.assertRaises(ConfigurationError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "not initialized"),
                    parent_capabilities=parent_capabilities,
                )
            service._initialized = True
            with self.assertRaises(ValueError):
                await service.run_subagent(
                    object(),  # type: ignore[arg-type]
                    parent_capabilities=parent_capabilities,
                )
            with self.assertRaises(ConfigurationError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "invalid parent"),
                    parent_capabilities=object(),  # type: ignore[arg-type]
                )

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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root, checkpoint_state=CheckpointState.CAPTURING)
            with self.assertRaisesRegex(ConfigurationError, "baseline checkpoint"):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "baseline must be ready"),
                    parent_capabilities=parent_capabilities,
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root, block=True)
            service._timeout_seconds = 0.01
            with self.assertRaises(SubagentTimeoutError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "timeout"),
                    parent_capabilities=parent_capabilities,
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
                    parent_capabilities,
                    parent_session_id,
                ) = await self._service(
                    root, **{key: value for key, value in case.items() if key != "message"}
                )
                with self.assertRaisesRegex(ConfigurationError, case["message"]):
                    await service.run_subagent(
                        RunWritableSubagentRequest(parent_session_id, f"identity {index}"),
                        parent_capabilities=parent_capabilities,
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(
                root,
                response="x" * (MAX_WRITABLE_SUBAGENT_RESULT_BYTES + 100),
                close_error=RuntimeError("close failed"),
            )
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "bounded result"),
                parent_capabilities=parent_capabilities,
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
                parent_capabilities,
                parent_session_id,
            ) = await self._service(root, created_session_id="bad\x00child")
            with self.assertRaises(ValueError):
                await service.run_subagent(
                    RunWritableSubagentRequest(parent_session_id, "invalid child id"),
                    parent_capabilities=parent_capabilities,
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
            reconciled = await service.reconcile_writable_subagent_workspaces()
            by_id = {lease.worktree_id.value: lease for lease in reconciled}
            self.assertIs(by_id[dead_id.value].state, WritableSubagentWorkspaceState.ORPHANED)
            self.assertEqual(by_id[dead_id.value].error_kind, "dead_writable_subagent_owner")

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

    async def test_real_git_worktree_keeps_dirty_parent_byte_for_byte_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, head = _make_real_repository(root)
            (repository / "tracked.txt").write_bytes(b"dirty staged content\n")
            _run_git(repository, "add", "tracked.txt")
            (repository / "untracked.txt").write_bytes(b"dirty untracked content\n")
            parent_status = _run_git(repository, "status", "--porcelain=v2", "-z")
            parent_index = (repository / ".git" / "index").read_bytes()
            parent_branch = _run_git(repository, "symbolic-ref", "--short", "HEAD")
            parent_tracked = (repository / "tracked.txt").read_bytes()

            session_store = SqliteSessionStore(root / "sessions.db")
            await session_store.initialize()
            parent_session_id = await session_store.create_session(
                str(repository), "fixture", "model"
            )
            worktree_store = SqliteManagedWorktreeStore(root / "worktrees.db")
            git = LocalGitWorktreeAdapter(hooks_directory=root / "hooks")
            worktrees = WorktreeApplicationService(
                git=git,
                store=worktree_store,
                managed_root=root / "managed-worktrees",
            )
            checkpoint_service = WorkspaceCheckpointApplicationService(
                git=git,
                workspace_git=git,
                worktrees=worktree_store,
                state=LocalWorkspaceStateAdapter(git=git, workspace_git=git),
                checkpoints=SqliteWorkspaceCheckpointStore(root / "checkpoints.db"),
                artifacts=LocalCheckpointArtifactStore(root),
            )
            factory = _RealRuntimeFactory(session_store)
            service = WritableSubagentApplicationService(
                session_store,
                session_store,
                worktrees,
                checkpoint_service,
                factory,
                global_policy=_capability(root / "global", max_steps=12),
            )
            await service.initialize()
            result = await service.run_subagent(
                RunWritableSubagentRequest(parent_session_id, "write only in child"),
                parent_capabilities=_capability(repository),
            )

            self.assertEqual(result.base_commit_sha, head)
            self.assertTrue(result.workspace_changed)
            snapshot = await worktrees.inspect(result.worktree_id.value)
            self.assertTrue((snapshot.canonical_path / "child.txt").is_file())
            checkpoint = await checkpoint_service.get(CheckpointId(result.baseline_checkpoint_id))
            self.assertIsNotNone(checkpoint)
            self.assertIs(checkpoint.state, CheckpointState.READY)
            self.assertEqual(_run_git(repository, "status", "--porcelain=v2", "-z"), parent_status)
            self.assertEqual((repository / ".git" / "index").read_bytes(), parent_index)
            self.assertEqual(_run_git(repository, "symbolic-ref", "--short", "HEAD"), parent_branch)
            self.assertEqual((repository / "tracked.txt").read_bytes(), parent_tracked)
            self.assertTrue(factory.runtime is not None and factory.runtime.closed)


if __name__ == "__main__":
    unittest.main()
