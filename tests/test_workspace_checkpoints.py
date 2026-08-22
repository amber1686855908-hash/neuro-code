from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.checkpoints import WorkspaceCheckpointApplicationService
from neuro_code.application.checkpoints.service import _owner_is_alive
from neuro_code.application.ports.checkpoints import (
    MAX_CHECKPOINT_MANIFEST_BYTES,
    MAX_CHECKPOINT_SINGLE_FILE_BYTES,
    CheckpointFailureKind,
    WorkspaceCheckpointError,
)
from neuro_code.application.ports.worktree import (
    MINIMUM_GIT_VERSION,
    WorktreeError,
    WorktreeFailureKind,
)
from neuro_code.application.worktrees import WorktreeApplicationService
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointFingerprint,
    CheckpointId,
    CheckpointState,
    RollbackAttempt,
    RollbackAttemptId,
    RollbackState,
    WorkspaceCheckpoint,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.worktree import WorktreeCreateRequest, WorktreeId
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.persistence.checkpoint_artifacts import (
    LocalCheckpointArtifactStore,
)
from neuro_code.infrastructure.persistence.managed_worktrees import SqliteManagedWorktreeStore
from neuro_code.infrastructure.persistence.workspace_checkpoints import (
    SqliteWorkspaceCheckpointStore,
)
from neuro_code.infrastructure.workspace.checkpoints import (
    LocalWorkspaceStateAdapter,
    _assert_root,
    _ensure_parent_directories,
    _entry_size,
    _expected_kind,
    _nested_repository_marker,
    _read_regular,
    _remove_leaf,
    _safe_relative,
    _safe_target,
)


