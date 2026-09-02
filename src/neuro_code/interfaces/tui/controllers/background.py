from __future__ import annotations

from datetime import UTC, datetime

from neuro_code.domain.background_tasks.models import (
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWakePolicy,
    BackgroundWakeDecision,
    BackgroundWakeState,
)
from neuro_code.domain.execution import (
    AgentExecutionStatus,
    SessionExecutionRecord,
)
from neuro_code.domain.session_tasks import SessionTask
from neuro_code.interfaces.tui.contracts import SessionController
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.text import ui_text


class BackgroundControllerMixin(TuiAppControllerMixin):
    async def _poll_background_tasks(self) -> None:
        if self._task_controller is None or self._task_polling:
            return
        self._task_polling = True
        try:
            await self._ensure_background_wake_state()
            if not self._background_wake_state_loaded:
                return
            snapshots = await self._task_controller.list_background_tasks()
        except Exception:
            return
        finally:
            self._task_polling = False

        pending_completion_ids = {
            snapshot.task_id
            for snapshot in snapshots
            if snapshot.status.terminal and not snapshot.completion_reported
        }
        reconciled = self._background_wake_state.reconcile_visible_tasks(pending_completion_ids)
        if reconciled != self._background_wake_state:
            self._background_wake_state = reconciled
            self._pending_auto_wake_tasks.intersection_update(
                self._background_wake_state.pending_task_ids
            )

        for snapshot in snapshots:
            if not snapshot.status.terminal:
                continue
            if snapshot.task_id not in self._announced_terminal_tasks:
                self._announced_terminal_tasks.add(snapshot.task_id)
                category = (
                    "status"
                    if snapshot.status
                    in {BackgroundTaskStatus.COMPLETED, BackgroundTaskStatus.CANCELLED}
                    else "error"
                )
                self._write_entry(category, self._task_completion_message(snapshot))
                self._background_wake_state = self._background_wake_state.record_terminal_task(
                    snapshot.task_id,
                    enqueue=(
                        self._background_task_wake_policy is BackgroundTaskWakePolicy.ENABLED
                        and not snapshot.completion_reported
                    ),
                )
            if snapshot.task_id in self._background_wake_state.pending_task_ids:
                self._pending_auto_wake_tasks.add(snapshot.task_id)

        await self._persist_background_wake_state()

        if (
            self._background_task_wake_policy is BackgroundTaskWakePolicy.ENABLED
            and self._pending_auto_wake_tasks
            and not (self._turn_worker is not None and self._turn_worker.is_running)
        ):
            await self._start_background_wake()

    async def _ensure_background_wake_state(self) -> None:
        if self._background_wake_state_loaded:
            return
        controller = self._task_controller
        if controller is None:
            self._background_wake_state_loaded = True
            return
        try:
            state = await controller.load_background_wake_state()
        except Exception:
            self._background_wake_state = BackgroundWakeState()
            self._background_wake_state_loaded = True
            return
        recovered = state.recover_after_restart()
        self._background_wake_state = recovered
        self._announced_terminal_tasks = set(recovered.announced_task_ids)
        self._pending_auto_wake_tasks.clear()
        self._background_wake_state_loaded = True
        if recovered != state:
            await self._persist_background_wake_state()

    async def _persist_background_wake_state(self) -> None:
        controller = self._task_controller
        if controller is None or not self._background_wake_state_loaded:
            return
        try:
            await controller.save_background_wake_state(self._background_wake_state)
        except Exception:
            # Wake bookkeeping must never make a task poll or user turn fail.
            return

    def _reset_background_task_tracking(self) -> None:
        self._announced_terminal_tasks.clear()
        self._pending_auto_wake_tasks.clear()
        self._background_wake_state = BackgroundWakeState()
        self._background_wake_state_loaded = self._task_controller is None
        self._background_wake_active = False
        self._background_wake_task_ids = ()

    async def _start_background_wake(self) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            return
        if not self._pending_auto_wake_tasks:
            return
        now = datetime.now(UTC)
        decision = self._background_wake_state.decision(
            now,
            limits=self._background_wake_limits,
        )
        if decision is not BackgroundWakeDecision.ALLOW:
            return
        self._background_wake_state = self._background_wake_state.begin_wake(
            now,
            limits=self._background_wake_limits,
        )
        await self._persist_background_wake_state()
        self._pending_auto_wake_tasks.clear()
        self._background_wake_active = True
        self._background_wake_task_ids = ()
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._write_ui_entry("status", "background_wake.started")
        self._turn_worker = self.run_worker(
            self._run_background_wake(),
            name="background-auto-wake",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _complete_background_wake(self) -> None:
        """Commit wake consumption only after the model turn completed successfully.

        仅在模型回合成功完成后提交唤醒消费."""

        task_ids = self._background_wake_task_ids
        if not task_ids:
            self._background_wake_state = self._background_wake_state.abandon_wake(
                failed_at=datetime.now(UTC)
            )
        else:
            self._background_wake_state = self._background_wake_state.complete_wake(
                task_ids,
                completed_at=datetime.now(UTC),
            )
            self._pending_auto_wake_tasks.difference_update(task_ids)
        self._background_wake_active = False
        self._background_wake_task_ids = ()
        await self._persist_background_wake_state()

    def _task_summary(self, snapshot: BackgroundTaskSnapshot) -> str:
        exit_note = (
            ui_text(self._language, "tasks.exit", code=snapshot.exit_code)
            if snapshot.exit_code is not None
            else ""
        )
        truncation_note = ui_text(self._language, "tasks.truncated") if snapshot.truncated else ""
        started = snapshot.started_at.astimezone().strftime("%H:%M:%S")
        return ui_text(
            self._language,
            "tasks.summary",
            task_id=snapshot.task_id,
            status=ui_text(self._language, f"tasks.status.{snapshot.status.value}"),
            exit_note=exit_note,
            bytes=snapshot.total_output_bytes,
            truncated=truncation_note,
            started=started,
        )

    def _task_completion_message(self, snapshot: BackgroundTaskSnapshot) -> str:
        exit_note = (
            ui_text(self._language, "tasks.completion.exit", code=snapshot.exit_code)
            if snapshot.exit_code is not None
            else ""
        )
        return ui_text(
            self._language,
            f"tasks.completion.{snapshot.status.value}",
            task_id=snapshot.task_id,
            exit_note=exit_note,
        )

    def _session_task_summary(self, task: SessionTask) -> str:
        started = task.started_at.astimezone().strftime("%H:%M:%S")
        finished = (
            ui_text(
                self._language,
                "tasks.session.finished",
                finished=task.finished_at.astimezone().strftime("%H:%M:%S"),
            )
            if task.finished_at is not None
            else ""
        )
        plan_note = ""
        if task.plan_snapshot is not None:
            completed = sum(step.status.value == "completed" for step in task.plan_snapshot.steps)
            plan_note = ui_text(
                self._language,
                "tasks.session.plan_revision",
                fingerprint=task.plan_snapshot.fingerprint[:12],
                completed=completed,
                total=len(task.plan_snapshot.steps),
            )
        return ui_text(
            self._language,
            "tasks.session.summary",
            task_id=task.task_id,
            kind=ui_text(self._language, f"tasks.kind.{task.kind.value}"),
            status=ui_text(self._language, f"tasks.status.{task.status.value}"),
            started=started,
            finished=finished,
            plan=plan_note,
        )

    def _stopped_task_note(self, count: int) -> str:
        if count == 0:
            return ""
        if count == 1:
            return ui_text(self._language, "tasks.stopped_one")
        return ui_text(self._language, "tasks.stopped_many", count=count)

    def _session_execution_record(self) -> SessionExecutionRecord | None:
        controller = self._session_controller
        if controller is None:
            return None
        record = getattr(controller, "execution_record", None)
        return record if isinstance(record, SessionExecutionRecord) else None

    def _session_selection_owner(self) -> SessionController | None:
        """Return the narrow session-selection boundary used by the TUI.

        返回 TUI 使用的窄会话选择边界.

        ``session_controller`` remains an optional compatibility input because
        it also supplies the current execution-record projection to the TUI.
        Production bootstrap injects the narrower application service for
        selection operations while retaining that projection compatibility.

        ``session_controller`` 仍是可选兼容输入,因为它还向 TUI 提供当前执行记录投影.
        生产 bootstrap 为选择操作注入更窄的应用服务,同时保留该投影兼容性.
        """

        return self._session_selection_service or self._session_controller

    def _write_recoverable_resume_notice(
        self,
        record: SessionExecutionRecord | None,
    ) -> None:
        if record is None or not record.outcome.recoverable:
            return
        key_by_status = {
            AgentExecutionStatus.STUCK: "session.stuck_recoverable",
            AgentExecutionStatus.BUDGET_LIMITED: "session.budget_limited_recoverable",
        }
        key = key_by_status.get(record.outcome.status)
        if key is not None:
            self._write_ui_entry("recoverable", key)
