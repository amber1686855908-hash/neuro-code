"""Application owner for normal-turn workspace checkpoint and undo policy.

This module coordinates the existing checkpoint application service with the
normal tool pipeline.  It owns only the latest session association and its
bounded policy; Git projection capture/restoration remains owned by
``WorkspaceCheckpointApplicationService``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.checkpoints.service import WorkspaceCheckpointApplicationService
from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.checkpoints import (
    CheckpointFailureKind,
    WorkspaceCheckpointError,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.terminal import InteractiveTerminalManager
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskStatus
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    SourceWorkspaceCheckpointGrant,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.workspace_undo import (
    WorkspaceUndoAssociation,
    WorkspaceUndoReason,
    WorkspaceUndoResult,
    WorkspaceUndoState,
)
from neuro_code.shared.errors import ToolError

_MUTATION_OPERATIONS = frozenset(
    {
        FilesystemAccessOperation.CREATE,
        FilesystemAccessOperation.UPDATE,
        FilesystemAccessOperation.DELETE,
        FilesystemAccessOperation.MOVE,
    }
)

WorkspaceUndoEventSink = Callable[[AgentEventKind, dict[str, object]], Awaitable[None]]


class WorkspaceUndoPreparationError(ToolError):
    """Fail closed when an unsafe-mutation invalidation cannot be durable."""


@dataclass(frozen=True, slots=True)
class WorkspaceVerificationHandoff:
    """One retry-safe handoff of the undo mutation fact to the next turn."""

    _coordinator: TurnWorkspaceCheckpointCoordinator
    session_id: str
    mutation_id: str
    _committed: bool = False

    async def commit(self) -> None:
        if self._committed:
            return
        await self._coordinator._commit_verification_handoff(self)
        object.__setattr__(self, "_committed", True)


class _WorkspaceUndoLedger:
    """Persist and load the latest undo projection through SessionStore events."""

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def latest(self, session_id: str) -> WorkspaceUndoAssociation | None:
        latest: WorkspaceUndoAssociation | None = None
        latest_sequence = -1
        for raw in await self._store.load_events(session_id):
            if not isinstance(raw, Mapping):
                raise WorkspaceUndoPreparationError("workspace undo durable event is malformed")
            if raw.get("kind") != AgentEventKind.WORKSPACE_UNDO_STATE.value:
                continue
            data = raw.get("data")
            created_at = raw.get("created_at")
            try:
                if isinstance(created_at, datetime):
                    timestamp = created_at
                elif isinstance(created_at, str):
                    timestamp = datetime.fromisoformat(created_at)
                else:
                    raise ValueError("workspace undo event timestamp is malformed")
                if timestamp.tzinfo is None:
                    raise ValueError("workspace undo event timestamp must be timezone-aware")
                sequence = raw.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                    raise ValueError("workspace undo durable sequence is malformed")
                candidate = WorkspaceUndoAssociation.from_event_data(
                    session_id,
                    data,
                    updated_at=timestamp,
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise WorkspaceUndoPreparationError(
                    "workspace undo durable state is malformed"
                ) from error
            if latest is None or sequence > latest_sequence:
                latest = candidate
                latest_sequence = sequence
        return latest

    async def append(
        self,
        association: WorkspaceUndoAssociation,
        *,
        event_sink: WorkspaceUndoEventSink | None = None,
    ) -> None:
        if event_sink is not None:
            await event_sink(
                AgentEventKind.WORKSPACE_UNDO_STATE,
                association.to_event_data(),
            )
            return
        sequence = await self._store.next_event_sequence(association.session_id)
        await self._store.append_event(
            association.session_id,
            AgentEvent.create(
                sequence,
                AgentEventKind.WORKSPACE_UNDO_STATE,
                association.to_event_data(),
            ),
        )


class TurnWorkspaceCheckpointCoordinator:
    """Coordinate one latest-only user undo target per durable session."""

    __slots__ = (
        "_background_tasks",
        "_capability_reason",
        "_checkpoint_service",
        "_enabled",
        "_ledger",
        "_lock",
        "_source_grant",
        "_source_workspace",
        "_terminals",
        "_turn_association",
        "_turn_key",
    )

    def __init__(
        self,
        *,
        checkpoint_service: WorkspaceCheckpointApplicationService,
        store: SessionStore,
        source_workspace: Path,
        enabled: bool = True,
        background_tasks: BackgroundTaskManager | None = None,
        interactive_terminals: InteractiveTerminalManager | None = None,
    ) -> None:
        self._checkpoint_service = checkpoint_service
        self._ledger = _WorkspaceUndoLedger(store)
        self._source_workspace = source_workspace.expanduser().resolve(strict=False)
        self._enabled = enabled
        self._background_tasks = background_tasks
        self._terminals = interactive_terminals
        self._source_grant: SourceWorkspaceCheckpointGrant | None = None
        self._capability_reason: WorkspaceUndoReason | None = None
        self._lock = asyncio.Lock()
        self._turn_key: tuple[str, str] | None = None
        self._turn_association: WorkspaceUndoAssociation | None = None

    async def initialize(self) -> None:
        """Best-effort capability initialization; normal Agent execution continues."""

        if not self._enabled:
            return
        try:
            await self._checkpoint_service.initialize()
            self._source_grant = await self._checkpoint_service.authorize_source_workspace(
                self._source_workspace
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._capability_reason = WorkspaceUndoReason.CAPABILITY_UNAVAILABLE

    async def prepare(
        self,
        *,
        session_id: str | None,
        turn_id: str | None,
        plan: FilesystemAccessPlan | None,
        client_file_system: object | None,
        client_terminal: object | None,
        event_sink: WorkspaceUndoEventSink | None = None,
    ) -> None:
        """Make undo state durable before an eligible mutation can start."""

        if not self._enabled or session_id is None or turn_id is None:
            return
        if not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str):
            return
        async with self._lock:
            if self._turn_key != (session_id, turn_id):
                self._turn_key = (session_id, turn_id)
                self._turn_association = await self._ledger.latest(session_id)
                if self._turn_association is not None and self._turn_association.turn_id != turn_id:
                    self._turn_association = None

            if (
                self._turn_association is not None
                and self._turn_association.state is WorkspaceUndoState.UNAVAILABLE
            ):
                return

            eligible, reason, paths = await self._classify_target(
                plan,
                client_file_system=client_file_system,
                client_terminal=client_terminal,
            )
            if (
                eligible
                and self._turn_association is not None
                and self._turn_association.state is WorkspaceUndoState.AVAILABLE
            ):
                return
            if not eligible:
                await self._prepare_unavailable(
                    session_id,
                    turn_id,
                    reason or WorkspaceUndoReason.UNBOUNDED_MUTATION,
                    strict=True,
                    event_sink=event_sink,
                )
                return
            if (
                self._turn_association is not None
                and self._turn_association.state is WorkspaceUndoState.ROLLED_BACK
            ):
                return

            if self._source_grant is None or self._capability_reason is not None:
                await self._prepare_unavailable(
                    session_id,
                    turn_id,
                    self._capability_reason or WorkspaceUndoReason.CAPABILITY_UNAVAILABLE,
                    strict=True,
                    event_sink=event_sink,
                )
                return
            if paths:
                try:
                    ignored = await self._checkpoint_service.ignored_source_paths(
                        self._source_grant,
                        paths,
                    )
                except Exception:
                    await self._prepare_unavailable(
                        session_id,
                        turn_id,
                        WorkspaceUndoReason.CHECKPOINT_FAILED,
                        strict=True,
                        event_sink=event_sink,
                    )
                    return
                if ignored:
                    await self._prepare_unavailable(
                        session_id,
                        turn_id,
                        WorkspaceUndoReason.IGNORED_TARGET,
                        strict=True,
                        event_sink=event_sink,
                    )
                    return
            try:
                checkpoint = await self._checkpoint_service.create(
                    CheckpointCreateRequest(self._source_grant)
                )
            except WorkspaceCheckpointError as error:
                await self._prepare_unavailable(
                    session_id,
                    turn_id,
                    self._checkpoint_reason(error),
                    strict=True,
                    event_sink=event_sink,
                )
                return
            association = WorkspaceUndoAssociation(
                session_id=session_id,
                turn_id=turn_id,
                state=WorkspaceUndoState.AVAILABLE,
                updated_at=datetime.now(UTC),
                checkpoint_id=checkpoint.checkpoint_id,
            )
            try:
                await self._ledger.append(association, event_sink=event_sink)
            except Exception as error:
                # A READY artifact without a durable session association is
                # not a safe undo contract.  Stop before the mutation rather
                # than allowing the caller to proceed with an untracked
                # checkpoint.
                self._capability_reason = WorkspaceUndoReason.PERSISTENCE_FAILED
                self._turn_association = None
                raise WorkspaceUndoPreparationError(
                    "workspace undo checkpoint state could not be persisted"
                ) from error
            self._turn_association = association

    async def undo(
        self,
        session_id: str | None,
        *,
        live_terminal: bool = False,
        live_background: bool = False,
    ) -> WorkspaceUndoResult:
        """Restore the latest safe checkpoint while the binding is idle."""

        if not self._enabled or session_id is None:
            return WorkspaceUndoResult(
                WorkspaceUndoState.UNAVAILABLE,
                WorkspaceUndoReason.CAPABILITY_UNAVAILABLE,
            )
        async with self._lock:
            association = await self._ledger.latest(session_id)
            self._turn_key = None
            self._turn_association = association
            if association is None:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.NO_CHECKPOINT,
                )
            if association.state is WorkspaceUndoState.UNAVAILABLE:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    association.reason,
                )
            if association.state is WorkspaceUndoState.ROLLED_BACK:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.ALREADY_ROLLED_BACK,
                )
            if live_terminal or live_background:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.LIVE_MUTATOR,
                )
            rollback_guard = WorkspaceUndoAssociation(
                session_id=session_id,
                turn_id=association.turn_id,
                state=WorkspaceUndoState.UNAVAILABLE,
                updated_at=datetime.now(UTC),
                reason=WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            )
            try:
                # Consume the AVAILABLE claim before entering the existing
                # rollback state machine.  If the process dies or the final
                # association write fails, a restart must not retry a
                # potentially destructive restore.
                await self._ledger.append(rollback_guard)
            except Exception:
                self._capability_reason = WorkspaceUndoReason.PERSISTENCE_FAILED
                self._turn_association = association
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.PERSISTENCE_FAILED,
                )
            self._turn_association = rollback_guard
            checkpoint_id = association.checkpoint_id
            if checkpoint_id is None:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.CHECKPOINT_FAILED,
                )
            checkpoint = await self._checkpoint_service.get(checkpoint_id)
            if checkpoint is None:
                await self._prepare_unavailable(
                    session_id,
                    association.turn_id,
                    WorkspaceUndoReason.CHECKPOINT_FAILED,
                    strict=False,
                )
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.CHECKPOINT_FAILED,
                )
            try:
                grant = SourceWorkspaceCheckpointGrant.from_checkpoint(checkpoint)
                attempt = await self._checkpoint_service.rollback(
                    checkpoint_id,
                    target=grant,
                )
            except WorkspaceCheckpointError as error:
                reason = self._rollback_reason(error)
                await self._prepare_unavailable(
                    session_id,
                    association.turn_id,
                    reason,
                    strict=False,
                )
                return WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, reason)
            mutation_id = f"undo-{attempt.attempt_id.value}"
            rolled_back = WorkspaceUndoAssociation(
                session_id=session_id,
                turn_id=association.turn_id,
                state=WorkspaceUndoState.ROLLED_BACK,
                updated_at=datetime.now(UTC),
                checkpoint_id=checkpoint_id,
                verification_mutation_id=mutation_id,
            )
            try:
                await self._ledger.append(rolled_back)
            except Exception:
                self._capability_reason = WorkspaceUndoReason.PERSISTENCE_FAILED
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.PERSISTENCE_FAILED,
                )
            self._turn_association = rolled_back
            return WorkspaceUndoResult(WorkspaceUndoState.ROLLED_BACK, restored=True)

    async def prepare_verification_handoff(
        self,
        session_id: str | None,
    ) -> WorkspaceVerificationHandoff | None:
        if not self._enabled or session_id is None:
            return None
        async with self._lock:
            association = await self._ledger.latest(session_id)
            if (
                association is None
                or association.state is not WorkspaceUndoState.ROLLED_BACK
                or association.verification_mutation_id is None
                or association.verification_handoff_consumed
            ):
                return None
            return WorkspaceVerificationHandoff(
                self,
                session_id,
                association.verification_mutation_id,
            )

    async def _commit_verification_handoff(self, handoff: WorkspaceVerificationHandoff) -> None:
        async with self._lock:
            association = await self._ledger.latest(handoff.session_id)
            if association is None or association.verification_mutation_id != handoff.mutation_id:
                return
            if association.verification_handoff_consumed:
                return
            await self._ledger.append(replace(association, verification_handoff_consumed=True))
            self._turn_association = replace(association, verification_handoff_consumed=True)

    async def _classify_target(
        self,
        plan: FilesystemAccessPlan | None,
        *,
        client_file_system: object | None,
        client_terminal: object | None,
    ) -> tuple[bool, WorkspaceUndoReason | None, tuple[str, ...]]:
        if client_file_system is not None or client_terminal is not None or plan is None:
            return False, WorkspaceUndoReason.UNBOUNDED_MUTATION, ()
        if self._source_grant is None:
            return True, None, ()
        paths: list[str] = []
        for target in plan.targets:
            if (
                not target.is_primary_workspace
                or target.owning_workspace_root != self._source_grant.path
                or target.operation not in _MUTATION_OPERATIONS
                or target.contains_link_like_component
            ):
                return False, WorkspaceUndoReason.UNBOUNDED_MUTATION, ()
            try:
                relative = target.canonical_path.relative_to(self._source_grant.path)
            except ValueError:
                return False, WorkspaceUndoReason.UNBOUNDED_MUTATION, ()
            rendered = relative.as_posix()
            if not rendered or rendered == ".":
                return False, WorkspaceUndoReason.UNBOUNDED_MUTATION, ()
            paths.append(rendered)
        return True, None, tuple(dict.fromkeys(paths))

    async def _prepare_unavailable(
        self,
        session_id: str,
        turn_id: str,
        reason: WorkspaceUndoReason,
        *,
        strict: bool,
        event_sink: WorkspaceUndoEventSink | None = None,
    ) -> None:
        association = WorkspaceUndoAssociation(
            session_id=session_id,
            turn_id=turn_id,
            state=WorkspaceUndoState.UNAVAILABLE,
            updated_at=datetime.now(UTC),
            reason=reason,
        )
        try:
            await self._ledger.append(association, event_sink=event_sink)
        except Exception as error:
            if strict:
                raise WorkspaceUndoPreparationError(
                    "workspace undo safety state could not be persisted"
                ) from error
            self._capability_reason = WorkspaceUndoReason.PERSISTENCE_FAILED
        self._turn_association = association

    @staticmethod
    def _checkpoint_reason(error: WorkspaceCheckpointError) -> WorkspaceUndoReason:
        if error.kind == CheckpointFailureKind.CHECKPOINT_TOO_LARGE:
            return WorkspaceUndoReason.CHECKPOINT_TOO_LARGE
        if error.kind == CheckpointFailureKind.UNSUPPORTED_WORKSPACE_STATE:
            return WorkspaceUndoReason.UNSUPPORTED_WORKSPACE
        return WorkspaceUndoReason.CHECKPOINT_FAILED

    @staticmethod
    def _rollback_reason(error: WorkspaceCheckpointError) -> WorkspaceUndoReason:
        if str(error.kind) == str(CheckpointFailureKind.HEAD_MISMATCH):
            return WorkspaceUndoReason.HEAD_CHANGED
        if str(error.kind) in {
            str(CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED),
            str(CheckpointFailureKind.COMMAND_FAILED),
        }:
            return WorkspaceUndoReason.ROLLBACK_INDETERMINATE
        return WorkspaceUndoReason.ROLLBACK_FAILED


async def binding_has_live_mutators(
    background_tasks: BackgroundTaskManager | None,
    terminals: InteractiveTerminalManager | None,
) -> tuple[bool, bool]:
    live_background = False
    if background_tasks is not None:
        snapshots = await background_tasks.list()
        live_background = any(
            snapshot.status is BackgroundTaskStatus.RUNNING for snapshot in snapshots
        )
    live_terminal = False
    if terminals is not None:
        for session in await terminals.list_sessions():
            try:
                exit_code = await session.wait(timeout_seconds=0)
            except Exception:
                live_terminal = True
                break
            if exit_code is None:
                live_terminal = True
                break
    return live_background, live_terminal


__all__ = [
    "TurnWorkspaceCheckpointCoordinator",
    "WorkspaceUndoPreparationError",
    "WorkspaceVerificationHandoff",
    "binding_has_live_mutators",
]