def _git(cwd: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def _crash_child_start_attempt(database: str, checkpoint_id: str, worktree_id: str) -> None:
    async def start() -> None:
        store = SqliteWorkspaceCheckpointStore(Path(database))
        await store.initialize()
        checkpoint = await store.get(CheckpointId(checkpoint_id))
        assert checkpoint is not None
        await store.start_attempt(
            RollbackAttempt(
                attempt_id=RollbackAttemptId("rb-crash-child"),
                checkpoint_id=checkpoint.checkpoint_id,
                worktree_id=WorktreeId(worktree_id),
                state=RollbackState.STARTED,
                started_at=datetime.now(UTC),
                completed_at=None,
                expected_fingerprint=checkpoint.source_fingerprint,
                owner_pid=os.getpid(),
                owner_token="crash-child-owner",
            )
        )

    try:
        asyncio.run(start())
    except BaseException:
        os._exit(1)
    os._exit(0)


class _CheckpointFixture:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="neuro-checkpoint-")
        self.root = Path(self.directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _git(self.repository, "init", "-q")
        _git(self.repository, "config", "user.email", "neuro-checkpoint@example.invalid")
        _git(self.repository, "config", "user.name", "Neuro Checkpoint Tests")
        _git(self.repository, "config", "core.autocrlf", "false")
        _git(self.repository, "config", "core.eol", "lf")
        (self.repository / "tracked.txt").write_bytes(b"base\n")
        (self.repository / "binary.bin").write_bytes(b"\x00\x01\xff\x00binary")
        (self.repository / "deleted.txt").write_bytes(b"delete me\n")
        (self.repository / "script.sh").write_text("#!/bin/sh\nprintf base\n", encoding="utf-8")
        (self.repository / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        _git(self.repository, "add", ".")
        _git(self.repository, "commit", "-qm", "initial")
        self.head = _git(self.repository, "rev-parse", "HEAD")
        self.state = self.root / "state"
        self.adapter = LocalGitWorktreeAdapter(hooks_directory=self.state / "hooks")
        self.worktree_store = SqliteManagedWorktreeStore(self.state / "worktrees.db")
        self.worktrees = WorktreeApplicationService(
            git=self.adapter,
            store=self.worktree_store,
            managed_root=self.state / "worktrees",
            id_factory=lambda: WorktreeId("wt-checkpoint"),
        )
        _run(self.worktrees.initialize())
        self.snapshot = _run(
            self.worktrees.create(
                WorktreeCreateRequest(
                    self.repository,
                    self.head,
                    worktree_id=WorktreeId("wt-checkpoint"),
                )
            )
        )
        self.target = self.snapshot.canonical_path
        self.checkpoint_store = SqliteWorkspaceCheckpointStore(self.state / "checkpoints.db")
        self.artifacts = LocalCheckpointArtifactStore(self.state)
        self.state_adapter = LocalWorkspaceStateAdapter(
            git=self.adapter,
            workspace_git=self.adapter,
        )
        self.checkpoints = WorkspaceCheckpointApplicationService(
            git=self.adapter,
            workspace_git=self.adapter,
            worktrees=self.worktree_store,
            state=self.state_adapter,
            checkpoints=self.checkpoint_store,
            artifacts=self.artifacts,
        )
        _run(self.checkpoints.initialize())

    def close(self) -> None:
        self.directory.cleanup()


class WorkspaceCheckpointDomainTests(unittest.TestCase):
    def test_projection_fingerprint_changes_for_content_mode_and_index(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        first = workspace_projection_fingerprint(fixture.snapshot.handle, projection)
        self.assertEqual(
            first, workspace_projection_fingerprint(fixture.snapshot.handle, projection)
        )
        changed = WorkspaceProjection(
            head_sha=projection.head_sha,
            branch=projection.branch,
            detached=projection.detached,
            index_bytes=projection.index_bytes + b"x",
            entries=projection.entries,
        )
        self.assertNotEqual(
            first, workspace_projection_fingerprint(fixture.snapshot.handle, changed)
        )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="../escape",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.REGULAR,
                mode=0o100644,
                content=b"x",
            )
        with self.assertRaises(ValueError):
            CheckpointFingerprint("not-a-sha")

    def test_checkpoint_request_never_accepts_a_raw_path(self) -> None:
        with self.assertRaises(TypeError):
            CheckpointCreateRequest("/tmp/raw-path")  # type: ignore[arg-type]


class WorkspaceCheckpointIntegrationTests(unittest.TestCase):
    def test_capture_and_rollback_preserve_index_modes_binary_symlinks_and_ignored_files(
        self,
    ) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        target = fixture.target
        (target / "tracked.txt").write_bytes(b"staged\n")
        _git(target, "add", "tracked.txt")
        (target / "tracked.txt").write_bytes(b"staged-and-unstaged\n")
        (target / "deleted.txt").unlink()
        (target / "untracked.bin").write_bytes(b"\x00\xff\x01")
        ignored = target / "ignored.tmp"
        ignored.write_bytes(b"ignored-A")
        script = target / "script.sh"
        script.chmod(0o755)
        symlink = target / "untracked-link"
        try:
            symlink.symlink_to("tracked.txt")
        except OSError as error:
            self.skipTest(f"symlink support required: {error}")

        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        self.assertIs(checkpoint.state, CheckpointState.READY)
        self.assertGreater(checkpoint.artifact_bytes, 0)

        (target / "tracked.txt").write_bytes(b"after\n")
        _git(target, "add", "tracked.txt")
        (target / "untracked.bin").unlink()
        symlink.unlink()
        (target / "new-after-checkpoint.txt").write_text("remove me", encoding="utf-8")
        ignored.write_bytes(b"ignored-B")
        script.chmod(0o644)
        (target / "deleted.txt").write_bytes(b"recreated")

        result = _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertIs(result.state, RollbackState.COMPLETED)
        self.assertEqual((target / "tracked.txt").read_bytes(), b"staged-and-unstaged\n")
        self.assertEqual((target / "untracked.bin").read_bytes(), b"\x00\xff\x01")
        self.assertFalse((target / "deleted.txt").exists())
        self.assertFalse((target / "new-after-checkpoint.txt").exists())
        self.assertEqual(ignored.read_bytes(), b"ignored-B")
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(os.readlink(symlink), "tracked.txt")
        self.assertIn("staged", _git(target, "diff", "--cached", "--", "tracked.txt"))

    def test_multiple_immutable_checkpoints_restore_in_reverse_order(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        target = fixture.target
        checkpoint_a = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        (target / "tracked.txt").write_bytes(b"B\n")
        checkpoint_b = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        (target / "tracked.txt").write_bytes(b"C\n")
        result_b = _run(fixture.checkpoints.rollback(checkpoint_b.checkpoint_id))
        self.assertIs(result_b.state, RollbackState.COMPLETED)
        self.assertEqual((target / "tracked.txt").read_bytes(), b"B\n")
        result_a = _run(fixture.checkpoints.rollback(checkpoint_a.checkpoint_id))
        self.assertIs(result_a.state, RollbackState.COMPLETED)
        self.assertEqual((target / "tracked.txt").read_bytes(), b"base\n")
        stored_a = _run(fixture.checkpoint_store.get(checkpoint_a.checkpoint_id))
        stored_b = _run(fixture.checkpoint_store.get(checkpoint_b.checkpoint_id))
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a.source_fingerprint, checkpoint_a.source_fingerprint)
        self.assertEqual(stored_b.source_fingerprint, checkpoint_b.source_fingerprint)

    def test_source_checkout_is_unchanged(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        source_head = _git(fixture.repository, "rev-parse", "HEAD")
        source_bytes = (fixture.repository / "tracked.txt").read_bytes()
        source_status = _git(fixture.repository, "status", "--porcelain=v2", "-z")
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        (fixture.target / "tracked.txt").write_bytes(b"managed-only\n")
        _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertEqual(_git(fixture.repository, "rev-parse", "HEAD"), source_head)
        self.assertEqual((fixture.repository / "tracked.txt").read_bytes(), source_bytes)
        self.assertEqual(_git(fixture.repository, "status", "--porcelain=v2", "-z"), source_status)

    def test_repeated_rollback_is_a_verified_no_op(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        first = _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        second = _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertIs(first.state, RollbackState.COMPLETED)
        self.assertIs(second.state, RollbackState.COMPLETED)
        self.assertEqual(second.observed_fingerprint, checkpoint.source_fingerprint)


class WorkspaceCheckpointFailureTests(unittest.TestCase):
    def test_head_mismatch_fails_before_starting_a_destructive_attempt(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        (fixture.target / "head-moved.txt").write_text("move", encoding="utf-8")
        _git(fixture.target, "add", "head-moved.txt")
        _git(fixture.target, "commit", "-qm", "move head")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.HEAD_MISMATCH)
        self.assertIsNone(
            _run(fixture.checkpoint_store.active_attempt(fixture.snapshot.worktree_id.value))
        )

    def test_external_git_lock_fails_closed_and_is_not_unlocked(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        _git(
            fixture.target, "worktree", "lock", "--reason=external-owner", "--", str(fixture.target)
        )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.LOCKED)
        records = _run(fixture.adapter.list_worktrees(fixture.repository))
        record = next(item for item in records if item.path == fixture.target)
        self.assertTrue(record.locked)
        self.assertEqual(record.lock_reason, "external-owner")
        _git(fixture.repository, "worktree", "unlock", "--", str(fixture.target))

    def test_intent_to_add_is_unsupported(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        (fixture.target / "intent.txt").write_text("intent", encoding="utf-8")
        _git(fixture.target, "add", "-N", "intent.txt")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle)))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE)
        self.assertEqual(
            _git(fixture.target, "status", "--porcelain=v2", "-z").count("intent.txt"), 1
        )

    def test_sparse_checkout_is_unsupported(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        _git(fixture.target, "config", "core.sparseCheckout", "true")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle)))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE)

    def test_corrupt_artifact_is_rejected_before_worktree_mutation(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        before = (fixture.target / "tracked.txt").read_bytes()
        manifest = checkpoint.artifact_path / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"corrupt")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)
        self.assertEqual((fixture.target / "tracked.txt").read_bytes(), before)

    def test_same_explicit_checkpoint_id_is_insert_only(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint_id = CheckpointId("cp-explicit")
        request = CheckpointCreateRequest(fixture.snapshot.handle, checkpoint_id)
        first = _run(fixture.checkpoints.create(request))
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.create(request))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CONCURRENT_MODIFICATION)
        self.assertEqual(first.checkpoint_id, checkpoint_id)


