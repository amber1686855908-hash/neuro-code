from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from neuro_code.application.ports.sandbox import LocalProcessNetworkPolicy, LocalProcessPurpose
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemTargetRequest,
)
from neuro_code.application.ports.worktree import (
    MAX_GIT_OUTPUT_BYTES,
    GitWorktreeRecord,
    WorktreeError,
    WorktreeFailureKind,
)
from neuro_code.application.worktrees import WorktreeApplicationService
from neuro_code.domain.worktree import (
    WorktreeCreateRequest,
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRemoveRequest,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
    WorktreeWorkspaceBinding,
)
from neuro_code.infrastructure.git.worktree import (
    LocalGitWorktreeAdapter,
    parse_worktree_porcelain,
)
from neuro_code.infrastructure.persistence.managed_worktrees import (
    SCHEMA_VERSION,
    SqliteManagedWorktreeStore,
)
from neuro_code.infrastructure.workspace.paths import (
    FilesystemWorkspaceIdentity,
    resolve_filesystem_access_targets,
)


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _new_repository(root: Path) -> tuple[Path, str]:
    repository = root / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.eol", "lf")
    _git(repository, "config", "user.email", "neuro-code-tests@example.invalid")
    _git(repository, "config", "user.name", "Neuro Code Tests")
    (repository / "tracked.txt").write_bytes(b"committed\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository, _git(repository, "rev-parse", "HEAD")


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def _crash_child_add(repository: str, target: str, commit: str) -> None:
    try:
        asyncio.run(
            LocalGitWorktreeAdapter().add_worktree(
                Path(repository),
                Path(target),
                commit,
                branch=None,
            )
        )
    except BaseException:
        os._exit(1)
    os._exit(0)


def _crash_child_remove(repository: str, target: str) -> None:
    try:
        asyncio.run(LocalGitWorktreeAdapter().remove_worktree(Path(repository), Path(target)))
    except BaseException:
        os._exit(1)
    os._exit(0)


class _ControlledOutput:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int = -1, /) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _ControlledProcess:
    def __init__(self, *, stdout: tuple[bytes, ...], stderr: tuple[bytes, ...]) -> None:
        self.stdout = _ControlledOutput(stdout)
        self.stderr = _ControlledOutput(stderr)
        self.terminate_calls = 0
        self._terminated = asyncio.Event()

    async def wait(self) -> int:
        await self._terminated.wait()
        return -15

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        del grace_seconds
        self.terminate_calls += 1
        self._terminated.set()


class _ControlledProcessSandbox:
    def __init__(self, process: _ControlledProcess) -> None:
        self.process = process
        self.request = None

    async def spawn(self, request: object) -> _ControlledProcess:
        self.request = request
        return self.process


class WorktreeParserTests(unittest.TestCase):
    def test_porcelain_parser_handles_detached_branch_lock_prunable_and_unknown_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-parser-") as raw_root:
            root = Path(raw_root)
            repository = os.fsencode(str(root / "repo"))
            detached = os.fsencode(str(root / "detached"))
            stale = os.fsencode(str(root / "stale"))
            records = parse_worktree_porcelain(
                b"".join(
                    (
                        b"worktree "
                        + repository
                        + b"\0HEAD "
                        + b"a" * 40
                        + b"\0branch refs/heads/main\0unknown future\0\0",
                        b"worktree "
                        + detached
                        + b"\0HEAD "
                        + b"b" * 40
                        + b"\0detached\0locked by test\0\0",
                        b"worktree "
                        + stale
                        + b"\0HEAD "
                        + b"c" * 40
                        + b"\0branch refs/heads/stale\0prunable missing\0\0",
                    )
                )
            )
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].branch, "refs/heads/main")
        self.assertTrue(records[1].detached)
        self.assertTrue(records[1].locked)
        self.assertTrue(records[2].prunable)

    def test_porcelain_parser_rejects_missing_identity_or_ambiguous_branch(self) -> None:
        with self.assertRaises(WorktreeError) as missing:
            parse_worktree_porcelain(b"worktree /repo\0detached\0\0")
        self.assertEqual(missing.exception.kind, WorktreeFailureKind.PROTOCOL)
        with self.assertRaises(WorktreeError) as ambiguous:
            parse_worktree_porcelain(
                b"worktree /repo\0HEAD " + b"a" * 40 + b"\0branch refs/heads/main\0detached\0\0"
            )
        self.assertEqual(ambiguous.exception.kind, WorktreeFailureKind.PROTOCOL)
        with self.assertRaises(WorktreeError):
            parse_worktree_porcelain(b"worktree /repo\0HEAD \0detached\0\0")
        with self.assertRaises(WorktreeError):
            parse_worktree_porcelain(b"worktree /repo\0HEAD " + b"z" * 40 + b"\0detached\0\0")
        with self.assertRaises(WorktreeError):
            parse_worktree_porcelain(b"worktree /repo\0HEAD " + b"a" * 40 + b"\0branch\0\0")
        with self.assertRaises(WorktreeError):
            parse_worktree_porcelain(b"worktree relative\0HEAD " + b"a" * 40 + b"\0detached\0\0")


