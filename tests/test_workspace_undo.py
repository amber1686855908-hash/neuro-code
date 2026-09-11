from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from neuro_code.application.checkpoints.turn_undo import (
    TurnWorkspaceCheckpointCoordinator,
    WorkspaceUndoPreparationError,
    binding_has_live_mutators,
)
from neuro_code.application.ports.checkpoints import (
    CheckpointFailureKind,
    WorkspaceCheckpointError,
)
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemAccessTarget,
)
from neuro_code.domain.background_tasks import BackgroundTaskStatus
from neuro_code.domain.checkpoints import (
    CheckpointId,
    SourceWorkspaceCheckpointGrant,
)
from neuro_code.domain.conversation.events import AgentEventKind
from neuro_code.domain.workspace_undo import (
    WorkspaceUndoAssociation,
    WorkspaceUndoReason,
    WorkspaceUndoState,
)
from neuro_code.domain.worktree import WorktreeRepositoryIdentity


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


class _MemorySessionStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.append_count = 0
        self.fail_on_append: int | None = None

    async def load_events(self, session_id: str) -> list[dict[str, object]]:
        del session_id
        return [dict(event) for event in self.events]

    async def next_event_sequence(self, session_id: str) -> int:
        del session_id
        return max((event["sequence"] for event in self.events), default=0) + 1  # type: ignore[operator]

    async def append_event(self, session_id: str, event: object) -> None:
        del session_id
        self.append_count += 1
        if self.fail_on_append == self.append_count:
            raise OSError("fixture append failure")
        self.events.append(event.to_dict())  # type: ignore[union-attr]


