from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from neuro_code.application.checkpoints.turn_undo import (
    TurnWorkspaceCheckpointCoordinator,
    WorkspaceUndoPreparationError,
    _WorkspaceUndoLedger,
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
    CheckpointFingerprint,
    CheckpointId,
    RollbackAttemptId,
    RollbackState,
    SourceWorkspaceCheckpointGrant,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.workspace_undo import (
    WorkspaceUndoAssociation,
    WorkspaceUndoReason,
    WorkspaceUndoResult,
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
        self.open_turns: list[object] = []

    async def load_events(self, session_id: str) -> list[dict[str, object]]:
        del session_id
        return [dict(event) for event in self.events]

    async def next_event_sequence(self, session_id: str) -> int:
        del session_id
        return max((event["sequence"] for event in self.events), default=0) + 1  # type: ignore[operator]

    async def append_event(self, session_id: str, event: object) -> None:
        del session_id
        self._append_event(event)

    def _append_event(self, event: object) -> None:
        self.append_count += 1
        if self.fail_on_append == self.append_count:
            raise OSError("fixture append failure")
        self.events.append(event.to_dict())  # type: ignore[union-attr]

    async def load_open_turn_attempts(self, session_id: str) -> list[object]:
        del session_id
        return list(self.open_turns)

    def _latest_undo_data(self, session_id: str) -> dict[str, object] | None:
        del session_id
        undo_events = [
            event
            for event in self.events
            if event.get("kind") == AgentEventKind.WORKSPACE_UNDO_STATE.value
        ]
        if not undo_events:
            return None
        return max(undo_events, key=lambda event: event["sequence"])["data"]  # type: ignore[return-value]

    async def claim_workspace_undo(
        self,
        expected: WorkspaceUndoAssociation,
        rolling_back: WorkspaceUndoAssociation,
    ) -> bool:
        if (
            self.open_turns
            or self._latest_undo_data(expected.session_id) != expected.to_event_data()
        ):
            return False
        sequence = await self.next_event_sequence(expected.session_id)
        self._append_event(
            AgentEvent.create(
                sequence,
                AgentEventKind.WORKSPACE_UNDO_STATE,
                rolling_back.to_event_data(),
            )
        )
        return True

    async def seal_workspace_undo(
        self,
        expected: WorkspaceUndoAssociation,
        sealed: WorkspaceUndoAssociation,
    ) -> bool:
        if (
            self.open_turns
            or self._latest_undo_data(expected.session_id) != expected.to_event_data()
        ):
            return False
        sequence = await self.next_event_sequence(expected.session_id)
        self._append_event(
            AgentEvent.create(
                sequence,
                AgentEventKind.WORKSPACE_UNDO_STATE,
                sealed.to_event_data(),
            )
        )
        return True


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
        self.projection = WorkspaceProjection(
            head_sha="a" * 40,
            branch="main",
            detached=False,
            index_bytes=b"index",
            entries=(),
        )
        self.checkpoint = SimpleNamespace(
            checkpoint_id=CheckpointId("cp-fixture"),
            repository=repository,
            canonical_path=self.root,
            head_sha="a" * 40,
            branch="main",
            detached=False,
            source_fingerprint=workspace_projection_fingerprint(self.grant, self.projection),
        )
        self.create_calls = 0
        self.rollback_calls = 0
        self.restore_calls = 0
        self.retire_calls = 0
        self.completed_attempts: set[str] = set()
        self.fail_rollback = False
        self.checkpoint_error: WorkspaceCheckpointError | None = None

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
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        self.create_calls += 1
        return self.checkpoint

    async def get(self, checkpoint_id: CheckpointId) -> SimpleNamespace | None:
        return self.checkpoint if checkpoint_id == self.checkpoint.checkpoint_id else None

    async def inspect(self, grant: SourceWorkspaceCheckpointGrant) -> WorkspaceProjection:
        assert grant == self.grant
        return self.projection

    async def rollback(
        self,
        checkpoint_id: CheckpointId,
        *,
        target: SourceWorkspaceCheckpointGrant,
        attempt_id: object,
        expected_current_fingerprint: CheckpointFingerprint | None = None,
    ) -> SimpleNamespace:
        assert checkpoint_id == self.checkpoint.checkpoint_id
        assert target == self.grant
        del expected_current_fingerprint
        self.rollback_calls += 1
        if self.fail_rollback:
            raise WorkspaceCheckpointError(
                "fixture rollback failed",
                kind=CheckpointFailureKind.COMMAND_FAILED,
            )
        attempt_value = attempt_id.value  # type: ignore[attr-defined]
        if attempt_value not in self.completed_attempts:
            self.completed_attempts.add(attempt_value)
            self.restore_calls += 1
        return SimpleNamespace(
            attempt_id=attempt_id,
            state=RollbackState.COMPLETED,
        )

    async def retire_source_rollback_attempt(
        self,
        attempt_id: RollbackAttemptId,
        checkpoint_id: CheckpointId,
        *,
        target: SourceWorkspaceCheckpointGrant,
    ) -> SimpleNamespace:
        assert checkpoint_id == self.checkpoint.checkpoint_id
        assert target == self.grant
        del attempt_id
        self.retire_calls += 1
        return SimpleNamespace(state=RollbackState.FAILED)


def _plan(root: Path) -> FilesystemAccessPlan:
    canonical_root = root.expanduser().resolve(strict=False)
    return FilesystemAccessPlan(
        "update_file",
        (
            FilesystemAccessTarget(
                requested_path="tracked.py",
                canonical_path=canonical_root / "tracked.py",
                owning_workspace_root=canonical_root,
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
        _run(self.coordinator.seal_turn("session-1", turn_id))

    def test_checkpoint_barrier_is_single_and_undo_is_one_shot(self) -> None:
        async def prepare_concurrently() -> None:
            await asyncio.gather(*(self._prepare_async() for _ in range(4)))

        async def run() -> None:
            await prepare_concurrently()
            await self.coordinator.seal_turn("session-1", "turn-1")
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
        self.assertEqual(len(self.store.events), 5)
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

    def test_unsealed_checkpoint_fails_closed_without_guessing_workspace_state(self) -> None:
        _run(
            self.coordinator.prepare(
                session_id="session-1",
                turn_id="turn-1",
                plan=_plan(self.root),
                client_file_system=None,
                client_terminal=None,
            )
        )

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.ROLLBACK_INDETERMINATE)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(len(self.store.events), 1)

    def test_post_turn_workspace_change_refuses_restore_without_overwrite(self) -> None:
        self._prepare()
        self.service.projection = replace(self.service.projection, index_bytes=b"manual-index")

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.WORKSPACE_CHANGED)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(self.service.retire_calls, 1)
        self.assertEqual(self.service.restore_calls, 0)
        self.assertEqual(self.service.projection.index_bytes, b"manual-index")
        latest = _run(self.coordinator._ledger.latest("session-1"))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.state, WorkspaceUndoState.UNAVAILABLE)
        self.assertIs(latest.reason, WorkspaceUndoReason.WORKSPACE_CHANGED)

    def test_post_turn_tracked_file_change_refuses_restore_without_overwrite(self) -> None:
        self._prepare()
        self.service.projection = replace(
            self.service.projection,
            entries=(
                WorkspaceFileEntry(
                    path="tracked.py",
                    scope=WorkspaceFileScope.TRACKED,
                    present=True,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                    content=b"manual tracked change",
                ),
            ),
        )

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.WORKSPACE_CHANGED)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(self.service.restore_calls, 0)
        self.assertEqual(self.service.projection.entries[0].content, b"manual tracked change")

    def test_post_turn_untracked_file_change_refuses_restore_without_overwrite(self) -> None:
        self._prepare()
        self.service.projection = replace(
            self.service.projection,
            entries=(
                WorkspaceFileEntry(
                    path="manual.txt",
                    scope=WorkspaceFileScope.UNTRACKED,
                    present=True,
                    kind=WorkspaceFileKind.REGULAR,
                    mode=0o100644,
                    content=b"manual untracked change",
                ),
            ),
        )

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.WORKSPACE_CHANGED)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(self.service.restore_calls, 0)
        self.assertEqual(self.service.projection.entries[0].content, b"manual untracked change")

    def test_ignored_only_change_does_not_invalidate_undo_projection(self) -> None:
        self._prepare()
        ignored = self.root / "ignored.tmp"
        ignored.write_bytes(b"manual ignored change")

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(ignored.read_bytes(), b"manual ignored change")
        self.assertEqual(self.service.restore_calls, 1)

    def test_open_turn_wins_before_undo_claim(self) -> None:
        self._prepare()
        self.store.open_turns.append(object())

        result = _run(self.coordinator.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.ACTIVE_TURN)
        self.assertEqual(self.service.rollback_calls, 0)
        latest = _run(self.coordinator._ledger.latest("session-1"))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.state, WorkspaceUndoState.AVAILABLE)
        self.assertIsNotNone(latest.expected_current_fingerprint)

    def test_interrupted_rollback_resumes_same_durable_attempt(self) -> None:
        self._prepare()
        available = _run(self.coordinator._ledger.latest("session-1"))
        assert available is not None
        self.assertIs(available.state, WorkspaceUndoState.AVAILABLE)
        self.assertIsNotNone(available.checkpoint_id)
        self.assertIsNotNone(available.expected_current_fingerprint)
        expected_current_fingerprint = available.expected_current_fingerprint
        attempt_id = RollbackAttemptId("rb-interrupted")
        rolling_back = replace(
            available,
            state=WorkspaceUndoState.ROLLING_BACK,
            expected_current_fingerprint=expected_current_fingerprint,
            rollback_attempt_id=attempt_id,
        )
        self.assertTrue(_run(self.store.claim_workspace_undo(available, rolling_back)))

        restarted = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(restarted.initialize())
        result = _run(restarted.undo("session-1"))

        self.assertIs(result.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(self.service.rollback_calls, 1)
        self.assertEqual(self.service.restore_calls, 1)
        latest = _run(restarted._ledger.latest("session-1"))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(latest.rollback_attempt_id, attempt_id)

    def test_interrupted_rollback_refuses_unexpected_workspace_without_restore(self) -> None:
        self._prepare()
        available = _run(self.coordinator._ledger.latest("session-1"))
        assert available is not None
        self.assertIs(available.state, WorkspaceUndoState.AVAILABLE)
        self.assertIsNotNone(available.checkpoint_id)
        self.assertIsNotNone(available.expected_current_fingerprint)
        rolling_back = replace(
            available,
            state=WorkspaceUndoState.ROLLING_BACK,
            rollback_attempt_id=RollbackAttemptId("rb-indeterminate"),
        )
        self.assertTrue(_run(self.store.claim_workspace_undo(available, rolling_back)))
        self.service.projection = replace(self.service.projection, index_bytes=b"unexpected")

        restarted = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(restarted.initialize())
        result = _run(restarted.undo("session-1"))

        self.assertIs(result.reason, WorkspaceUndoReason.ROLLBACK_INDETERMINATE)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(self.service.restore_calls, 0)
        self.assertEqual(self.service.projection.index_bytes, b"unexpected")

    def test_undo_refuses_live_terminal_without_rollback(self) -> None:
        self._prepare()

        result = _run(self.coordinator.undo("session-1", live_terminal=True))

        self.assertIs(result.reason, WorkspaceUndoReason.LIVE_MUTATOR)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(len(self.store.events), 2)

    def test_undo_refuses_live_background_without_rollback(self) -> None:
        self._prepare()

        result = _run(self.coordinator.undo("session-1", live_background=True))

        self.assertIs(result.reason, WorkspaceUndoReason.LIVE_MUTATOR)
        self.assertEqual(self.service.rollback_calls, 0)
        self.assertEqual(len(self.store.events), 2)

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

    def test_disabled_and_invalid_inputs_are_safe_noops(self) -> None:
        disabled = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=self.service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
            enabled=False,
        )
        _run(disabled.initialize())
        _run(
            disabled.prepare(
                session_id="session-1",
                turn_id="turn-1",
                plan=_plan(self.root),
                client_file_system=None,
                client_terminal=None,
            )
        )
        disabled_result = _run(disabled.undo("session-1"))
        self.assertIs(disabled_result.reason, WorkspaceUndoReason.CAPABILITY_UNAVAILABLE)

        for session_id, turn_id in (
            (None, "turn-1"),
            ("", "turn-1"),
            ("session-1", None),
        ):
            _run(
                self.coordinator.prepare(
                    session_id=session_id,
                    turn_id=turn_id,
                    plan=_plan(self.root),
                    client_file_system=None,
                    client_terminal=None,
                )
            )
        self.assertEqual(self.store.events, [])

    def test_capability_initialization_failure_marks_next_turn_unavailable(self) -> None:
        service = _FakeCheckpointService(self.root)

        async def fail_initialize() -> None:
            raise RuntimeError("checkpoint capability unavailable")

        service.initialize = fail_initialize  # type: ignore[method-assign]
        coordinator = TurnWorkspaceCheckpointCoordinator(
            checkpoint_service=service,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            source_workspace=self.root,
        )
        _run(coordinator.initialize())
        _run(
            coordinator.prepare(
                session_id="session-1",
                turn_id="turn-1",
                plan=_plan(self.root),
                client_file_system=None,
                client_terminal=None,
            )
        )
        latest = _run(coordinator._ledger.latest("session-1"))
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.state, WorkspaceUndoState.UNAVAILABLE)
        self.assertIs(latest.reason, WorkspaceUndoReason.CAPABILITY_UNAVAILABLE)

    def test_prepare_uses_event_sink_and_rejects_ignored_targets(self) -> None:
        captured: list[tuple[AgentEventKind, dict[str, object]]] = []

        async def ignored_paths(
            grant: SourceWorkspaceCheckpointGrant,
            paths: tuple[str, ...],
        ) -> tuple[str, ...]:
            assert grant == self.service.grant
            assert paths == ("tracked.py",)
            return paths

        async def event_sink(kind: AgentEventKind, data: dict[str, object]) -> None:
            captured.append((kind, data))

        self.service.ignored_source_paths = ignored_paths  # type: ignore[method-assign]
        _run(
            self.coordinator.prepare(
                session_id="session-1",
                turn_id="turn-1",
                plan=_plan(self.root),
                client_file_system=None,
                client_terminal=None,
                event_sink=event_sink,
            )
        )
        self.assertEqual(self.store.events, [])
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0][0], AgentEventKind.WORKSPACE_UNDO_STATE)
        self.assertEqual(captured[0][1]["reason"], WorkspaceUndoReason.IGNORED_TARGET.value)

    def test_checkpoint_failure_kinds_map_to_bounded_undo_reasons(self) -> None:
        for kind, expected in (
            (
                CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                WorkspaceUndoReason.CHECKPOINT_TOO_LARGE,
            ),
            (
                CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE,
                WorkspaceUndoReason.UNSUPPORTED_WORKSPACE,
            ),
            (CheckpointFailureKind.COMMAND_FAILED, WorkspaceUndoReason.CHECKPOINT_FAILED),
        ):
            with self.subTest(kind=kind):
                service = _FakeCheckpointService(self.root)
                store = _MemorySessionStore()
                service.checkpoint_error = WorkspaceCheckpointError("checkpoint failed", kind=kind)
                coordinator = TurnWorkspaceCheckpointCoordinator(
                    checkpoint_service=service,  # type: ignore[arg-type]
                    store=store,  # type: ignore[arg-type]
                    source_workspace=self.root,
                )
                _run(coordinator.initialize())
                _run(
                    coordinator.prepare(
                        session_id="session-1",
                        turn_id="turn-1",
                        plan=_plan(self.root),
                        client_file_system=None,
                        client_terminal=None,
                    )
                )
                latest = _run(coordinator._ledger.latest("session-1"))
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertIs(latest.reason, expected)

    def test_undo_claim_failure_and_race_remain_fail_closed(self) -> None:
        self._prepare()
        _run(self.coordinator.seal_turn("session-1", "turn-1"))
        self.store.fail_on_append = self.store.append_count + 1
        failed = _run(self.coordinator.undo("session-1"))
        self.assertIs(failed.reason, WorkspaceUndoReason.PERSISTENCE_FAILED)
        self.assertEqual(self.service.rollback_calls, 0)

        class _RollingRaceStore(_MemorySessionStore):
            def __init__(self, *, publish_rolling: bool) -> None:
                super().__init__()
                self.publish_rolling = publish_rolling
                self.expected: WorkspaceUndoAssociation | None = None
                self.rolling: WorkspaceUndoAssociation | None = None

            async def claim_workspace_undo(
                self,
                expected: WorkspaceUndoAssociation,
                rolling_back: WorkspaceUndoAssociation,
            ) -> bool:
                self.expected = expected
                self.rolling = rolling_back
                if self.publish_rolling:
                    sequence = await self.next_event_sequence(expected.session_id)
                    self._append_event(
                        AgentEvent.create(
                            sequence,
                            AgentEventKind.WORKSPACE_UNDO_STATE,
                            rolling_back.to_event_data(),
                        )
                    )
                return False

        for publish_rolling, expected_reason in (
            (False, WorkspaceUndoReason.CONCURRENT_MODIFICATION),
            (True, WorkspaceUndoReason.ROLLBACK_INDETERMINATE),
        ):
            with self.subTest(publish_rolling=publish_rolling):
                store = _RollingRaceStore(publish_rolling=publish_rolling)
                service = _FakeCheckpointService(self.root)
                coordinator = TurnWorkspaceCheckpointCoordinator(
                    checkpoint_service=service,  # type: ignore[arg-type]
                    store=store,  # type: ignore[arg-type]
                    source_workspace=self.root,
                )
                _run(coordinator.initialize())
                _run(
                    coordinator.prepare(
                        session_id="session-1",
                        turn_id="turn-1",
                        plan=_plan(self.root),
                        client_file_system=None,
                        client_terminal=None,
                    )
                )
                available = _run(coordinator._ledger.latest("session-1"))
                assert available is not None
                sealed = replace(
                    available,
                    expected_current_fingerprint=CheckpointFingerprint("a" * 64),
                )
                store.events.append(
                    AgentEvent.create(
                        2,
                        AgentEventKind.WORKSPACE_UNDO_STATE,
                        sealed.to_event_data(),
                    ).to_dict()
                )
                result = _run(coordinator.undo("session-1"))
                self.assertIs(result.reason, expected_reason)

    def test_ledger_handles_malformed_events_and_missing_atomic_operations(self) -> None:
        association = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.AVAILABLE,
            updated_at=datetime.now(UTC),
            checkpoint_id=CheckpointId("cp-fixture"),
        )

        class _ReadOnlyStore:
            def __init__(self, events: list[object]) -> None:
                self.events = events

            async def load_events(self, session_id: str) -> list[object]:
                del session_id
                return self.events

        valid_event = AgentEvent.create(
            1,
            AgentEventKind.WORKSPACE_UNDO_STATE,
            association.to_event_data(),
        ).to_dict()
        for malformed in (
            [object()],
            [{**valid_event, "created_at": datetime.now(UTC).replace(tzinfo=None)}],
            [{**valid_event, "sequence": True}],
            [{**valid_event, "data": {}}],
        ):
            with self.subTest(malformed=malformed):
                ledger = _WorkspaceUndoLedger(_ReadOnlyStore(malformed))  # type: ignore[arg-type]
                with self.assertRaises(WorkspaceUndoPreparationError):
                    _run(ledger.latest("session-1"))

        rolling_back = replace(
            association,
            state=WorkspaceUndoState.ROLLING_BACK,
            expected_current_fingerprint=CheckpointFingerprint("a" * 64),
            rollback_attempt_id=RollbackAttemptId("rb-fixture"),
        )
        sealed = replace(
            association,
            expected_current_fingerprint=CheckpointFingerprint("a" * 64),
        )
        ledger = _WorkspaceUndoLedger(_ReadOnlyStore([]))  # type: ignore[arg-type]
        with self.assertRaises(WorkspaceUndoPreparationError):
            _run(ledger.claim(association, rolling_back))
        with self.assertRaises(WorkspaceUndoPreparationError):
            _run(ledger.seal(association, sealed))

    def test_rollback_and_checkpoint_error_projection_is_bounded(self) -> None:
        cases = (
            (CheckpointFailureKind.HEAD_MISMATCH, WorkspaceUndoReason.HEAD_CHANGED),
            (CheckpointFailureKind.CONCURRENT_MODIFICATION, WorkspaceUndoReason.WORKSPACE_CHANGED),
            (
                CheckpointFailureKind.ALREADY_ROLLING_BACK,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            ),
            (
                CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            ),
            (CheckpointFailureKind.COMMAND_FAILED, WorkspaceUndoReason.ROLLBACK_INDETERMINATE),
            (CheckpointFailureKind.IDENTITY_MISMATCH, WorkspaceUndoReason.ROLLBACK_FAILED),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind):
                error = WorkspaceCheckpointError("bounded error", kind=kind)
                self.assertIs(self.coordinator._rollback_reason(error), expected)

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
        self.store.fail_on_append = 4
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
        self.assertIs(repeated.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(self.service.rollback_calls, 2)
        self.assertEqual(self.service.restore_calls, 1)

    def test_next_checkpoint_is_usable_after_completed_rollback(self) -> None:
        self._prepare(turn_id="turn-1")
        first = _run(self.coordinator.undo("session-1"))
        self.assertIs(first.state, WorkspaceUndoState.ROLLED_BACK)

        self._prepare(turn_id="turn-2")
        second = _run(self.coordinator.undo("session-1"))

        self.assertIs(second.state, WorkspaceUndoState.ROLLED_BACK)
        self.assertEqual(self.service.create_calls, 2)
        self.assertEqual(self.service.rollback_calls, 2)
        self.assertEqual(self.service.restore_calls, 2)

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

    def test_association_states_round_trip_their_durable_optional_fields(self) -> None:
        timestamp = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
        available = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.AVAILABLE,
            updated_at=timestamp,
            checkpoint_id=CheckpointId("cp-fixture"),
        )
        rolling_back = replace(
            available,
            state=WorkspaceUndoState.ROLLING_BACK,
            expected_current_fingerprint=CheckpointFingerprint("a" * 64),
            rollback_attempt_id=RollbackAttemptId("rb-fixture"),
        )
        unavailable = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.UNAVAILABLE,
            updated_at=timestamp,
            reason=WorkspaceUndoReason.CAPABILITY_UNAVAILABLE,
        )
        rolled_back = replace(
            rolling_back,
            state=WorkspaceUndoState.ROLLED_BACK,
            verification_mutation_id="undo-mutation",
        )

        for association in (available, rolling_back, unavailable, rolled_back):
            with self.subTest(state=association.state):
                restored = WorkspaceUndoAssociation.from_event_data(
                    association.session_id,
                    association.to_event_data(),
                    updated_at=timestamp,
                )
                self.assertEqual(restored, association)

    def test_association_rejects_untrusted_types_bounds_and_state_combinations(self) -> None:
        timestamp = datetime.now(UTC)
        available = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.AVAILABLE,
            updated_at=timestamp.replace(tzinfo=UTC),
            checkpoint_id=CheckpointId("cp-fixture"),
        )
        expected = CheckpointFingerprint("a" * 64)
        rolling_back = replace(
            available,
            state=WorkspaceUndoState.ROLLING_BACK,
            expected_current_fingerprint=expected,
            rollback_attempt_id=RollbackAttemptId("rb-fixture"),
        )
        unavailable = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.UNAVAILABLE,
            updated_at=timestamp.replace(tzinfo=UTC),
            reason=WorkspaceUndoReason.NO_CHECKPOINT,
        )
        rolled_back = replace(
            rolling_back,
            state=WorkspaceUndoState.ROLLED_BACK,
            verification_mutation_id="undo-mutation",
        )

        type_cases = (
            {"session_id": ""},
            {"turn_id": "\x00"},
            {"session_id": "é" * 129},
            {"state": "available"},
            {"updated_at": datetime.now(UTC).replace(tzinfo=None)},
            {"checkpoint_id": "cp-fixture"},
            {"reason": "no_checkpoint"},
            {"expected_current_fingerprint": "a" * 64},
            {"rollback_attempt_id": "rb-fixture"},
            {"verification_mutation_id": object()},
            {"verification_handoff_consumed": 1},
        )
        for changes in type_cases:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                replace(available, **changes)

        invalid_states = (
            (available, {"checkpoint_id": None}),
            (available, {"reason": WorkspaceUndoReason.NO_CHECKPOINT}),
            (available, {"verification_mutation_id": "mutation"}),
            (available, {"rollback_attempt_id": RollbackAttemptId("rb-extra")}),
            (rolling_back, {"checkpoint_id": None}),
            (rolling_back, {"reason": WorkspaceUndoReason.NO_CHECKPOINT}),
            (rolling_back, {"expected_current_fingerprint": None}),
            (rolling_back, {"rollback_attempt_id": None}),
            (rolling_back, {"verification_mutation_id": "mutation"}),
            (rolling_back, {"verification_handoff_consumed": True}),
            (unavailable, {"reason": None}),
            (unavailable, {"checkpoint_id": CheckpointId("cp-extra")}),
            (unavailable, {"verification_mutation_id": "mutation"}),
            (unavailable, {"verification_handoff_consumed": True}),
            (unavailable, {"expected_current_fingerprint": expected}),
            (unavailable, {"rollback_attempt_id": RollbackAttemptId("rb-extra")}),
            (rolled_back, {"checkpoint_id": None}),
            (rolled_back, {"reason": WorkspaceUndoReason.NO_CHECKPOINT}),
            (rolled_back, {"verification_mutation_id": None}),
        )
        for association, changes in invalid_states:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(association, **changes)

    def test_event_data_rejects_malformed_fields_and_unknown_states(self) -> None:
        association = WorkspaceUndoAssociation(
            session_id="session-1",
            turn_id="turn-1",
            state=WorkspaceUndoState.AVAILABLE,
            updated_at=datetime.now(UTC),
            checkpoint_id=CheckpointId("cp-fixture"),
        )
        base = association.to_event_data()
        malformed = (
            [],
            {**base, "schema": 2},
            {**base, "turn_id": 1},
            {**base, "state": 1},
            {**base, "checkpoint_id": 1},
            {**base, "reason": 1},
            {**base, "verification_mutation_id": 1},
            {**base, "expected_current_fingerprint": 1},
            {**base, "rollback_attempt_id": 1},
            {**base, "verification_handoff_consumed": 1},
            {**base, "state": "unknown"},
        )
        for data in malformed:
            with self.subTest(data=data), self.assertRaises((TypeError, ValueError)):
                WorkspaceUndoAssociation.from_event_data(
                    "session-1",
                    data,
                    updated_at=datetime.now(UTC),
                )

    def test_result_projection_rejects_invalid_state_and_restore_combinations(self) -> None:
        with self.assertRaises(TypeError):
            WorkspaceUndoResult("available")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WorkspaceUndoResult(WorkspaceUndoState.AVAILABLE, reason="no_checkpoint")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WorkspaceUndoResult(WorkspaceUndoState.AVAILABLE, restored=1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            WorkspaceUndoResult(WorkspaceUndoState.AVAILABLE, restored=True)
        with self.assertRaises(ValueError):
            WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, restored=True)
        with self.assertRaises(ValueError):
            WorkspaceUndoResult(WorkspaceUndoState.ROLLED_BACK)


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

    def test_probe_treats_terminal_probe_failure_as_live(self) -> None:
        terminal = _ProbeTerminalSession(
            exit_code=None,
            error=OSError("terminal state unavailable"),
        )
        terminals = _ProbeTerminalManager((terminal,))

        live_background, live_terminal = _run(binding_has_live_mutators(None, terminals))

        self.assertFalse(live_background)
        self.assertTrue(live_terminal)
        self.assertEqual(terminal.wait_calls, 1)
        self.assertEqual(terminal.close_calls, 0)


class _ProbeBackgroundManager:
    def __init__(self, snapshots: tuple[object, ...]) -> None:
        self.snapshots = snapshots

    async def list(self) -> tuple[object, ...]:
        return self.snapshots


class _ProbeTerminalSession:
    def __init__(self, *, exit_code: int | None, error: Exception | None = None) -> None:
        self.exit_code = exit_code
        self.error = error
        self.wait_calls = 0
        self.close_calls = 0

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None:
        del timeout_seconds
        self.wait_calls += 1
        if self.error is not None:
            raise self.error
        return self.exit_code

    async def close(self) -> None:
        self.close_calls += 1


class _ProbeTerminalManager:
    def __init__(self, sessions: tuple[_ProbeTerminalSession, ...]) -> None:
        self.sessions = sessions

    async def list_sessions(self) -> tuple[_ProbeTerminalSession, ...]:
        return self.sessions