class WorktreeGitBoundaryTests(unittest.TestCase):
    def test_git_timeout_terminates_the_owned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-timeout-") as raw:
            process = _ControlledProcess(stdout=(b"",), stderr=(b"",))
            sandbox = _ControlledProcessSandbox(process)
            adapter = LocalGitWorktreeAdapter(local_process_sandbox=sandbox)  # type: ignore[arg-type]
            with self.assertRaises(WorktreeError) as timeout:
                _run(adapter._run_git(Path(raw), ("status",), timeout_seconds=0.01))
            self.assertEqual(timeout.exception.kind, WorktreeFailureKind.TIMEOUT)
            self.assertEqual(process.terminate_calls, 1)
            self.assertIsNotNone(sandbox.request)
            assert sandbox.request is not None
            self.assertTrue(Path(sandbox.request.executable).is_absolute())
            self.assertFalse(sandbox.request.uses_shell)
            self.assertIs(sandbox.request.purpose, LocalProcessPurpose.GIT_WORKTREE)
            self.assertIs(sandbox.request.network_policy, LocalProcessNetworkPolicy.ISOLATED)

    def test_git_output_limit_terminates_the_owned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-output-limit-") as raw:
            process = _ControlledProcess(
                stdout=(b"x" * (MAX_GIT_OUTPUT_BYTES + 1),),
                stderr=(b"y" * (MAX_GIT_OUTPUT_BYTES + 1),),
            )
            sandbox = _ControlledProcessSandbox(process)
            adapter = LocalGitWorktreeAdapter(local_process_sandbox=sandbox)  # type: ignore[arg-type]
            with self.assertRaises(WorktreeError) as output_limit:
                _run(adapter._run_git(Path(raw), ("status",)))
            self.assertEqual(output_limit.exception.kind, WorktreeFailureKind.OUTPUT_LIMIT)
            self.assertEqual(process.terminate_calls, 1)


class WorktreeDomainTests(unittest.TestCase):
    def test_ids_and_requests_are_bounded_and_typed(self) -> None:
        with self.assertRaises(ValueError):
            WorktreeId("../escape")
        with self.assertRaises(ValueError):
            WorktreeId("-option")
        with self.assertRaises(ValueError):
            WorktreeCreateRequest(Path("/repo"), "HEAD", kind=WorktreeKind.DETACHED, branch="main")

    def test_remove_request_does_not_hide_branch_deletion(self) -> None:
        with self.assertRaises(ValueError):
            WorktreeRemoveRequest(WorktreeId("wt-test"), delete_branch=True)

    def test_domain_values_reject_ambiguous_status_and_preserve_handle_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-domain-") as raw:
            root = Path(raw)
            head = "a" * 40
            identity = WorktreeRepositoryIdentity(
                root / ".git",
                root,
                root / ".git",
                head,
            )
            with self.assertRaises(ValueError):
                WorktreeStatus(root / "status", head, detached=True, branch="main")
            with self.assertRaises(ValueError):
                WorktreeStatus(root / "status", head, detached=False)
            with self.assertRaises(ValueError):
                WorktreeStatus(root / "status", head, detached=True, changed_file_count=-1)
            with self.assertRaises(TypeError):
                WorktreeStatus(root / "status", head, detached=True, locked=1)  # type: ignore[arg-type]
            self.assertEqual(
                GitWorktreeRecord(root / "worker", head, detached=True).path,
                (root / "worker").resolve(),
            )
            snapshot = WorktreeSnapshot(
                worktree_id=WorktreeId("wt-domain"),
                repository=identity,
                canonical_path=root / "worker",
                base_revision="HEAD",
                base_commit_sha=head,
                branch=None,
                kind=WorktreeKind.DETACHED,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.READY,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                created_by_session_id="session-1",
            )
            self.assertEqual(snapshot.handle.path, (root / "worker").resolve())
            self.assertEqual(snapshot.handle.base_commit_sha, head)
            with self.assertRaises(ValueError):
                WorktreeWorkspaceBinding(root / "worker", (root / "worker" / "nested",))
            with self.assertRaises(ValueError):
                WorktreeWorkspaceBinding(root / "worker", (root / "one", root / "one" / "two"))


