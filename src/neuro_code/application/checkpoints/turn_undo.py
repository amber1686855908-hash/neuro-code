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
    CheckpointFingerprint,
    CheckpointId,
    RollbackAttemptId,
    RollbackState,
    SourceWorkspaceCheckpointGrant,
    workspace_projection_fingerprint,
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

    async def claim(
        self,
        expected: WorkspaceUndoAssociation,
        rolling_back: WorkspaceUndoAssociation,
    ) -> bool:
        try:
            return await self._store.claim_workspace_undo(expected, rolling_back)
        except AttributeError as error:
            raise WorkspaceUndoPreparationError(
                "session store does not support atomic workspace undo claims"
            ) from error

    async def seal(
        self,
        expected: WorkspaceUndoAssociation,
        sealed: WorkspaceUndoAssociation,
    ) -> bool:
        try:
            return await self._store.seal_workspace_undo(expected, sealed)
        except AttributeError as error:
            raise WorkspaceUndoPreparationError(
                "session store does not support atomic workspace undo sealing"
            ) from error

    async def has_open_turn(self, session_id: str) -> bool:
        return bool(await self._store.load_open_turn_attempts(session_id))


class TurnWorkspaceCheckpointCoordinator:
    """Coordinate one latest-only user undo target per durable session."""

    __slots__ = (
        "_background_tasks",
        "_capability_reason",
        "_checkpoint_service",
        "_enabled",
        "_ledger",
        "_lock",
        "_rollback_attempt_id_factory",
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
        rollback_attempt_id_factory: Callable[[], RollbackAttemptId] = RollbackAttemptId.new,
    ) -> None:
        self._checkpoint_service = checkpoint_service
        self._ledger = _WorkspaceUndoLedger(store)
        self._source_workspace = source_workspace.expanduser().resolve(strict=False)
        self._enabled = enabled
        self._background_tasks = background_tasks
        self._terminals = interactive_terminals
        self._rollback_attempt_id_factory = rollback_attempt_id_factory
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
            if (
                self._turn_association is not None
                and self._turn_association.state is WorkspaceUndoState.ROLLING_BACK
            ):
                raise WorkspaceUndoPreparationError(
                    "workspace undo rollback is already in progress"
                )

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
            if association.state is WorkspaceUndoState.ROLLING_BACK:
                return await self._resume_rolling_back(session_id, association)
            if association.expected_current_fingerprint is None:
                # A process may have died before the terminal workspace
                # fingerprint was durably sealed.  Never infer a protected
                # post-turn state from the checkpoint source projection.
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
                )
            checkpoint_id = association.checkpoint_id
            if checkpoint_id is None:
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.CHECKPOINT_FAILED,
                )
            try:
                attempt_id = self._rollback_attempt_id_factory()
                if not isinstance(attempt_id, RollbackAttemptId):
                    raise TypeError("rollback attempt factory must return RollbackAttemptId")
                rolling_back = WorkspaceUndoAssociation(
                    session_id=session_id,
                    turn_id=association.turn_id,
                    state=WorkspaceUndoState.ROLLING_BACK,
                    updated_at=datetime.now(UTC),
                    checkpoint_id=checkpoint_id,
                    expected_current_fingerprint=association.expected_current_fingerprint,
                    rollback_attempt_id=attempt_id,
                )
                claimed = await self._ledger.claim(association, rolling_back)
            except Exception:
                self._capability_reason = WorkspaceUndoReason.PERSISTENCE_FAILED
                self._turn_association = association
                return WorkspaceUndoResult(
                    WorkspaceUndoState.UNAVAILABLE,
                    WorkspaceUndoReason.PERSISTENCE_FAILED,
                )
            if not claimed:
                open_turns = await self._ledger.has_open_turn(session_id)
                current = await self._ledger.latest(session_id)
                reason = (
                    WorkspaceUndoReason.ACTIVE_TURN
                    if open_turns
                    else WorkspaceUndoReason.ROLLBACK_INDETERMINATE
                    if current is not None and current.state is WorkspaceUndoState.ROLLING_BACK
                    else WorkspaceUndoReason.CONCURRENT_MODIFICATION
                )
                self._turn_association = current
                return WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, reason)
            self._turn_association = rolling_back
            return await self._perform_rollback(
                session_id,
                rolling_back,
                mismatch_reason=WorkspaceUndoReason.WORKSPACE_CHANGED,
            )

    async def seal_turn(self, session_id: str | None, turn_id: str | None = None) -> None:
        """Durably seal the protected post-turn workspace projection.

        The seal is deliberately best effort.  If the terminal boundary is
        interrupted before inspection succeeds, the association remains
        unsealed and a later explicit undo fails closed instead of guessing
        that the checkpoint source is still the current workspace.
        """

        if not self._enabled or session_id is None or turn_id is None:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        if not isinstance(turn_id, str) or not turn_id:
            return
        async with self._lock:
            association = await self._ledger.latest(session_id)
            self._turn_key = (session_id, turn_id)
            self._turn_association = association
            if (
                association is None
                or association.turn_id != turn_id
                or association.state is not WorkspaceUndoState.AVAILABLE
                or association.expected_current_fingerprint is not None
            ):
                return
            try:
                if await self._ledger.has_open_turn(session_id):
                    return
                if self._source_grant is None or self._capability_reason is not None:
                    return
                checkpoint_id = association.checkpoint_id
                if checkpoint_id is None:
                    return
                checkpoint = await self._checkpoint_service.get(checkpoint_id)
                if checkpoint is None:
                    return
                grant = SourceWorkspaceCheckpointGrant.from_checkpoint(checkpoint)
                projection = await self._checkpoint_service.inspect(grant)
                expected = workspace_projection_fingerprint(grant, projection)
                sealed = replace(
                    association,
                    updated_at=datetime.now(UTC),
                    expected_current_fingerprint=expected,
                )
                if await self._ledger.seal(association, sealed):
                    self._turn_association = sealed
            except asyncio.CancelledError:
                raise
            except Exception:
                # No fingerprint means no safe later rollback.  Keeping the
                # durable AVAILABLE association is safe because undo rejects
                # it until a later terminal boundary can prove the state.
                return

    async def _resume_rolling_back(
        self,
        session_id: str,
        association: WorkspaceUndoAssociation,
    ) -> WorkspaceUndoResult:
        """Resume only the exact durable rollback attempt after interruption."""

        if (
            association.rollback_attempt_id is None
            or association.expected_current_fingerprint is None
        ):
            await self._prepare_unavailable(
                session_id,
                association.turn_id,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
                strict=False,
            )
            return WorkspaceUndoResult(
                WorkspaceUndoState.UNAVAILABLE,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            )
        return await self._perform_rollback(
            session_id,
            association,
            mismatch_reason=WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
        )

    async def _perform_rollback(
        self,
        session_id: str,
        association: WorkspaceUndoAssociation,
        *,
        mismatch_reason: WorkspaceUndoReason,
    ) -> WorkspaceUndoResult:
        checkpoint_id = association.checkpoint_id
        attempt_id = association.rollback_attempt_id
        expected = association.expected_current_fingerprint
        if checkpoint_id is None or attempt_id is None or expected is None:
            await self._prepare_unavailable(
                session_id,
                association.turn_id,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
                strict=False,
            )
            return WorkspaceUndoResult(
                WorkspaceUndoState.UNAVAILABLE,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
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
            current = await self._checkpoint_service.inspect(grant)
            current_fingerprint = workspace_projection_fingerprint(grant, current)
            if current_fingerprint == checkpoint.source_fingerprint:
                service_expected: CheckpointFingerprint | None = None
            elif current_fingerprint == expected:
                service_expected = expected
            else:
                if not await self._retire_source_rollback_attempt(
                    checkpoint_id,
                    grant,
                    attempt_id,
                ):
                    # A live owner or a failed identity proof means the old
                    # attempt is still the only safe durable owner.  Do not
                    # hide it behind UNAVAILABLE while it may still mutate.
                    self._turn_association = association
                    return WorkspaceUndoResult(
                        WorkspaceUndoState.UNAVAILABLE,
                        mismatch_reason,
                    )
                await self._prepare_unavailable(
                    session_id,
                    association.turn_id,
                    mismatch_reason,
                    strict=False,
                )
                return WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, mismatch_reason)
            attempt = await self._checkpoint_service.rollback(
                checkpoint_id,
                target=grant,
                attempt_id=attempt_id,
                expected_current_fingerprint=service_expected,
            )
            if attempt.state is not RollbackState.COMPLETED:
                raise WorkspaceCheckpointError(
                    "rollback did not reach a completed state",
                    kind=CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                )
            final_projection = await self._checkpoint_service.inspect(grant)
            final_fingerprint = workspace_projection_fingerprint(grant, final_projection)
            if final_fingerprint != checkpoint.source_fingerprint:
                raise WorkspaceCheckpointError(
                    "rollback completion did not prove the source projection",
                    kind=CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                )
        except WorkspaceCheckpointError as error:
            reason = self._rollback_reason(error)
            if error.kind == CheckpointFailureKind.ALREADY_ROLLING_BACK:
                # Do not replace a still-owned rollback with a terminal
                # session projection.  The owner must finish or be proven
                # dead before the exact attempt can be retired.
                self._turn_association = association
                return WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, reason)
            await self._prepare_unavailable(
                session_id,
                association.turn_id,
                reason,
                strict=False,
            )
            return WorkspaceUndoResult(WorkspaceUndoState.UNAVAILABLE, reason)
        except (TypeError, ValueError):
            await self._prepare_unavailable(
                session_id,
                association.turn_id,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
                strict=False,
            )
            return WorkspaceUndoResult(
                WorkspaceUndoState.UNAVAILABLE,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            )
        except Exception:
            await self._prepare_unavailable(
                session_id,
                association.turn_id,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
                strict=False,
            )
            return WorkspaceUndoResult(
                WorkspaceUndoState.UNAVAILABLE,
                WorkspaceUndoReason.ROLLBACK_INDETERMINATE,
            )
        rolled_back = WorkspaceUndoAssociation(
            session_id=session_id,
            turn_id=association.turn_id,
            state=WorkspaceUndoState.ROLLED_BACK,
            updated_at=datetime.now(UTC),
            checkpoint_id=checkpoint_id,
            verification_mutation_id=f"undo-{attempt_id.value}",
            expected_current_fingerprint=expected,
            rollback_attempt_id=attempt_id,
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

    async def _retire_source_rollback_attempt(
        self,
        checkpoint_id: CheckpointId,
        grant: SourceWorkspaceCheckpointGrant,
        attempt_id: RollbackAttemptId,
    ) -> bool:
        try:
            retired = await self._checkpoint_service.retire_source_rollback_attempt(
                attempt_id,
                checkpoint_id,
                target=grant,
            )
        except (AttributeError, TypeError, ValueError, WorkspaceCheckpointError):
            return False
        return retired.state in {RollbackState.COMPLETED, RollbackState.FAILED}

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
            return
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
        if str(error.kind) == str(CheckpointFailureKind.CONCURRENT_MODIFICATION):
            return WorkspaceUndoReason.WORKSPACE_CHANGED
        if str(error.kind) == str(CheckpointFailureKind.ALREADY_ROLLING_BACK):
            return WorkspaceUndoReason.ROLLBACK_INDETERMINATE
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