class WorkspaceCheckpointPersistenceTests(unittest.TestCase):
    def test_capturing_intent_with_published_artifact_recovers_to_ready(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        checkpoint_id = CheckpointId("cp-capturing-recovery")
        intent = WorkspaceCheckpoint(
            checkpoint_id=checkpoint_id,
            worktree_id=fixture.snapshot.worktree_id,
            repository=fixture.snapshot.repository,
            canonical_path=fixture.snapshot.canonical_path,
            head_sha=projection.head_sha,
            branch=projection.branch,
            detached=projection.detached,
            created_at=datetime.now(UTC),
            source_fingerprint=workspace_projection_fingerprint(
                fixture.snapshot.handle,
                projection,
            ),
            artifact_path=fixture.artifacts.path_for(checkpoint_id),
            artifact_sha256="0" * 64,
            artifact_bytes=0,
            artifact_file_count=0,
            state=CheckpointState.CAPTURING,
        )
        _run(fixture.checkpoint_store.insert_capturing(intent))
        _run(fixture.artifacts.publish(intent, projection))
        _run(fixture.checkpoints.reconcile())
        recovered = _run(fixture.checkpoint_store.get(checkpoint_id))
        assert recovered is not None
        self.assertIs(recovered.state, CheckpointState.READY)
        self.assertGreater(recovered.artifact_bytes, 0)

    def test_child_process_death_leaves_started_attempt_for_reconciliation(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        (fixture.target / "tracked.txt").write_bytes(b"changed before crash\n")
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_child_start_attempt,
            args=(
                str(fixture.checkpoint_store.database_path),
                checkpoint.checkpoint_id.value,
                fixture.snapshot.worktree_id.value,
            ),
        )
        process.start()
        process.join(timeout=30)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)
        results = _run(fixture.checkpoints.reconcile())
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].state, RollbackState.COMPLETED)
        self.assertEqual((fixture.target / "tracked.txt").read_bytes(), b"base\n")

    def test_malformed_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-checkpoint-schema-") as directory:
            path = Path(directory) / "checkpoints.db"
            store = SqliteWorkspaceCheckpointStore(path)
            _run(store.initialize())
            import sqlite3

            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE schema_meta SET version = 999 WHERE singleton = 1")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(WorkspaceCheckpointError) as raised:
                _run(SqliteWorkspaceCheckpointStore(path).initialize())
            self.assertEqual(raised.exception.kind, CheckpointFailureKind.PROTOCOL)

    def test_artifact_metadata_has_no_absolute_internal_paths(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        manifest = json.loads((checkpoint.artifact_path / "manifest.json").read_text())
        rendered = json.dumps(manifest)
        self.assertNotIn(str(fixture.target), rendered)
        self.assertNotIn(str(fixture.repository), rendered)
        for entry in manifest["projection"]["entries"]:
            self.assertFalse(Path(entry["path"]).is_absolute())


class WorkspaceCheckpointBoundaryTests(unittest.TestCase):
    def test_domain_values_reject_invalid_shapes(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        invalid_ids = ("", "checkpoint", "cp-", "cp-UPPER", "cp-a/b")
        for value in invalid_ids:
            with self.subTest(value=value), self.assertRaises(ValueError):
                CheckpointId(value)
        for value in ("", "attempt", "rb-", "rb-UPPER", "rb-a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RollbackAttemptId(value)
        checkpoint_id = CheckpointId.new()
        self.assertEqual(str(checkpoint_id), checkpoint_id.value)
        self.assertIn("CheckpointId", repr(checkpoint_id))
        self.assertEqual(hash(checkpoint_id), hash(CheckpointId(checkpoint_id.value)))
        self.assertNotEqual(checkpoint_id, object())
        attempt_id = RollbackAttemptId.new()
        self.assertEqual(str(attempt_id), attempt_id.value)
        self.assertIn("RollbackAttemptId", repr(attempt_id))
        self.assertEqual(hash(attempt_id), hash(RollbackAttemptId(attempt_id.value)))
        self.assertNotEqual(attempt_id, object())
        self.assertEqual(CheckpointFingerprint("A" * 64).value, "a" * 64)
        with self.assertRaises(ValueError):
            CheckpointId("cp-" + "a" * 126)
        with self.assertRaises(ValueError):
            CheckpointFingerprint("a" * 63)

        for path in ("", "/absolute", "../escape", "a/../b", "a\\b", "C:/drive"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                WorkspaceFileEntry(
                    path=path,
                    scope=WorkspaceFileScope.UNTRACKED,
                    present=False,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                )
        with self.assertRaises(TypeError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope="untracked",  # type: ignore[arg-type]
                present=True,
                kind=WorkspaceFileKind.REGULAR,
                mode=0o100644,
                content=b"x",
            )
        with self.assertRaises(TypeError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.REGULAR,
                mode=0o100644,
            )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.SYMLINK,
                mode=0o120000,
                link_target="x\x00y",
            )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=False,
                kind=WorkspaceFileKind.REGULAR,
                mode=0o100644,
                content=b"not absent",
            )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.REGULAR,
                mode=0o100644,
                content=b"x",
                link_target="not allowed",
            )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.SYMLINK,
                mode=0o120000,
                content=b"not allowed",
                link_target="target",
            )
        with self.assertRaises(ValueError):
            WorkspaceFileEntry(
                path="typed.txt",
                scope=WorkspaceFileScope.UNTRACKED,
                present=True,
                kind=WorkspaceFileKind.SYMLINK,
                mode=0o120000,
                link_target="x" * 32_769,
            )
        for invalid_mode in (True, -1, 0o200000):
            with self.subTest(invalid_mode=invalid_mode), self.assertRaises(ValueError):
                WorkspaceFileEntry(
                    path="typed.txt",
                    scope=WorkspaceFileScope.UNTRACKED,
                    present=False,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=invalid_mode,
                )

        entry_a = WorkspaceFileEntry(
            path="a.txt",
            scope=WorkspaceFileScope.TRACKED,
            present=True,
            kind=WorkspaceFileKind.REGULAR,
            mode=0o100644,
            content=b"a",
        )
        entry_b = WorkspaceFileEntry(
            path="b.txt",
            scope=WorkspaceFileScope.UNTRACKED,
            present=False,
            kind=WorkspaceFileKind.REGULAR,
            mode=0o100644,
        )
        with self.assertRaises(ValueError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached=True,
                index_bytes=b"",
                entries=(entry_a, entry_b),
            )
        with self.assertRaises(ValueError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch=None,
                detached=False,
                index_bytes=b"",
                entries=(entry_a, entry_b),
            )
        with self.assertRaises(ValueError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached=False,
                index_bytes=b"",
                entries=(entry_b, entry_a),
            )
        with self.assertRaises(ValueError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached=False,
                index_bytes=b"",
                entries=(entry_a, entry_a),
            )
        with self.assertRaises(ValueError):
            WorkspaceProjection(
                head_sha="",
                branch="main",
                detached=False,
                index_bytes=b"",
                entries=(),
            )
        with self.assertRaises(TypeError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch=123,  # type: ignore[arg-type]
                detached=False,
                index_bytes=b"",
                entries=(),
            )
        with self.assertRaises(TypeError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached="no",  # type: ignore[arg-type]
                index_bytes=b"",
                entries=(),
            )
        with self.assertRaises(TypeError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached=False,
                index_bytes="raw",  # type: ignore[arg-type]
                entries=(),
            )
        with self.assertRaises(TypeError):
            WorkspaceProjection(
                head_sha=fixture.head,
                branch="main",
                detached=False,
                index_bytes=b"",
                entries=("raw",),  # type: ignore[arg-type]
            )

        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        with self.assertRaises(ValueError):
            replace(checkpoint, detached=True, branch="main")
        with self.assertRaises(ValueError):
            replace(checkpoint, created_at=datetime.now(UTC).replace(tzinfo=None))
        with self.assertRaises(ValueError):
            replace(checkpoint, artifact_sha256="not-a-digest")
        with self.assertRaises(ValueError):
            replace(checkpoint, artifact_bytes=-1)
        with self.assertRaises(TypeError):
            replace(checkpoint, state="ready")  # type: ignore[arg-type]

        attempt = RollbackAttempt(
            attempt_id=RollbackAttemptId("rb-domain"),
            checkpoint_id=checkpoint.checkpoint_id,
            worktree_id=checkpoint.worktree_id,
            state=RollbackState.STARTED,
            started_at=datetime.now(UTC),
            completed_at=None,
            expected_fingerprint=checkpoint.source_fingerprint,
        )
        with self.assertRaises(ValueError):
            replace(attempt, owner_pid=0)
        with self.assertRaises(ValueError):
            replace(attempt, owner_token="")
        with self.assertRaises(ValueError):
            replace(attempt, started_at=datetime.now(UTC).replace(tzinfo=None))
        with self.assertRaises(ValueError):
            replace(attempt, version=-1)

    def test_git_projection_helpers_are_strict_and_bounded(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="neuro-checkpoint-helpers-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.assertEqual(_safe_relative("nested/file.txt"), "nested/file.txt")
        for value in ("", "../x", "/x", "a\\b", "C:/x", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(WorkspaceCheckpointError):
                _safe_relative(value)
        with self.assertRaises(WorkspaceCheckpointError):
            _safe_target(root, "../escape")
        self.assertEqual(_expected_kind(0o100644), WorkspaceFileKind.REGULAR)
        self.assertEqual(_expected_kind(0o100755), WorkspaceFileKind.REGULAR)
        self.assertEqual(_expected_kind(0o120000), WorkspaceFileKind.SYMLINK)
        with self.assertRaises(WorkspaceCheckpointError):
            _expected_kind(0o160000)

        object_id = "1" * 40
        valid = f"100644 {object_id} 0\ttracked.txt\0".encode()
        self.assertEqual(
            LocalWorkspaceStateAdapter._parse_index_records(valid), {"tracked.txt": 0o100644}
        )
        self.assertEqual(LocalWorkspaceStateAdapter._parse_index_records(b""), {})
        self.assertEqual(
            LocalWorkspaceStateAdapter._parse_untracked_records(b"a.txt\0a.txt\0b.txt\0"),
            ("a.txt", "b.txt"),
        )
        self.assertFalse(
            LocalWorkspaceStateAdapter._has_intent_to_add(b"# branch.oid abc\0? untracked.txt\0")
        )
        self.assertTrue(
            LocalWorkspaceStateAdapter._has_intent_to_add(b"1 .A 100644 0000 0000\tintent.txt\0")
        )
        for raw in (
            b"malformed\0",
            f"100644 {object_id} 1\tmerge.txt\0".encode(),
            f"100644 {'0' * 40} 0\tintent.txt\0".encode(),
            f"160000 {object_id} 0\tmodule\0".encode(),
            valid + valid,
        ):
            with self.subTest(raw=raw), self.assertRaises(WorkspaceCheckpointError):
                LocalWorkspaceStateAdapter._parse_index_records(raw)
        with self.assertRaises(WorkspaceCheckpointError):
            LocalWorkspaceStateAdapter._parse_untracked_records(b"../escape\0")

        regular = root / "regular.txt"
        regular.write_bytes(b"content")
        self.assertEqual(_read_regular(regular, expected_size=7), b"content")
        with self.assertRaises(WorkspaceCheckpointError):
            _read_regular(regular, expected_size=6)
        with self.assertRaises(WorkspaceCheckpointError):
            _read_regular(root, expected_size=0)
        oversized = root / "oversized.bin"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_CHECKPOINT_SINGLE_FILE_BYTES + 1)
        with self.assertRaises(WorkspaceCheckpointError):
            _read_regular(oversized)

        nested = root / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        self.assertTrue(_nested_repository_marker(root, nested / "file.txt"))
        self.assertEqual(
            _entry_size(
                WorkspaceFileEntry(
                    path="a.txt",
                    scope=WorkspaceFileScope.TRACKED,
                    present=True,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                    content=b"abc",
                )
            ),
            3,
        )
        self.assertEqual(
            _entry_size(
                WorkspaceFileEntry(
                    path="link",
                    scope=WorkspaceFileScope.UNTRACKED,
                    present=True,
                    kind=WorkspaceFileKind.SYMLINK,
                    mode=0o120000,
                    link_target="target",
                )
            ),
            6,
        )
        self.assertEqual(
            _entry_size(
                WorkspaceFileEntry(
                    path="absent",
                    scope=WorkspaceFileScope.TRACKED,
                    present=False,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                )
            ),
            0,
        )

    def test_store_filters_and_compare_and_swap_guards(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(
                CheckpointCreateRequest(fixture.snapshot.handle, CheckpointId("cp-store"))
            )
        )
        self.assertEqual(len(_run(fixture.checkpoint_store.list(worktree_id="wt-checkpoint"))), 1)
        failed = _run(
            fixture.checkpoint_store.compare_and_transition_checkpoint(
                replace(checkpoint, state=CheckpointState.FAILED),
                expected_version=checkpoint.version,
                expected_state=CheckpointState.READY,
            )
        )
        self.assertIs(failed.state, CheckpointState.FAILED)
        self.assertEqual(_run(fixture.checkpoint_store.list()), ())
        self.assertEqual(len(_run(fixture.checkpoint_store.list(include_failed=True))), 1)
        with self.assertRaises(WorkspaceCheckpointError):
            _run(
                fixture.checkpoint_store.compare_and_transition_checkpoint(
                    failed,
                    expected_version=checkpoint.version,
                    expected_state=CheckpointState.FAILED,
                )
            )
        with self.assertRaises(TypeError):
            _run(fixture.checkpoint_store.get("cp-store"))  # type: ignore[arg-type]

        attempt = RollbackAttempt(
            attempt_id=RollbackAttemptId("rb-store"),
            checkpoint_id=checkpoint.checkpoint_id,
            worktree_id=checkpoint.worktree_id,
            state=RollbackState.STARTED,
            started_at=datetime.now(UTC),
            completed_at=None,
            expected_fingerprint=checkpoint.source_fingerprint,
            owner_pid=os.getpid(),
            owner_token="store-owner",
        )
        stored = _run(fixture.checkpoint_store.start_attempt(attempt))
        self.assertEqual(_run(fixture.checkpoint_store.active_attempt("wt-checkpoint")), stored)
        self.assertEqual(_run(fixture.checkpoint_store.list_active_attempts()), (stored,))
        indeterminate = _run(
            fixture.checkpoint_store.compare_and_transition_attempt(
                replace(stored, state=RollbackState.INDETERMINATE),
                expected_version=stored.version,
                expected_state=RollbackState.STARTED,
            )
        )
        completed = _run(
            fixture.checkpoint_store.compare_and_transition_attempt(
                replace(
                    indeterminate, state=RollbackState.COMPLETED, completed_at=datetime.now(UTC)
                ),
                expected_version=indeterminate.version,
                expected_state=RollbackState.INDETERMINATE,
            )
        )
        self.assertIs(completed.state, RollbackState.COMPLETED)
        self.assertIsNone(_run(fixture.checkpoint_store.active_attempt("wt-checkpoint")))
        with self.assertRaises(WorkspaceCheckpointError):
            _run(
                fixture.checkpoint_store.compare_and_transition_attempt(
                    completed,
                    expected_version=stored.version,
                    expected_state=RollbackState.COMPLETED,
                )
            )
        with self.assertRaises(TypeError):
            _run(fixture.checkpoint_store.get_attempt("rb-store"))  # type: ignore[arg-type]

    def test_store_malformed_rows_fail_closed(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        import sqlite3

        connection = sqlite3.connect(fixture.checkpoint_store.database_path)
        try:
            connection.execute(
                "UPDATE checkpoints SET artifact_bytes = 'not-an-int' WHERE checkpoint_id = ?",
                (checkpoint.checkpoint_id.value,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoint_store.get(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.PROTOCOL)

    def test_store_input_states_and_cas_arguments_are_strict(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        with self.assertRaises(ValueError):
            _run(fixture.checkpoint_store.insert_capturing(checkpoint))
        with self.assertRaises(TypeError):
            _run(
                fixture.checkpoint_store.compare_and_transition_checkpoint(
                    checkpoint,
                    expected_version=True,  # type: ignore[arg-type]
                    expected_state=CheckpointState.READY,
                )
            )
        with self.assertRaises(TypeError):
            _run(
                fixture.checkpoint_store.compare_and_transition_checkpoint(
                    checkpoint,
                    expected_version=checkpoint.version,
                    expected_state="ready",  # type: ignore[arg-type]
                )
            )

        attempt = RollbackAttempt(
            attempt_id=RollbackAttemptId("rb-invalid-store"),
            checkpoint_id=checkpoint.checkpoint_id,
            worktree_id=checkpoint.worktree_id,
            state=RollbackState.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            expected_fingerprint=checkpoint.source_fingerprint,
        )
        with self.assertRaises(ValueError):
            _run(fixture.checkpoint_store.start_attempt(attempt))
        started = replace(attempt, state=RollbackState.STARTED, completed_at=None)
        stored = _run(fixture.checkpoint_store.start_attempt(started))
        with self.assertRaises(TypeError):
            _run(
                fixture.checkpoint_store.compare_and_transition_attempt(
                    stored,
                    expected_version=stored.version,
                    expected_state="started",  # type: ignore[arg-type]
                )
            )


class WorkspaceCheckpointArtifactBoundaryTests(unittest.TestCase):
    def test_artifact_load_rejects_metadata_and_layout_tampering(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        mutations = ("identity", "malformed", "missing_blob", "bad_integrity", "extra")
        for index, mutation in enumerate(mutations):
            checkpoint = _run(
                fixture.checkpoints.create(
                    CheckpointCreateRequest(
                        fixture.snapshot.handle,
                        CheckpointId(f"cp-tamper-{index}"),
                    )
                )
            )
            manifest_path = checkpoint.artifact_path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            if mutation == "identity":
                manifest["checkpoint_id"] = "cp-other"
                manifest_path.write_text(json.dumps(manifest))
            elif mutation == "malformed":
                manifest_path.write_bytes(b"not-json")
            elif mutation == "missing_blob":
                digest = next(
                    entry["sha256"]
                    for entry in manifest["projection"]["entries"]
                    if entry["present"] and entry["kind"] == "regular"
                )
                (checkpoint.artifact_path / "blobs" / digest).unlink()
            elif mutation == "bad_integrity":
                integrity_path = checkpoint.artifact_path / "integrity.json"
                integrity = json.loads(integrity_path.read_text())
                integrity["blobs"] = None
                integrity_path.write_text(json.dumps(integrity))
            else:
                (checkpoint.artifact_path / "unexpected").write_bytes(b"unexpected")
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(WorkspaceCheckpointError) as raised,
            ):
                _run(fixture.artifacts.load(checkpoint))
            self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)

    def test_artifact_missing_and_malformed_projection_members_fail_closed(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        mutations = (
            "missing_index",
            "large_manifest",
            "projection_type",
            "entries_type",
            "entry_type",
        )
        for index, mutation in enumerate(mutations):
            checkpoint = _run(
                fixture.checkpoints.create(
                    CheckpointCreateRequest(
                        fixture.snapshot.handle,
                        CheckpointId(f"cp-structure-{index}"),
                    )
                )
            )
            manifest_path = checkpoint.artifact_path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            if mutation == "missing_index":
                (checkpoint.artifact_path / "index").unlink()
            elif mutation == "large_manifest":
                manifest_path.write_bytes(b"x" * (MAX_CHECKPOINT_MANIFEST_BYTES + 1))
            elif mutation == "projection_type":
                manifest["projection"] = "not-an-object"
                manifest_path.write_text(json.dumps(manifest))
            elif mutation == "entries_type":
                manifest["projection"]["entries"] = "not-a-list"
                manifest_path.write_text(json.dumps(manifest))
            else:
                manifest["projection"]["entries"][0] = "not-an-entry"
                manifest_path.write_text(json.dumps(manifest))
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(WorkspaceCheckpointError) as raised,
            ):
                _run(fixture.artifacts.load(checkpoint))
            expected_kind = (
                CheckpointFailureKind.CHECKPOINT_TOO_LARGE
                if mutation == "large_manifest"
                else CheckpointFailureKind.CHECKPOINT_CORRUPT
            )
            self.assertEqual(raised.exception.kind, expected_kind)

    def test_artifact_public_ports_and_temporary_cleanup_are_typed(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        with self.assertRaises(TypeError):
            _run(fixture.artifacts.publish("raw", projection))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _run(fixture.artifacts.publish(checkpoint, "raw"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _run(fixture.artifacts.load("raw"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _run(fixture.artifacts.remove_temporary_capture("raw"))  # type: ignore[arg-type]

        temporary = fixture.artifacts.root / f".{checkpoint.checkpoint_id.value}.unsafe.tmp"
        temporary.write_text("not a directory")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.artifacts.remove_temporary_capture(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)

        integrity = json.loads((checkpoint.artifact_path / "integrity.json").read_text())
        integrity["artifact_sha256"] = "0" * 64
        (checkpoint.artifact_path / "integrity.json").write_text(json.dumps(integrity))
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.artifacts.load(checkpoint))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)

    def test_artifact_rejects_durable_metadata_mismatch_and_publish_conflicts(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(
                fixture.artifacts.load(
                    replace(checkpoint, artifact_path=fixture.state / "other-artifact")
                )
            )
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(
                fixture.artifacts.load(
                    replace(checkpoint, artifact_bytes=checkpoint.artifact_bytes + 1)
                )
            )
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_CORRUPT)
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.artifacts.publish(checkpoint, projection))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.PATH_CONFLICT)

        changed_projection = replace(projection, index_bytes=projection.index_bytes + b"changed")
        new_id = CheckpointId("cp-publish-race")
        intent = replace(
            checkpoint,
            checkpoint_id=new_id,
            state=CheckpointState.CAPTURING,
            version=0,
            artifact_path=fixture.artifacts.path_for(new_id),
            artifact_sha256="0" * 64,
            artifact_bytes=0,
            artifact_file_count=0,
        )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.artifacts.publish(intent, changed_projection))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CONCURRENT_MODIFICATION)

    def test_artifact_bounds_and_owned_state_paths_fail_closed(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        large_projection = WorkspaceProjection(
            head_sha=projection.head_sha,
            branch=projection.branch,
            detached=projection.detached,
            index_bytes=b"",
            entries=(
                WorkspaceFileEntry(
                    path="large.bin",
                    scope=WorkspaceFileScope.UNTRACKED,
                    present=True,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                    content=b"x" * (MAX_CHECKPOINT_SINGLE_FILE_BYTES + 1),
                ),
            ),
        )
        large_id = CheckpointId("cp-large")
        large_intent = replace(
            checkpoint,
            checkpoint_id=large_id,
            state=CheckpointState.CAPTURING,
            version=0,
            artifact_path=fixture.artifacts.path_for(large_id),
            artifact_sha256="0" * 64,
            artifact_bytes=0,
            artifact_file_count=0,
            source_fingerprint=workspace_projection_fingerprint(
                fixture.snapshot.handle, large_projection
            ),
        )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.artifacts.publish(large_intent, large_projection))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CHECKPOINT_TOO_LARGE)
        with self.assertRaises(TypeError):
            fixture.artifacts.path_for("cp-raw")  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory(prefix="neuro-checkpoint-path-") as directory:
            state_file = Path(directory) / "state"
            state_file.write_text("not a directory")
            with self.assertRaises(WorkspaceCheckpointError) as raised:
                _run(LocalCheckpointArtifactStore(state_file).initialize())
            self.assertEqual(raised.exception.kind, CheckpointFailureKind.PATH_CONFLICT)
            state_dir = Path(directory) / "state-dir"
            state_dir.mkdir()
            (state_dir / "checkpoints").write_text("not a directory")
            with self.assertRaises(WorkspaceCheckpointError) as raised:
                _run(LocalCheckpointArtifactStore(state_dir).initialize())
            self.assertEqual(raised.exception.kind, CheckpointFailureKind.PATH_CONFLICT)


class WorkspaceCheckpointRestoreBoundaryTests(unittest.TestCase):
    def test_restore_rejects_head_parent_and_index_conflicts(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(
                fixture.state_adapter.restore(
                    fixture.snapshot.handle,
                    replace(projection, head_sha="0" * 40),
                )
            )
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.HEAD_MISMATCH)

        (fixture.target / "new").write_text("parent is a file")
        nested_entry = WorkspaceFileEntry(
            path="new/file.txt",
            scope=WorkspaceFileScope.UNTRACKED,
            present=True,
            kind=WorkspaceFileKind.REGULAR,
            mode=0o100644,
            content=b"nested",
        )
        parent_entry = WorkspaceFileEntry(
            path="new",
            scope=WorkspaceFileScope.UNTRACKED,
            present=True,
            kind=WorkspaceFileKind.REGULAR,
            mode=0o100644,
            content=b"parent",
        )
        nested_projection = replace(
            projection,
            entries=tuple(
                sorted(
                    (*projection.entries, parent_entry, nested_entry),
                    key=lambda entry: (entry.path, entry.scope.value),
                )
            ),
        )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.state_adapter.restore(fixture.snapshot.handle, nested_projection))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE)

        (fixture.target / "new").unlink()
        with (
            patch.object(
                fixture.adapter,
                "replace_index",
                side_effect=WorktreeError(
                    "index replacement rejected", kind=WorktreeFailureKind.COMMAND_FAILED
                ),
            ),
            self.assertRaises(WorkspaceCheckpointError) as raised,
        ):
            _run(fixture.state_adapter.restore(fixture.snapshot.handle, projection))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.COMMAND_FAILED)

    def test_restore_leaf_helpers_are_exact_and_never_remove_directories(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="neuro-checkpoint-restore-"))
        self.addCleanup(shutil.rmtree, root, True)
        root.mkdir(exist_ok=True)
        nested = root / "a" / "b"
        _ensure_parent_directories(root, nested / "file.txt")
        self.assertTrue((root / "a" / "b").is_dir())
        leaf = nested / "file.txt"
        leaf.write_text("old")
        _remove_leaf(root, leaf)
        _remove_leaf(root, leaf)
        (root / "directory").mkdir()
        with self.assertRaises(WorkspaceCheckpointError):
            _remove_leaf(root, root / "directory")
        with self.assertRaises(WorkspaceCheckpointError):
            _remove_leaf(root, root)
        (root / "blocked").write_text("file")
        with self.assertRaises(WorkspaceCheckpointError):
            _ensure_parent_directories(root, root / "blocked" / "child.txt")
        with self.assertRaises(WorkspaceCheckpointError):
            _assert_root(root / "missing")


