from __future__ import annotations

from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipActionRequest,
)
from neuro_code.application.sessions.subagent_queries import (
    ListSubagentRelationshipsRequest,
    SubagentRelationshipAction,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.session_tasks import SessionTaskKind, SessionTaskStatus
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import _TASK_LIST_LIMIT
from neuro_code.interfaces.tui.text import ui_text


class TaskControllerMixin(TuiAppControllerMixin):
    async def _run_queued_task(self, task_id: str) -> None:
        controller = self._plan_controller
        task_controller = self._session_task_controller
        if controller is None or task_controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        try:
            task = await task_controller.get_session_task(task_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if task is None:
            self._write_ui_entry("error", "tasks.run.not_found", task_id=task_id)
            return
        if task.kind is not SessionTaskKind.PLAN_EXECUTION:
            self._write_ui_entry("error", "tasks.run.not_plan", task_id=task_id)
            return
        if task.status is not SessionTaskStatus.QUEUED:
            self._write_ui_entry("error", "tasks.run.not_queued", task_id=task_id)
            return
        await self._apply_interaction_mode(InteractionMode.ACCEPT_EDITS)
        if self._interaction_mode is not InteractionMode.ACCEPT_EDITS:
            return
        self._write_ui_entry("user", "plan.task_execution_user", task_id=task_id)
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_queued_plan(task_id),
            name="agent-queued-plan-execution",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _show_tasks(self) -> None:
        if self._task_controller is None and self._session_task_controller is None:
            self._write_ui_entry("error", "tasks.unavailable")
            return
        try:
            snapshots = (
                await self._task_controller.list_background_tasks()
                if self._task_controller is not None
                else ()
            )
            session_tasks = (
                await self._session_task_controller.list_session_tasks()
                if self._session_task_controller is not None
                else ()
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not snapshots and not session_tasks:
            self._write_ui_entry("status", "tasks.none")
            return

        visible = snapshots[-_TASK_LIST_LIMIT:]
        omitted = len(snapshots) - len(visible)
        lines = [self._task_summary(snapshot) for snapshot in visible]
        lines.extend(self._session_task_summary(task) for task in session_tasks[:_TASK_LIST_LIMIT])
        if omitted:
            lines.insert(0, ui_text(self._language, "tasks.omitted", count=omitted))
        self._write_ui_entry(
            "system",
            "tasks.heading",
            lines="\n".join(lines),
        )

    async def _show_session_task(self, task_id: str) -> None:
        if not task_id:
            self._write_ui_entry("error", "tasks.view.usage")
            return
        controller = self._session_task_controller
        if controller is None:
            self._write_ui_entry("error", "tasks.unavailable")
            return
        try:
            task = await controller.get_session_task(task_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if task is None:
            self._write_ui_entry("status", "tasks.view.not_found", task_id=task_id)
            return
        if task.plan_snapshot is None:
            self._write_ui_entry("status", "tasks.view.no_plan", task_id=task.task_id)
            return

        plan = task.plan_snapshot
        finished = (
            ui_text(
                self._language,
                "tasks.session.finished",
                finished=task.finished_at.astimezone().strftime("%H:%M:%S"),
            )
            if task.finished_at is not None
            else ""
        )
        lines = [
            ui_text(self._language, "tasks.view.heading", task_id=task.task_id),
            ui_text(
                self._language,
                "tasks.view.lifecycle",
                kind=ui_text(self._language, f"tasks.kind.{task.kind.value}"),
                status=ui_text(self._language, f"tasks.status.{task.status.value}"),
                started=task.started_at.astimezone().strftime("%H:%M:%S"),
                finished=finished,
            ),
            ui_text(
                self._language,
                "tasks.view.revision",
                fingerprint=plan.fingerprint,
            ),
            ui_text(self._language, "tasks.view.snapshot"),
        ]
        if plan.explanation is not None:
            lines.append(
                ui_text(self._language, "tasks.view.purpose", explanation=plan.explanation)
            )
        lines.extend(
            ui_text(
                self._language,
                "tasks.view.step",
                index=index,
                status=ui_text(self._language, f"plan.status.{step.status.value}"),
                step=step.step,
            )
            for index, step in enumerate(plan.steps, start=1)
        )
        lines.append(ui_text(self._language, "tasks.view.reference"))
        self._write_entry("system", "\n".join(lines))

    async def _show_subagent_relationships(self) -> None:
        """Render bounded child metadata without executing lifecycle actions.

        在不执行生命周期动作的前提下渲染有界子代理元数据.
        """

        controller = self._subagent_relationship_query
        if controller is None:
            self._write_ui_entry("error", "subagents.unavailable")
            return
        session_id = self._runner.session_id
        if session_id is None:
            self._write_ui_entry("error", "subagents.session_required")
            return
        try:
            relationships = await controller.list_subagent_relationships(
                ListSubagentRelationshipsRequest(session_id),
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not relationships:
            self._write_ui_entry("status", "subagents.none")
            return

        lines = [
            ui_text(
                self._language,
                "subagents.summary",
                task_id=relationship.parent_task_id,
                child_session_id=relationship.child_session_id,
                provider=relationship.child_provider,
                model=relationship.child_model,
                status=ui_text(
                    self._language,
                    f"tasks.status.{relationship.task_status.value}",
                ),
                created=relationship.created_at.astimezone().strftime("%H:%M:%S"),
                updated=relationship.child_updated_at.astimezone().strftime("%H:%M:%S"),
                actions=(
                    ", ".join(action.value for action in relationship.available_actions)
                    or ui_text(self._language, "subagents.actions.none")
                ),
            )
            for relationship in relationships
        ]
        self._write_ui_entry(
            "system",
            "subagents.heading",
            lines="\n".join(lines),
        )

    async def _run_subagent_relationship_action(self, arguments: str) -> None:
        """Run one explicit relationship action through the application owner.

        通过应用 owner 执行一次明确的关系生命周期动作.

        The TUI only parses the small command shape and projects the bounded
        result.  It does not touch SQLite or infer ownership from a child ID.
        TUI 只解析精简命令形状并投影有界结果,不会直接访问 SQLite 或仅凭子会话 ID 推断归属.
        """

        controller = self._subagent_relationship_lifecycle
        if controller is None:
            self._write_ui_entry("error", "subagents.actions_unavailable")
            return
        parent_session_id = self._runner.session_id
        if parent_session_id is None:
            self._write_ui_entry("error", "subagents.session_required")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return

        action_text, separator, task_id = arguments.partition(" ")
        if not separator or not action_text.strip() or not task_id.strip():
            self._write_ui_entry("error", "subagents.actions_usage")
            return
        try:
            action = SubagentRelationshipAction(action_text.casefold())
        except ValueError:
            self._write_ui_entry("error", "subagents.actions_usage")
            return
        try:
            result = await controller.execute(
                SubagentRelationshipActionRequest(
                    parent_session_id=parent_session_id,
                    parent_task_id=task_id.strip(),
                    action=action,
                )
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        if result.action is SubagentRelationshipAction.RESUME:
            await self._apply_session_selection(result.child_session_id)
        elif result.action is SubagentRelationshipAction.FORK:
            assert result.forked_session_id is not None
            self._write_ui_entry(
                "status",
                "subagents.actions.forked",
                session_id=result.forked_session_id,
            )
        else:
            self._write_ui_entry(
                "status",
                "subagents.actions.deleted",
                session_id=result.child_session_id,
            )