class _FakeCheckpointService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        repository = WorktreeRepositoryIdentity(
            common_dir=self.root / ".git",
            source_worktree=self.root,
            git_dir=self.root / ".git",
            head_sha="a" * 40,
        )
        self.grant = SourceWorkspaceCheckpointGrant.issue(
            repository,
            path=self.root,
            head_sha="a" * 40,
            branch="main",
            detached=False,
        )
        self.checkpoint = SimpleNamespace(
            checkpoint_id=CheckpointId("cp-fixture"),
            repository=repository,
            canonical_path=self.root,
            head_sha="a" * 40,
            branch="main",
            detached=False,
        )
        self.create_calls = 0
        self.rollback_calls = 0
        self.fail_rollback = False

    async def initialize(self) -> None:
        return

    async def authorize_source_workspace(self, path: Path) -> SourceWorkspaceCheckpointGrant:
        assert path == self.root
        return self.grant

    async def ignored_source_paths(
        self,
        grant: SourceWorkspaceCheckpointGrant,
        paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        assert grant == self.grant
        del paths
        return ()

    async def create(self, request: object) -> SimpleNamespace:
        assert request.worktree == self.grant  # type: ignore[union-attr]
        self.create_calls += 1
        return self.checkpoint

    async def get(self, checkpoint_id: CheckpointId) -> SimpleNamespace | None:
        return self.checkpoint if checkpoint_id == self.checkpoint.checkpoint_id else None

    async def rollback(
        self,
        checkpoint_id: CheckpointId,
        *,
        target: SourceWorkspaceCheckpointGrant,
    ) -> SimpleNamespace:
        assert checkpoint_id == self.checkpoint.checkpoint_id
        assert target == self.grant
        self.rollback_calls += 1
        if self.fail_rollback:
            raise WorkspaceCheckpointError(
                "fixture rollback failed",
                kind=CheckpointFailureKind.COMMAND_FAILED,
            )
        return SimpleNamespace(attempt_id=SimpleNamespace(value="rb-fixture"))


def _plan(root: Path) -> FilesystemAccessPlan:
    return FilesystemAccessPlan(
        "update_file",
        (
            FilesystemAccessTarget(
                requested_path="tracked.py",
                canonical_path=root / "tracked.py",
                owning_workspace_root=root,
                policy_path="tracked.py",
                operation=FilesystemAccessOperation.UPDATE,
                exists=True,
                is_primary_workspace=True,
            ),
        ),
    )


class WorkspaceUndoCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="neuro-workspace-undo-")
        self.root = Path(self.directory.name)
        self.store = _MemorySessionStore()
        self.service = _FakeCheckpointService(self.root)
        self.coordinator = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(self.coordinator.initialize())

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _prepare(self, *, turn_id: str = "turn-1") -> None:
        _run(
            self.coordinator.prepare(
                session_id="session-1",
                turn_id=turn_id,
                plan=_plan(self.root),
                client_file_system=None,
                client_terminal=None,
            )
        )

    def test_checkpoint_barrier_is_single_and_undo_is_one_shot(self) -> None:
        async def prepare_concurrently() -> None:
            await asyncio.gather(*(self._prepare_async() for _ in range(4)))

        async def run() -> None:
            await prepare_concurrently()
            result = await self.coordinator.undo("session-1")
            self.assertIs(result.state, WorkspaceUndoState.ROLLED_BACK)
            self.assertTrue(result.restored)
            repeated = await self.coordinator.undo("session-1")
            self.assertIs(repeated.reason, WorkspaceUndoReason.ALREADY_ROLLED_BACK)
            self.assertEqual(self.service.rollback_calls, 1)
            handoff = await self.coordinator.prepare_verification_handoff("session-1")
            self.assertIsNotNone(handoff)
            assert handoff is not None
            await handoff.commit()
            await handoff.commit()
            self.assertIsNone(await self.coordinator.prepare_verification_handoff("session-1"))

        _run(run())
        self.assertEqual(self.service.create_calls, 1)
        self.assertEqual(len(self.store.events), 4)
        self.assertEqual(
            self.store.events[0]["kind"],
            AgentEventKind.WORKSPACE_UNDO_STATE.value,
        )

    async def _prepare_async(self) -> None:
        await self.coordinator.prepare(
            session_id="session-1",
            turn_id="turn-1",
            plan=_plan(self.root),
            client_file_system=None,
            client_terminal=None,
        )

    def test_unsafe_later_turn_invalidates_latest_checkpoint(self) -> None:
        self._prepare()
        _run(
            self.coordinator.prepare(
                session_id="session-1",
                turn_id="turn-2",
                plan=None,
                client_file_system=None,
                client_terminal=None,
            )
        )

        result = _run(self.coordinator.undo("session-1"))
        self.assertIs(result.state, WorkspaceUndoState.UNAVAILABLE)
        self.assertIs(result.reason, WorkspaceUndoReason.UNBOUNDED_MUTATION)
        self.assertEqual(self.service.rollback_calls, 0)

    def test_undo_refuses_live_terminal_without_rollback(self) -> None:
        self._prepare()

        result = _run(self.coordinator.undo("session-1", live_terminal=True))

        self.assertIs(result.reason, WorkspaceUndoReason.LIVE_MUTATOR)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(len(self.store.events), 1)

    def test_undo_refuses_live_background_without_rollback(self) -> None:
        self._prepare()

        result = _run(self.coordinator.undo("session-1", live_background=True))

        self.assertIs(result.reason, WorkspaceUndoReason.LIVE_MUTATOR)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(len(self.store.events), 1)

    def test_checkpoint_or_invalidation_persistence_failure_fails_closed(self) -> None:
        self.store.fail_on_append = 1
        with self.assertRaises(WorkspaceUndoPreparationError):
            _run(
                self.coordinator.prepare(
                    session_id="session-1",
                    turn_id="turn-1",
                    plan=None,
                    client_file_system=None,
                    client_terminal=None,
                )
            )
        self.assertEqual(self.service.create_calls, 0)

    def test_restart_can_undo_available_checkpoint(self) -> None:
        self._prepare()
        restarted = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(restarted.initialize())
        result = _run(restarted.undo("session-1"))
        self.assertIs(result.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(self.service.rollback_calls, 1)

    def test_rollback_guard_prevents_duplicate_after_final_association_failure(self) -> None:
        self._prepare()
        self.store.fail_on_append = 3
        result = _run(self.coordinator.undo("session-1"))
        self.assertIs(result.reason, WorkspaceUndoReason.PERSISTENCE_FAILED)
        self.assertEqual(self.service.rollback_calls, 1)

        restarted = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(restarted.initialize())
        repeated = _run(restarted.undo("session-1"))
        self.assertIs(repeated.reason, WorkspaceUndoReason.ROLLBACK_INDETERMINATE)
        self.assertEqual(self.service.rollback_calls, 1)

    def test_rollback_failure_is_durable_and_not_retried(self) -> None:
        self._prepare()
        self.service.fail_rollback = True
        result = _run(self.coordinator.undo("session-1"))
        self.assertIs(result.reason, WorkspaceUndoReason.ROLLBACK_INDETERMINATE)

        restarted = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(restarted.initialize())
        repeated = _run(restarted.undo("session-1"))
        self.assertIs(repeated.reason, WorkspaceUndoReason.ROLLBACK_INDETERMINATE)
        self.assertEqual(self.service.rollback_calls, 1)

    def test_malformed_ledger_event_fails_closed(self) -> None:
        self.store.events.append(
            {
                "kind": AgentEventKind.WORKSPACE_UNDO_STATE.value,
                "sequence": 1,
                "created_at": "not-a-timestamp",
                "data": {},
            }
        )
        with self.assertRaises(WorkspaceUndoPreparationError):
            _run(self.coordinator.undo("session-1"))


class WorkspaceUndoDomainTests(unittest.TestCase):
    def test_handoff_consumption_is_only_valid_for_rolled_back_state(self) -> None:
        with self.assertRaises(ValueError):
            WorkspaceUndoAssociation(
                session_id="session-1",
                turn_id="turn-1",
                state=WorkspaceUndoState.AVAILABLE,
                updated_at=datetime.now(UTC),
                checkpoint_id=CheckpointId("cp-fixture"),
                verification_handoff_consumed=True,
            )


class WorkspaceUndoMutatorProbeTests(unittest.TestCase):
    def test_probe_detects_live_resources_without_terminating_them(self) -> None:
        terminal = _ProbeTerminalSession(exit_code=None)
        background = _ProbeBackgroundManager(
            (SimpleNamespace(status=BackgroundTaskStatus.RUNNING),)
        )
        terminals = _ProbeTerminalManager((terminal,))

        live_background, live_terminal = _run(binding_has_live_mutators(background, terminals))

        self.assertTrue(live_background)
        self.assertTrue(live_terminal)
        self.assertEqual(terminal.wait_calls, 1)
        self.assertEqual(terminal.close_calls, 0)

    def test_probe_ignores_terminal_resources_that_have_exited(self) -> None:
        terminal = _ProbeTerminalSession(exit_code=0)
        background = _ProbeBackgroundManager(
            (SimpleNamespace(status=BackgroundTaskStatus.COMPLETED),)
        )
        terminals = _ProbeTerminalManager((terminal,))

        live_background, live_terminal = _run(binding_has_live_mutators(background, terminals))

        self.assertFalse(live_background)
        self.assertFalse(live_terminal)
        self.assertEqual(terminal.wait_calls, 1)
        self.assertEqual(terminal.close_calls, 0)


class _ProbeBackgroundManager:
    def __init__(self, snapshots: tuple[object, ...]) -> None:
        self.snapshots = snapshots

    async def list(self) -> tuple[object, ...]:
        return self.snapshots


class _ProbeTerminalSession:
    def __init__(self, *, exit_code: int | None) -> None:
        self.exit_code = exit_code
        self.wait_calls = 0
        self.close_calls = 0

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None:
        del timeout_seconds
        self.wait_calls += 1
        return self.exit_code

    async def close(self) -> None:
        self.close_calls += 1


class _ProbeTerminalManager:
    def __init__(self, sessions: tuple[_ProbeTerminalSession, ...]) -> None:
        self.sessions = sessions

    async def list_sessions(self) -> tuple[_ProbeTerminalSession, ...]:
        return self.sessions