class ManagedWorktreePersistenceTests(unittest.TestCase):
    def test_fresh_store_round_trips_ownership_and_has_independent_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-db-") as raw:
            root = Path(raw)
            common = root / "repo" / ".git"
            source = root / "repo"
            git_dir = common
            identity = WorktreeRepositoryIdentity(common, source, git_dir, "a" * 40)
            snapshot = WorktreeSnapshot(
                worktree_id=WorktreeId("wt-roundtrip"),
                repository=identity,
                canonical_path=root / "state" / "worktrees" / "wt-roundtrip",
                base_revision="HEAD",
                base_commit_sha="a" * 40,
                branch=None,
                kind=WorktreeKind.DETACHED,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.CREATING,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            database = root / "state" / "worktrees.db"
            store = SqliteManagedWorktreeStore(database)
            _run(store.initialize())
            _run(store.save(snapshot))
            reopened = SqliteManagedWorktreeStore(database)
            _run(reopened.initialize())
            loaded = _run(reopened.get("wt-roundtrip"))
            assert isinstance(loaded, WorktreeSnapshot)
            self.assertEqual(loaded, snapshot)
            self.assertEqual(_run(reopened.list()), (snapshot,))
            self.assertEqual(reopened.database_path, database.resolve())
            self.assertEqual(
                _run(reopened.list(include_removed=True, repository_id=identity.repository_id)),
                (snapshot,),
            )
            with self.assertRaises(TypeError):
                _run(reopened.list(include_removed=1))  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                _run(reopened.save(object()))  # type: ignore[arg-type]

            connection = __import__("sqlite3").connect(database)
            version = connection.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(managed_worktrees)").fetchall()
            }
            connection.close()
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertIn("created_by_session_id", columns)