class WorkspaceCheckpointServiceBoundaryTests(unittest.TestCase):
    @staticmethod
    def _new_service(
        fixture: _CheckpointFixture, **options: object
    ) -> WorkspaceCheckpointApplicationService:
        return WorkspaceCheckpointApplicationService(
            git=fixture.adapter,
            workspace_git=fixture.adapter,
            worktrees=fixture.worktree_store,
            state=fixture.state_adapter,
            checkpoints=fixture.checkpoint_store,
            artifacts=fixture.artifacts,
            **options,
        )

    def test_service_requires_initialization_and_enforces_git_boundary(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        uninitialized = self._new_service(fixture)
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(uninitialized.reconcile())
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.FAILED_STATE)
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(uninitialized.create(CheckpointCreateRequest(fixture.snapshot.handle)))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.FAILED_STATE)

        self.assertEqual(MINIMUM_GIT_VERSION, (2, 40, 0))
        for version, expected_kind in (
            ((2, 39, 5), CheckpointFailureKind.NOT_AVAILABLE),
            ((2, 40, 0), None),
        ):
            service = self._new_service(fixture)
            with patch.object(fixture.adapter, "git_version", return_value=version):
                if expected_kind is None:
                    _run(service.initialize())
                else:
                    with self.assertRaises(WorkspaceCheckpointError) as raised:
                        _run(service.initialize())
                    self.assertEqual(raised.exception.kind, expected_kind)

    def test_service_rejects_invalid_requests_and_marks_capture_failure(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        with self.assertRaises(TypeError):
            _run(fixture.checkpoints.create("raw"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _run(fixture.checkpoints.rollback("raw"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _run(
                fixture.checkpoints.rollback(
                    CheckpointId("cp-missing"),
                    attempt_id="raw",  # type: ignore[arg-type]
                )
            )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.rollback(CheckpointId("cp-missing")))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.UNMANAGED)

        bad_factory = self._new_service(fixture, checkpoint_id_factory=lambda: "bad")
        _run(bad_factory.initialize())
        with self.assertRaises(TypeError):
            _run(bad_factory.create(CheckpointCreateRequest(fixture.snapshot.handle)))

        failing_artifacts = self._new_service(fixture)
        _run(failing_artifacts.initialize())
        with (
            patch.object(fixture.artifacts, "publish", side_effect=RuntimeError("publish failed")),
            self.assertRaises(RuntimeError),
        ):
            _run(
                failing_artifacts.create(
                    CheckpointCreateRequest(
                        fixture.snapshot.handle,
                        CheckpointId("cp-publish-fails"),
                    )
                )
            )
        failed = _run(fixture.checkpoint_store.get(CheckpointId("cp-publish-fails")))
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertIs(failed.state, CheckpointState.FAILED)

    def test_service_reconciles_failed_capture_and_preserves_live_owner(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        capturing = _run(
            fixture.checkpoint_store.compare_and_transition_checkpoint(
                replace(checkpoint, state=CheckpointState.CAPTURING),
                expected_version=checkpoint.version,
                expected_state=CheckpointState.READY,
            )
        )
        shutil.rmtree(capturing.artifact_path)
        self.assertEqual(_run(fixture.checkpoints.reconcile()), ())
        failed = _run(fixture.checkpoint_store.get(checkpoint.checkpoint_id))
        assert failed is not None
        self.assertIs(failed.state, CheckpointState.FAILED)

        ready = _run(
            fixture.checkpoints.create(
                CheckpointCreateRequest(
                    fixture.snapshot.handle,
                    CheckpointId("cp-live-owner"),
                )
            )
        )
        live = RollbackAttempt(
            attempt_id=RollbackAttemptId("rb-live-owner"),
            checkpoint_id=ready.checkpoint_id,
            worktree_id=ready.worktree_id,
            state=RollbackState.STARTED,
            started_at=datetime.now(UTC),
            completed_at=None,
            expected_fingerprint=ready.source_fingerprint,
            owner_pid=os.getpid(),
            owner_token="foreign-live-owner",
        )
        _run(fixture.checkpoint_store.start_attempt(live))
        results = _run(fixture.checkpoints.reconcile())
        self.assertEqual(results, (live,))
        self.assertIs(
            _run(fixture.checkpoint_store.active_attempt("wt-checkpoint")).state,
            RollbackState.STARTED,
        )

    def test_service_rejects_conflicting_active_attempts(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        first = _run(
            fixture.checkpoints.create(
                CheckpointCreateRequest(fixture.snapshot.handle, CheckpointId("cp-first"))
            )
        )
        second = _run(
            fixture.checkpoints.create(
                CheckpointCreateRequest(fixture.snapshot.handle, CheckpointId("cp-second"))
            )
        )
        active = RollbackAttempt(
            attempt_id=RollbackAttemptId("rb-first"),
            checkpoint_id=first.checkpoint_id,
            worktree_id=first.worktree_id,
            state=RollbackState.STARTED,
            started_at=datetime.now(UTC),
            completed_at=None,
            expected_fingerprint=first.source_fingerprint,
            owner_pid=os.getpid(),
            owner_token="foreign-live-owner",
        )
        _run(fixture.checkpoint_store.start_attempt(active))
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints.rollback(second.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.ALREADY_ROLLING_BACK)
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(
                fixture.checkpoints.rollback(
                    first.checkpoint_id,
                    attempt_id=RollbackAttemptId("rb-other"),
                )
            )
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.CONCURRENT_MODIFICATION)

    def test_service_records_indeterminate_verification_and_git_failures(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        checkpoint = _run(
            fixture.checkpoints.create(CheckpointCreateRequest(fixture.snapshot.handle))
        )
        projection = _run(fixture.state_adapter.inspect(fixture.snapshot.handle))
        wrong_projection = replace(projection, index_bytes=projection.index_bytes + b"wrong")
        with (
            patch.object(
                fixture.state_adapter,
                "inspect",
                side_effect=(projection, wrong_projection),
            ),
            self.assertRaises(WorkspaceCheckpointError) as raised,
        ):
            _run(fixture.checkpoints.rollback(checkpoint.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED)
        attempt = _run(fixture.checkpoint_store.active_attempt(fixture.snapshot.worktree_id.value))
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertIs(attempt.state, RollbackState.INDETERMINATE)
        _run(fixture.adapter.unlock_worktree(fixture.target))
        _run(
            fixture.checkpoint_store.compare_and_transition_attempt(
                replace(attempt, state=RollbackState.FAILED),
                expected_version=attempt.version,
                expected_state=RollbackState.INDETERMINATE,
            )
        )

        lock_fixture = _CheckpointFixture()
        self.addCleanup(lock_fixture.close)
        checkpoint_2 = _run(
            lock_fixture.checkpoints.create(
                CheckpointCreateRequest(lock_fixture.snapshot.handle, CheckpointId("cp-lock-fails"))
            )
        )
        with (
            patch.object(
                lock_fixture.adapter,
                "lock_worktree",
                side_effect=WorktreeError(
                    "git boundary failed", kind=WorktreeFailureKind.COMMAND_FAILED
                ),
            ),
            self.assertRaises(WorkspaceCheckpointError) as raised,
        ):
            _run(lock_fixture.checkpoints.rollback(checkpoint_2.checkpoint_id))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.COMMAND_FAILED)

    def test_service_ownership_and_capture_timeout_guards(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        self.assertFalse(_owner_is_alive(None))
        self.assertFalse(_owner_is_alive(0))
        self.assertTrue(_owner_is_alive(os.getpid()))
        missing_handle = replace(
            fixture.snapshot.handle,
            worktree_id=WorktreeId("wt-missing"),
        )
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints._prove_handle(missing_handle, allow_lock_reason=None))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.UNMANAGED)
        wrong_handle = replace(fixture.snapshot.handle, path=fixture.target / "replaced")
        with self.assertRaises(WorkspaceCheckpointError) as raised:
            _run(fixture.checkpoints._prove_handle(wrong_handle, allow_lock_reason=None))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.IDENTITY_MISMATCH)

        class _SlowState:
            async def inspect(self, handle: object) -> WorkspaceProjection:
                del handle
                await asyncio.sleep(0.02)
                raise AssertionError("timeout should happen first")

        service = WorkspaceCheckpointApplicationService(
            git=fixture.adapter,
            workspace_git=fixture.adapter,
            worktrees=fixture.worktree_store,
            state=_SlowState(),  # type: ignore[arg-type]
            checkpoints=fixture.checkpoint_store,
            artifacts=fixture.artifacts,
        )
        with (
            patch(
                "neuro_code.application.checkpoints.service.MAX_CHECKPOINT_CAPTURE_SECONDS",
                0.001,
            ),
            self.assertRaises(WorkspaceCheckpointError) as raised,
        ):
            _run(service._capture(fixture.snapshot.handle))
        self.assertEqual(raised.exception.kind, CheckpointFailureKind.TIMEOUT)


class WorkspaceCheckpointGitAdapterTests(unittest.TestCase):
    def test_git_index_status_lock_and_config_boundaries(self) -> None:
        fixture = _CheckpointFixture()
        self.addCleanup(fixture.close)
        index_path = _run(fixture.adapter.index_path(fixture.target))
        self.assertTrue(index_path.is_file())
        self.assertTrue(_run(fixture.adapter.index_entries(fixture.target)))
        self.assertEqual(_run(fixture.adapter.nonignored_untracked_paths(fixture.target)), b"")
        self.assertEqual(_run(fixture.adapter.status_porcelain(fixture.target)), b"")
        self.assertFalse(_run(fixture.adapter.config_bool(fixture.target, "core.sparseCheckout")))
        _git(fixture.target, "config", "core.sparseCheckout", "true")
        self.assertTrue(_run(fixture.adapter.config_bool(fixture.target, "core.sparseCheckout")))
        _git(fixture.target, "config", "--unset", "core.sparseCheckout")
        with self.assertRaises(WorktreeError) as raised:
            _run(fixture.adapter.config_bool(fixture.target, ""))
        self.assertEqual(raised.exception.kind, WorktreeFailureKind.PROTOCOL)

        index_bytes = _run(fixture.adapter.read_index(fixture.target))
        _run(fixture.adapter.replace_index(fixture.target, index_bytes))
        with self.assertRaises(TypeError):
            _run(fixture.adapter.replace_index(fixture.target, "raw"))  # type: ignore[arg-type]
        with self.assertRaises(WorktreeError) as raised:
            _run(fixture.adapter.lock_worktree(fixture.target, ""))
        self.assertEqual(raised.exception.kind, WorktreeFailureKind.PROTOCOL)
        _run(fixture.adapter.lock_worktree(fixture.target, "adapter-test"))
        record = next(
            item
            for item in _run(fixture.adapter.list_worktrees(fixture.repository))
            if item.path == fixture.target
        )
        self.assertTrue(record.locked)
        self.assertEqual(record.lock_reason, "adapter-test")
        _run(fixture.adapter.unlock_worktree(fixture.target))
        self.assertEqual(_run(fixture.adapter.git_version()) >= MINIMUM_GIT_VERSION, True)