class WorktreeApplicationTests(unittest.TestCase):
    def test_queries_and_fail_closed_states_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-queries-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(root / "state" / "worktrees.db"),
                managed_root=root / "state" / "worktrees",
                id_factory=lambda: WorktreeId("wt-queries"),
            )
            with self.assertRaises(WorktreeError) as uninitialized:
                _run(service.create(WorktreeCreateRequest(repository, head)))
            self.assertEqual(uninitialized.exception.kind, WorktreeFailureKind.FAILED_STATE)
            _run(service.initialize())
            ready = _run(service.create(WorktreeCreateRequest(repository, head)))
            listed = _run(service.list_managed(reconcile=False))
            self.assertEqual(tuple(item.worktree_id for item in listed), (ready.worktree_id,))
            self.assertEqual(listed[0].state, WorktreeState.READY)
            self.assertEqual(
                _run(service.status(ready.worktree_id.value)).path, ready.canonical_path
            )
            self.assertEqual(_run(service.get_handle(ready.worktree_id.value)), ready.handle)
            with self.assertRaises(WorktreeError) as unknown:
                _run(service.inspect("wt-unknown"))
            self.assertEqual(unknown.exception.kind, WorktreeFailureKind.UNMANAGED)
            removed = _run(service.remove(WorktreeRemoveRequest(ready.worktree_id)))
            self.assertEqual(
                _run(service.remove(WorktreeRemoveRequest(ready.worktree_id))), removed
            )
            with self.assertRaises(WorktreeError) as no_binding:
                _run(service.workspace_binding(ready.worktree_id.value))
            self.assertEqual(no_binding.exception.kind, WorktreeFailureKind.FAILED_STATE)
            with self.assertRaises(WorktreeError) as no_handle:
                _run(service.get_handle(ready.worktree_id.value))
            self.assertEqual(no_handle.exception.kind, WorktreeFailureKind.FAILED_STATE)

    def test_initialize_rejects_unsupported_git_version(self) -> None:
        class _OldGit:
            async def git_version(self) -> tuple[int, int, int]:
                return (2, 29, 0)

        with tempfile.TemporaryDirectory(prefix="neuro-worktree-version-") as raw:
            root = Path(raw)
            service = WorktreeApplicationService(
                git=_OldGit(),  # type: ignore[arg-type]
                store=SqliteManagedWorktreeStore(root / "state" / "worktrees.db"),
                managed_root=root / "state" / "worktrees",
            )
            with self.assertRaises(WorktreeError) as error:
                _run(service.initialize())
            self.assertEqual(error.exception.kind, WorktreeFailureKind.NOT_AVAILABLE)

    def test_concurrent_creation_is_serialized_and_keeps_ids_and_paths_distinct(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-concurrent-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            identifiers = iter((WorktreeId("wt-concurrent-a"), WorktreeId("wt-concurrent-b")))
            service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(root / "state" / "worktrees.db"),
                managed_root=root / "state" / "worktrees",
                id_factory=lambda: next(identifiers),
            )
            _run(service.initialize())

            async def create_both() -> tuple[WorktreeSnapshot, ...]:
                return tuple(
                    await asyncio.gather(
                        service.create(WorktreeCreateRequest(repository, head)),
                        service.create(WorktreeCreateRequest(repository, head)),
                    )
                )

            snapshots = _run(create_both())
            try:
                self.assertEqual(
                    {snapshot.worktree_id.value for snapshot in snapshots},
                    {"wt-concurrent-a", "wt-concurrent-b"},
                )
                self.assertEqual(
                    len({snapshot.canonical_path for snapshot in snapshots}),
                    2,
                )
                self.assertTrue(
                    all(snapshot.state is WorktreeState.READY for snapshot in snapshots)
                )
            finally:
                for snapshot in snapshots:
                    _run(service.remove(WorktreeRemoveRequest(snapshot.worktree_id)))

    def test_reconciliation_fails_closed_for_identity_and_lifecycle_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-reconcile-branches-") as raw:
            root = Path(raw)
            head = "a" * 40
            identity = WorktreeRepositoryIdentity(
                root / "common.git",
                root / "source",
                root / "git-dir",
                head,
            )
            other_identity = WorktreeRepositoryIdentity(
                root / "other.git",
                root / "source",
                root / "other-git-dir",
                head,
            )
            same_common_different_checkout = WorktreeRepositoryIdentity(
                identity.common_dir,
                root / "different-source",
                root / "different-git-dir",
                head,
            )
            clean_status = WorktreeStatus(root / "worker", head, detached=True)
            matching_record = GitWorktreeRecord(root / "worker", head, detached=True)

            def snapshot(state: WorktreeState, path: Path = root / "worker") -> WorktreeSnapshot:
                return WorktreeSnapshot(
                    worktree_id=WorktreeId(f"wt-{state.value}"),
                    repository=identity,
                    canonical_path=path,
                    base_revision="HEAD",
                    base_commit_sha=head,
                    branch=None,
                    kind=WorktreeKind.DETACHED,
                    ownership=WorktreeOwnership.MANAGED,
                    state=state,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )

            def service_for(
                *,
                repository: WorktreeRepositoryIdentity = identity,
                records: tuple[GitWorktreeRecord, ...] = (matching_record,),
                status: WorktreeStatus = clean_status,
            ) -> tuple[WorktreeApplicationService, Mock]:
                git = Mock()
                git.repository_identity = AsyncMock(return_value=repository)
                git.list_worktrees = AsyncMock(return_value=records)
                git.inspect_status = AsyncMock(return_value=status)
                service = WorktreeApplicationService(
                    git=git,
                    store=Mock(),
                    managed_root=root / "managed",
                )
                return service, git

            repository_error_service, repository_error_git = service_for()
            repository_error_git.repository_identity.side_effect = WorktreeError(
                "source disappeared", kind=WorktreeFailureKind.REPOSITORY_MISSING
            )
            self.assertEqual(
                _run(repository_error_service._reconcile_one(snapshot(WorktreeState.READY))).state,
                WorktreeState.ORPHANED,
            )
            mismatch_service, _ = service_for(repository=other_identity)
            self.assertEqual(
                _run(mismatch_service._reconcile_one(snapshot(WorktreeState.READY))).state,
                WorktreeState.ORPHANED,
            )
            checkout_mismatch_service, _ = service_for(repository=same_common_different_checkout)
            self.assertEqual(
                _run(checkout_mismatch_service._reconcile_one(snapshot(WorktreeState.READY))).state,
                WorktreeState.ORPHANED,
            )
            list_error_service, list_error_git = service_for()
            list_error_git.list_worktrees.side_effect = WorktreeError(
                "Git metadata unavailable", kind=WorktreeFailureKind.COMMAND_FAILED
            )
            self.assertEqual(
                _run(list_error_service._reconcile_one(snapshot(WorktreeState.READY))).state,
                WorktreeState.FAILED,
            )
            reused = root / "reused"
            reused.mkdir()
            reused_service, _ = service_for(records=())
            reused_result = _run(
                reused_service._reconcile_one(snapshot(WorktreeState.CREATING, reused))
            )
            self.assertEqual(reused_result.state, WorktreeState.ORPHANED)
            for state, expected in (
                (WorktreeState.CREATING, WorktreeState.FAILED),
                (WorktreeState.FAILED, WorktreeState.FAILED),
                (WorktreeState.REMOVING, WorktreeState.REMOVED),
                (WorktreeState.READY, WorktreeState.ORPHANED),
            ):
                missing_service, _ = service_for(records=())
                result = _run(missing_service._reconcile_one(snapshot(state, root / state.value)))
                self.assertEqual(result.state, expected)
            wrong_record = GitWorktreeRecord(root / "worker", "b" * 40, detached=True)
            wrong_service, _ = service_for(records=(wrong_record,))
            wrong_result = _run(wrong_service._reconcile_one(snapshot(WorktreeState.READY)))
            self.assertEqual(wrong_result.state, WorktreeState.ORPHANED)
            status_error_service, status_error_git = service_for()
            status_error_git.inspect_status.side_effect = WorktreeError(
                "status unavailable", kind=WorktreeFailureKind.COMMAND_FAILED
            )
            status_error = _run(status_error_service._reconcile_one(snapshot(WorktreeState.READY)))
            self.assertEqual(status_error.state, WorktreeState.FAILED)
            ready_service, _ = service_for()
            ready = _run(ready_service._reconcile_one(snapshot(WorktreeState.CREATING)))
            self.assertEqual(ready.state, WorktreeState.READY)
            self.assertIsNotNone(ready.status)

    def test_creation_and_removal_uncertainty_is_persisted_without_force_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-failure-branches-") as raw:
            root = Path(raw)
            head = "a" * 40
            repository = root / "source"
            repository.mkdir()
            identity = WorktreeRepositoryIdentity(
                root / "common.git",
                repository,
                root / "git-dir",
                head,
            )

            def new_git() -> Mock:
                git = Mock()
                git.git_version = AsyncMock(return_value=(2, 45, 0))
                git.repository_identity = AsyncMock(return_value=identity)
                git.resolve_commit = AsyncMock(return_value=head)
                git.validate_branch = AsyncMock(side_effect=lambda _path, branch: branch)
                git.branch_exists = AsyncMock(return_value=False)
                git.list_worktrees = AsyncMock(return_value=())
                git.inspect_status = AsyncMock(
                    return_value=WorktreeStatus(root / "worker", head, detached=True)
                )
                git.add_worktree = AsyncMock()
                git.remove_worktree = AsyncMock()
                return git

            failing_git = new_git()
            failing_git.add_worktree.side_effect = WorktreeError(
                "Git add failed", kind=WorktreeFailureKind.COMMAND_FAILED
            )
            failed_store = SqliteManagedWorktreeStore(root / "failed" / "worktrees.db")
            failed_service = WorktreeApplicationService(
                git=failing_git,
                store=failed_store,
                managed_root=root / "failed" / "managed",
                id_factory=lambda: WorktreeId("wt-add-failure"),
            )
            _run(failed_service.initialize())
            with self.assertRaises(WorktreeError):
                _run(failed_service.create(WorktreeCreateRequest(repository, head)))
            failed_snapshot = _run(failed_store.get("wt-add-failure"))
            self.assertIsNotNone(failed_snapshot)
            assert failed_snapshot is not None
            self.assertEqual(failed_snapshot.state, WorktreeState.FAILED)

            target = root / "remove-target"
            clean_record = GitWorktreeRecord(target, head, detached=True)
            clean_status = WorktreeStatus(target, head, detached=True)
            remove_git = new_git()
            remove_git.list_worktrees = AsyncMock(return_value=(clean_record,))
            remove_git.inspect_status = AsyncMock(return_value=clean_status)
            remove_git.remove_worktree.side_effect = WorktreeError(
                "Git remove timed out", kind=WorktreeFailureKind.TIMEOUT
            )
            remove_store = SqliteManagedWorktreeStore(root / "remove" / "worktrees.db")
            remove_service = WorktreeApplicationService(
                git=remove_git,
                store=remove_store,
                managed_root=root / "remove" / "managed",
            )
            remove_snapshot = WorktreeSnapshot(
                worktree_id=WorktreeId("wt-remove-failure"),
                repository=identity,
                canonical_path=target,
                base_revision="HEAD",
                base_commit_sha=head,
                branch=None,
                kind=WorktreeKind.DETACHED,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.READY,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            _run(remove_store.initialize())
            _run(remove_store.save(remove_snapshot))
            remove_service._initialized = True
            with self.assertRaises(WorktreeError):
                _run(remove_service.remove(WorktreeRemoveRequest(remove_snapshot.worktree_id)))
            self.assertEqual(
                _run(remove_store.get(remove_snapshot.worktree_id.value)).state,  # type: ignore[union-attr]
                WorktreeState.FAILED,
            )

            orphan_git = new_git()
            orphan_git.list_worktrees = AsyncMock(return_value=(clean_record,))
            orphan_git.inspect_status = AsyncMock(return_value=clean_status)
            orphan_store = SqliteManagedWorktreeStore(root / "orphan" / "worktrees.db")
            orphan_service = WorktreeApplicationService(
                git=orphan_git,
                store=orphan_store,
                managed_root=root / "orphan" / "managed",
            )
            _run(orphan_store.initialize())
            _run(orphan_store.save(remove_snapshot))
            orphan_service._initialized = True
            with self.assertRaises(WorktreeError) as orphaned:
                _run(orphan_service.remove(WorktreeRemoveRequest(remove_snapshot.worktree_id)))
            self.assertEqual(orphaned.exception.kind, WorktreeFailureKind.IDENTITY_MISMATCH)
            self.assertEqual(
                _run(orphan_store.get(remove_snapshot.worktree_id.value)).state,  # type: ignore[union-attr]
                WorktreeState.ORPHANED,
            )

    def test_repository_identity_distinguishes_linked_worktree_from_common_git_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-linked-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            linked = root / "linked checkout"
            _git(repository, "worktree", "add", "--detach", str(linked), head)
            try:
                adapter = LocalGitWorktreeAdapter()
                main_identity = _run(adapter.repository_identity(repository))
                linked_identity = _run(adapter.repository_identity(linked))
                self.assertEqual(main_identity.common_dir, linked_identity.common_dir)
                self.assertNotEqual(main_identity.source_worktree, linked_identity.source_worktree)
                self.assertNotEqual(linked_identity.git_dir, linked_identity.common_dir)
                self.assertTrue((linked / ".git").is_file())
            finally:
                _git(repository, "worktree", "remove", "--", str(linked))

    def test_real_lifecycle_preserves_dirty_source_and_isolates_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-lifecycle-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            (repository / "tracked.txt").write_bytes(b"dirty source\n")
            source_before = (repository / "tracked.txt").read_bytes()
            service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(root / "state" / "worktrees.db"),
                managed_root=root / "state" / "worktrees",
                id_factory=lambda: WorktreeId("wt-detached"),
            )
            _run(service.initialize())
            detached = _run(service.create(WorktreeCreateRequest(repository, head)))
            self.assertEqual(detached.state, WorktreeState.READY)
            self.assertEqual(
                (detached.canonical_path / "tracked.txt").read_text(encoding="utf-8"),
                "committed\n",
            )
            self.assertEqual((repository / "tracked.txt").read_bytes(), source_before)
            binding = _run(service.workspace_binding(detached.worktree_id.value))
            self.assertEqual(binding.primary_root, detached.canonical_path)
            self.assertEqual(binding.additional_roots, ())
            plan = resolve_filesystem_access_targets(
                "read_file",
                binding.primary_root,
                (
                    FilesystemTargetRequest(
                        "tracked.txt",
                        FilesystemAccessOperation.READ,
                        must_exist=True,
                    ),
                ),
            )
            self.assertEqual(plan.target_at(0).canonical_path.read_bytes(), b"committed\n")
            self.assertFalse(
                FilesystemWorkspaceIdentity().matches(repository, binding.primary_root)
            )

            (detached.canonical_path / "unintegrated.txt").write_text(
                "worker result\n", encoding="utf-8"
            )
            with self.assertRaises(WorktreeError) as dirty:
                _run(service.remove(WorktreeRemoveRequest(detached.worktree_id)))
            self.assertEqual(dirty.exception.kind, WorktreeFailureKind.DIRTY)
            (detached.canonical_path / "unintegrated.txt").unlink()
            removed = _run(service.remove(WorktreeRemoveRequest(detached.worktree_id)))
            self.assertEqual(removed.state, WorktreeState.REMOVED)
            self.assertEqual((repository / "tracked.txt").read_bytes(), source_before)

            branch_service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(root / "branch-state" / "worktrees.db"),
                managed_root=root / "branch-state" / "worktrees",
                id_factory=lambda: WorktreeId("wt-branch"),
            )
            _run(branch_service.initialize())
            branch = _run(
                branch_service.create(
                    WorktreeCreateRequest(repository, head, kind=WorktreeKind.MANAGED_BRANCH)
                )
            )
            self.assertEqual(branch.branch, "neuro/worktree/wt-branch")
            with self.assertRaises(WorktreeError) as collision:
                _run(
                    branch_service.create(
                        WorktreeCreateRequest(
                            repository,
                            head,
                            kind=WorktreeKind.MANAGED_BRANCH,
                            worktree_id=WorktreeId("wt-branch-2"),
                            branch="neuro/worktree/wt-branch",
                        )
                    )
                )
            self.assertEqual(collision.exception.kind, WorktreeFailureKind.BRANCH_CONFLICT)
            _run(branch_service.remove(WorktreeRemoveRequest(branch.worktree_id)))
            self.assertEqual(
                _git(
                    repository,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    "refs/heads/neuro/worktree/wt-branch",
                    check=False,
                ),
                "",
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/heads/neuro/worktree/wt-branch",
                    ],
                    cwd=repository,
                    check=False,
                ).returncode,
                0,
            )

            overlap_service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(repository / ".neuro-state" / "worktrees.db"),
                managed_root=repository / ".neuro-state" / "worktrees",
            )
            _run(overlap_service.initialize())
            with self.assertRaises(WorktreeError) as overlap:
                _run(overlap_service.create(WorktreeCreateRequest(repository, head)))
            self.assertEqual(overlap.exception.kind, WorktreeFailureKind.PATH_CONFLICT)

    def test_locked_worktree_refuses_cleanup_and_non_git_is_typed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-locked-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=SqliteManagedWorktreeStore(root / "state" / "worktrees.db"),
                managed_root=root / "state" / "worktrees",
                id_factory=lambda: WorktreeId("wt-locked"),
            )
            _run(service.initialize())
            snapshot = _run(service.create(WorktreeCreateRequest(repository, head)))
            _git(
                repository,
                "worktree",
                "lock",
                "--reason",
                "test lock",
                str(snapshot.canonical_path),
            )
            try:
                with self.assertRaises(WorktreeError) as locked:
                    _run(service.remove(WorktreeRemoveRequest(snapshot.worktree_id)))
                self.assertEqual(locked.exception.kind, WorktreeFailureKind.LOCKED)
            finally:
                _git(repository, "worktree", "unlock", str(snapshot.canonical_path))
            _run(service.remove(WorktreeRemoveRequest(snapshot.worktree_id)))

            with tempfile.TemporaryDirectory(prefix="neuro-not-repo-") as non_repo_raw:
                with self.assertRaises(WorktreeError) as not_repo:
                    _run(LocalGitWorktreeAdapter().repository_identity(Path(non_repo_raw)))
                self.assertEqual(not_repo.exception.kind, WorktreeFailureKind.NOT_REPOSITORY)
            with self.assertRaises(WorktreeError) as invalid_revision:
                _run(LocalGitWorktreeAdapter().resolve_commit(repository, "--help"))
            self.assertEqual(invalid_revision.exception.kind, WorktreeFailureKind.INVALID_REVISION)
            with self.assertRaises(WorktreeError) as invalid_branch:
                _run(LocalGitWorktreeAdapter().validate_branch(repository, "-f"))
            self.assertEqual(invalid_branch.exception.kind, WorktreeFailureKind.INVALID_REF)
            with self.assertRaises(WorktreeError) as control_branch:
                _run(LocalGitWorktreeAdapter().validate_branch(repository, "feature\nname"))
            self.assertEqual(control_branch.exception.kind, WorktreeFailureKind.INVALID_REF)
            with self.assertRaises(WorktreeError) as unsafe_branch:
                _run(LocalGitWorktreeAdapter().validate_branch(repository, "feature..name"))
            self.assertEqual(unsafe_branch.exception.kind, WorktreeFailureKind.INVALID_REF)
            with self.assertRaises(WorktreeError) as missing_revision:
                _run(
                    LocalGitWorktreeAdapter().resolve_commit(repository, "revision-that-is-missing")
                )
            self.assertEqual(missing_revision.exception.kind, WorktreeFailureKind.INVALID_REVISION)
            with self.assertRaises(WorktreeError) as relative_target:
                _run(
                    LocalGitWorktreeAdapter().add_worktree(
                        repository,
                        Path("relative-target"),
                        head,
                        branch=None,
                    )
                )
            self.assertEqual(relative_target.exception.kind, WorktreeFailureKind.PATH_CONFLICT)
            with self.assertRaises(WorktreeError) as relative_remove:
                _run(LocalGitWorktreeAdapter().remove_worktree(repository, Path("relative-target")))
            self.assertEqual(relative_remove.exception.kind, WorktreeFailureKind.PATH_CONFLICT)
            with tempfile.TemporaryDirectory(prefix="neuro-missing-git-dir-") as missing_raw:
                missing_path = Path(missing_raw) / "missing"
                with self.assertRaises(WorktreeError) as missing_repository:
                    _run(LocalGitWorktreeAdapter().repository_identity(missing_path))
                self.assertEqual(
                    missing_repository.exception.kind,
                    WorktreeFailureKind.REPOSITORY_MISSING,
                )

    def test_reconcile_marks_reused_path_and_real_process_death_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-worktree-reconcile-") as raw:
            root = Path(raw)
            repository, head = _new_repository(root)
            adapter = LocalGitWorktreeAdapter()
            store = SqliteManagedWorktreeStore(root / "state" / "worktrees.db")
            service = WorktreeApplicationService(
                git=adapter,
                store=store,
                managed_root=root / "state" / "worktrees",
            )
            _run(service.initialize())
            identity = _run(adapter.repository_identity(repository))
            reused_id = WorktreeId("wt-reused")
            reused_path = root / "state" / "worktrees" / identity.repository_id / reused_id.value
            reused_path.mkdir(parents=True)
            reused_snapshot = WorktreeSnapshot(
                worktree_id=reused_id,
                repository=identity,
                canonical_path=reused_path,
                base_revision=head,
                base_commit_sha=head,
                branch=None,
                kind=WorktreeKind.DETACHED,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.CREATING,
                created_at=datetime.now(UTC),
            )
            _run(store.save(reused_snapshot))
            reconciled = _run(service.reconcile_managed_worktrees(worktree_id=reused_id))
            self.assertEqual(reconciled[0].state, WorktreeState.ORPHANED)

            crash_id = WorktreeId("wt-crash")
            crash_path = root / "state" / "worktrees" / identity.repository_id / crash_id.value
            intent = WorktreeSnapshot(
                worktree_id=crash_id,
                repository=identity,
                canonical_path=crash_path,
                base_revision=head,
                base_commit_sha=head,
                branch=None,
                kind=WorktreeKind.DETACHED,
                ownership=WorktreeOwnership.MANAGED,
                state=WorktreeState.CREATING,
                created_at=datetime.now(UTC),
            )
            _run(store.save(intent))
            child = multiprocessing.Process(
                target=_crash_child_add,
                args=(str(repository), str(crash_path), head),
            )
            child.start()
            child.join(timeout=30)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)
            result = _run(service.reconcile_managed_worktrees(worktree_id=crash_id))
            self.assertEqual(result[0].state, WorktreeState.READY)
            _run(service.remove(WorktreeRemoveRequest(crash_id)))

            removing = _run(service.create(WorktreeCreateRequest(repository, head)))
            _run(store.save(replace(removing, state=WorktreeState.REMOVING, status=None)))
            child = multiprocessing.Process(
                target=_crash_child_remove,
                args=(str(repository), str(removing.canonical_path)),
            )
            child.start()
            child.join(timeout=30)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)

            reopened_store = SqliteManagedWorktreeStore(root / "state" / "worktrees.db")
            reopened_service = WorktreeApplicationService(
                git=LocalGitWorktreeAdapter(),
                store=reopened_store,
                managed_root=root / "state" / "worktrees",
            )
            _run(reopened_service.initialize())
            remove_result = _run(
                reopened_service.reconcile_managed_worktrees(
                    worktree_id=removing.worktree_id,
                )
            )
            self.assertEqual(remove_result[0].state, WorktreeState.REMOVED)
            self.assertFalse(removing.canonical_path.exists())


if __name__ == "__main__":
    unittest.main()
