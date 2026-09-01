"""Bounded zero-tool model-generated Task DAG planning workflow."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from neuro_code.application.ports.model_planning import (
    ModelPlanningStore,
    ModelPlanningStoreError,
    PlanningAttemptClaim,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import EventSink
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.workflows.task_dag import CreateTaskDagRequest
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import TurnSource
from neuro_code.domain.model_planning import (
    MAX_MODEL_PLANNING_OBJECTIVE_BYTES,
    MAX_MODEL_PLANNING_RESPONSE_BYTES,
    MAX_PLANNING_CONTEXT_ITEM_BYTES,
    MAX_PLANNING_CONTEXT_ITEMS,
    MAX_PLANNING_CONTEXT_PROJECTED_BYTES,
    ModelDagProposal,
    PlanningAttempt,
    PlanningAttemptState,
    PlanningContextEnvelope,
    PlanningContextItem,
    PlanningProposalRecord,
    _truncate_utf8,
)
from neuro_code.domain.task_dag import TaskDag
from neuro_code.shared.errors import ConfigurationError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_MODEL_PLANNING_LEASE_SECONDS = 3_600.0
MAX_MODEL_PLANNING_PROMPT_BYTES = 40 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_objective(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_MODEL_PLANNING_OBJECTIVE_BYTES
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError("model planning objective is invalid")


def _validate_planning_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("model planning id is invalid")


def _bounded_redacted(value: str, *, limit: int, explicit_values: tuple[str, ...]) -> str:
    redacted = redact_sensitive_text(value, explicit_values=explicit_values)
    if len(redacted.encode("utf-8")) > limit:
        raise ConfigurationError("model planning response exceeds its bounded size")
    if not redacted.strip():
        raise ConfigurationError("model planning response became empty after redaction")
    return redacted


@dataclass(frozen=True, slots=True)
class RunModelDagPlanningRequest:
    """Explicit planning identity and parent objective."""

    planning_id: str
    objective: str

    def __post_init__(self) -> None:
        _validate_planning_id(self.planning_id)
        _validate_objective(self.objective)


@dataclass(frozen=True, slots=True)
class ModelDagPlanningResult:
    """The exact durable proposal and Task DAG produced by one plan."""

    planning_id: str
    attempt: PlanningAttempt
    proposal: PlanningProposalRecord
    dag: TaskDag


@runtime_checkable
class ModelPlanningDagService(Protocol):
    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag: ...


class ModelDagPlanningApplicationService:
    """Convert one parent objective into one immutable bounded Task DAG."""

    def __init__(
        self,
        store: ModelPlanningStore,
        dag_service: ModelPlanningDagService,
        *,
        parent_binding: ConversationBinding,
        planner_binding: ConversationBinding,
        session_store: SessionStore | None = None,
        clock: Callable[[], datetime] = _now,
        lease_seconds: float = 300.0,
        redaction_values: tuple[str, ...] = (),
        owner_id: str | None = None,
    ) -> None:
        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("model planning parent binding is required")
        if not isinstance(planner_binding, ConversationBinding):
            raise ConfigurationError("model planning binding is required")
        if not isinstance(dag_service, ModelPlanningDagService):
            raise ConfigurationError("model planning Task DAG service is invalid")
        parent_session_id = parent_binding.runner.session_id
        planner_session_id = planner_binding.runner.session_id
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("model planning parent session identity is missing")
        if not isinstance(planner_session_id, str) or not planner_session_id.strip():
            raise ConfigurationError("model planning session identity is missing")
        capabilities = planner_binding.capabilities
        if capabilities is None or (
            capabilities.allowed_tool_names
            or capabilities.mcp_tool_names
            or capabilities.mcp_server_names
            or capabilities.background_tasks
            or capabilities.filesystem_read
            or capabilities.filesystem_write
            or capabilities.bash
            or capabilities.terminal
            or capabilities.max_steps != 1
        ):
            raise ConfigurationError(
                "model planning binding must have exactly zero tools and one model step"
            )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not 1.0 <= float(lease_seconds) <= MAX_MODEL_PLANNING_LEASE_SECONDS
        ):
            raise ConfigurationError("model planning lease duration is invalid")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ConfigurationError("model planning owner identity is invalid")
        self._store = store
        self._dag_service = dag_service
        self._parent_binding = parent_binding
        self._planner_binding = planner_binding
        self._session_store = session_store
        self._clock = clock
        self._lease_seconds = float(lease_seconds)
        self._redaction_values = tuple(redaction_values)
        self._owner_id = owner_id or f"model-planner-owner-{uuid.uuid4().hex}"
        self._parent_session_id = parent_session_id
        self._planner_session_id = planner_session_id

    @property
    def planning_session_id(self) -> str:
        return self._planner_session_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    async def close(self) -> None:
        await self._planner_binding.close()

    async def run(
        self,
        request: RunModelDagPlanningRequest,
        *,
        sink: EventSink | None = None,
    ) -> ModelDagPlanningResult:
        del sink  # Planning is intentionally a private zero-tool workflow.
        if not isinstance(request, RunModelDagPlanningRequest):
            raise ValueError("model planning request must be canonical")
        objective = redact_sensitive_text(
            request.objective,
            explicit_values=self._redaction_values,
        )
        _validate_objective(objective)
        parent_session_id = self._require_parent_session_id()
        context = self._project_context(parent_session_id)
        objective_fingerprint = _sha256_text(objective)
        context_fingerprint = context.fingerprint
        now = self._clock().astimezone(UTC)
        existing = await self._load_attempt(request.planning_id)
        if existing is not None:
            self._verify_attempt_identity(
                existing,
                parent_session_id=parent_session_id,
                objective_fingerprint=objective_fingerprint,
                context_fingerprint=context_fingerprint,
            )
            await self._guard_existing_attempt(existing, now)

        candidate = PlanningAttempt(
            planning_id=request.planning_id,
            parent_session_id=parent_session_id,
            objective_fingerprint=objective_fingerprint,
            context_fingerprint=context_fingerprint,
            planner_session_id=self._planner_session_id,
            planner_turn_id=f"model-planning-turn-{uuid.uuid4().hex}",
            intended_dag_id=f"dag-planning-{uuid.uuid4().hex}",
            state=PlanningAttemptState.CLAIMED,
            owner_id=self._owner_id,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        claim = await self._claim(candidate, now)
        attempt = claim.attempt
        if not claim.acquired:
            return await self._recover(attempt)
        if attempt.planner_session_id != self._planner_session_id:
            raise ConfigurationError("model planning session identity does not match its binding")
        prompt = self._prompt(objective, context)
        fenced = await self._fence(attempt)
        try:
            result = await self._planner_binding.runner.run(
                prompt,
                sink=None,
                turn_id=fenced.planner_turn_id,
                turn_source=TurnSource.USER,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_indeterminate(fenced))
            raise
        except Exception as error:
            await self._mark_indeterminate(fenced)
            raise ConfigurationError(
                "model planning turn failed; automatic provider replay is disabled"
            ) from error
        raw_response = getattr(result, "response", None)
        if not isinstance(raw_response, str) or not raw_response.strip():
            await self._mark_indeterminate(fenced)
            raise ConfigurationError("model planning returned an empty response")
        try:
            response = _bounded_redacted(
                raw_response,
                limit=MAX_MODEL_PLANNING_RESPONSE_BYTES,
                explicit_values=self._redaction_values,
            )
        except ConfigurationError as error:
            await self._mark_stale(fenced)
            raise ConfigurationError(
                "observable model planning output exceeded its bounded response contract"
            ) from error
        try:
            committed = await self._store.mark_model_planning_model_committed(
                fenced.planning_id,
                owner_id=self._owner_id,
                planner_session_id=fenced.planner_session_id,
                planner_turn_id=fenced.planner_turn_id,
                model_response=response,
                updated_at=self._clock().astimezone(UTC),
            )
        except ModelPlanningStoreError as error:
            raise ConfigurationError(
                "model planning output durability failed; automatic provider replay is disabled"
            ) from error
        return await self._publish_from_model(committed)

    async def _recover(self, attempt: PlanningAttempt) -> ModelDagPlanningResult:
        if attempt.state is PlanningAttemptState.MODEL_COMMITTED:
            return await self._publish_from_model(attempt)
        if attempt.state in {
            PlanningAttemptState.PROPOSAL_PUBLISHED,
            PlanningAttemptState.DAG_PUBLISHED,
            PlanningAttemptState.COMPLETED,
        }:
            return await self._publish_from_proposal(attempt)
        if attempt.state in {
            PlanningAttemptState.CLAIMED,
            PlanningAttemptState.PROVIDER_FENCED,
        }:
            raise ConfigurationError(
                "another model planning controller owns this attempt; explicit recovery is required"
            )
        raise ConfigurationError(
            f"model planning attempt is {attempt.state.value}; recovery is required"
        )

    async def _publish_from_model(self, attempt: PlanningAttempt) -> ModelDagPlanningResult:
        response = attempt.model_response
        if response is None:
            raise ConfigurationError("durable model planning commitment has no response")
        try:
            proposal = ModelDagProposal.parse(response)
            record = PlanningProposalRecord(
                proposal_id=f"model-planning-proposal-{uuid.uuid4().hex}",
                planning_id=attempt.planning_id,
                parent_session_id=attempt.parent_session_id,
                intended_dag_id=attempt.intended_dag_id,
                objective_fingerprint=attempt.objective_fingerprint,
                context_fingerprint=attempt.context_fingerprint,
                proposal=proposal,
                created_at=self._clock().astimezone(UTC),
            )
            persisted = await self._store.publish_model_planning_proposal(
                record,
                owner_id=self._owner_id,
            )
        except (ValueError, ModelPlanningStoreError) as error:
            await self._mark_stale(attempt)
            raise ConfigurationError(
                "observable model planning output is not a valid immutable DAG proposal"
            ) from error
        return await self._publish_from_proposal(
            await self._require_attempt(attempt.planning_id),
            proposal_record=persisted,
        )

    async def _publish_from_proposal(
        self,
        attempt: PlanningAttempt,
        *,
        proposal_record: PlanningProposalRecord | None = None,
    ) -> ModelDagPlanningResult:
        proposal = proposal_record or await self._load_proposal(attempt.planning_id)
        if proposal is None:
            raise ConfigurationError("durable model planning proposal is missing")
        self._verify_proposal_identity(proposal, attempt)
        try:
            dag = await self._dag_service.create_task_dag(
                CreateTaskDagRequest(
                    attempt.intended_dag_id,
                    proposal.proposal.to_task_dag_nodes(),
                    max_parallel=proposal.proposal.max_parallel,
                )
            )
        except (ConfigurationError, ValueError, TypeError) as error:
            await self._mark_stale(attempt)
            raise ConfigurationError(
                "model planning proposal was rejected by canonical Task DAG validation"
            ) from error
        self._verify_dag(dag, attempt, proposal)
        if attempt.state is PlanningAttemptState.COMPLETED:
            return ModelDagPlanningResult(attempt.planning_id, attempt, proposal, dag)
        try:
            published = await self._store.mark_model_planning_dag_published(
                attempt.planning_id,
                owner_id=self._owner_id,
                dag_id=dag.dag_id,
                proposal_fingerprint=proposal.proposal_fingerprint,
                updated_at=self._clock().astimezone(UTC),
            )
            completed = await self._store.transition_model_planning_attempt(
                attempt.planning_id,
                expected_state=PlanningAttemptState.DAG_PUBLISHED,
                state=PlanningAttemptState.COMPLETED,
                updated_at=self._clock().astimezone(UTC),
            )
        except ModelPlanningStoreError as error:
            raise ConfigurationError(
                "model planning DAG publication identity could not be durably finalized"
            ) from error
        del published
        return ModelDagPlanningResult(attempt.planning_id, completed, proposal, dag)

    async def _load_attempt(self, planning_id: str) -> PlanningAttempt | None:
        try:
            return await self._store.get_model_planning_attempt(planning_id)
        except ModelPlanningStoreError as error:
            raise ConfigurationError("model planning durable lookup failed") from error

    async def _require_attempt(self, planning_id: str) -> PlanningAttempt:
        attempt = await self._load_attempt(planning_id)
        if attempt is None:
            raise ConfigurationError("model planning attempt disappeared")
        return attempt

    async def _load_proposal(self, planning_id: str) -> PlanningProposalRecord | None:
        try:
            return await self._store.get_model_planning_proposal(planning_id)
        except ModelPlanningStoreError as error:
            raise ConfigurationError("model planning proposal lookup failed") from error

    async def _claim(self, candidate: PlanningAttempt, now: datetime) -> PlanningAttemptClaim:
        try:
            return await self._store.claim_model_planning_attempt(candidate, now=now)
        except ModelPlanningStoreError as error:
            raise ConfigurationError("model planning durable claim failed") from error

    async def _fence(self, attempt: PlanningAttempt) -> PlanningAttempt:
        try:
            return await self._store.fence_model_planning_attempt(
                attempt.planning_id,
                owner_id=self._owner_id,
                planner_session_id=self._planner_session_id,
                planner_turn_id=attempt.planner_turn_id,
                updated_at=self._clock().astimezone(UTC),
            )
        except ModelPlanningStoreError as error:
            raise ConfigurationError(
                "model planning provider fence was lost; automatic replay is disabled"
            ) from error

    async def _guard_existing_attempt(self, attempt: PlanningAttempt, now: datetime) -> None:
        if attempt.state in {
            PlanningAttemptState.MODEL_COMMITTED,
            PlanningAttemptState.PROPOSAL_PUBLISHED,
            PlanningAttemptState.DAG_PUBLISHED,
            PlanningAttemptState.COMPLETED,
        }:
            return
        if attempt.state is PlanningAttemptState.PROVIDER_FENCED:
            await self._fail_if_turn_exists(attempt)
            raise ConfigurationError(
                "model planning provider fence exists; explicit recovery is required"
            )
        if attempt.state is not PlanningAttemptState.CLAIMED:
            return
        if attempt.lease_expires_at > now:
            raise ConfigurationError("another model planning controller owns this attempt")
        await self._fail_if_turn_exists(attempt)

    async def _fail_if_turn_exists(self, attempt: PlanningAttempt) -> None:
        if self._session_store is None:
            raise ConfigurationError(
                "model planning recovery inspection is unavailable; automatic replay is disabled"
            )
        try:
            turn_attempts = await self._session_store.load_turn_attempts(attempt.planner_session_id)
        except Exception as error:
            raise ConfigurationError(
                "model planning recovery inspection failed; automatic replay is disabled"
            ) from error
        if any(item.turn_id == attempt.planner_turn_id for item in turn_attempts):
            await self._mark_indeterminate(attempt)
            raise ConfigurationError(
                "model planning has observable provider-turn evidence; explicit recovery is required"
            )

    async def _mark_stale(self, attempt: PlanningAttempt) -> None:
        if attempt.state is PlanningAttemptState.STALE:
            return
        try:
            await self._store.transition_model_planning_attempt(
                attempt.planning_id,
                expected_state=attempt.state,
                state=PlanningAttemptState.STALE,
                updated_at=self._clock().astimezone(UTC),
            )
        except ModelPlanningStoreError:
            return

    async def _mark_indeterminate(self, attempt: PlanningAttempt) -> None:
        if attempt.state is PlanningAttemptState.INDETERMINATE:
            return
        try:
            await self._store.transition_model_planning_attempt(
                attempt.planning_id,
                expected_state=attempt.state,
                state=PlanningAttemptState.INDETERMINATE,
                updated_at=self._clock().astimezone(UTC),
            )
        except ModelPlanningStoreError:
            return

    def _require_parent_session_id(self) -> str:
        session_id = self._parent_binding.runner.session_id
        if not isinstance(session_id, str) or not session_id.strip():
            raise ConfigurationError("model planning parent binding session identity is missing")
        if session_id != self._parent_session_id:
            raise ConfigurationError("model planning parent binding identity changed")
        return session_id

    def _project_context(self, parent_session_id: str) -> PlanningContextEnvelope:
        items = self._parent_binding.runner.items
        selected: list[PlanningContextItem] = []
        projected_bytes = 0
        truncated = False
        for source_index, item in enumerate(items):
            if not isinstance(item, Message):
                continue
            if (
                item.role not in {Role.USER, Role.ASSISTANT}
                or item.synthetic_reason is not None
                or item.tool_calls
                or item.content_parts
                or item.reasoning_content is not None
                or item.name is not None
                or item.tool_call_id is not None
                or not item.content.strip()
            ):
                continue
            if len(selected) >= MAX_PLANNING_CONTEXT_ITEMS:
                truncated = True
                continue
            redacted = redact_sensitive_text(
                item.content,
                explicit_values=self._redaction_values,
            )
            remaining = max(0, MAX_PLANNING_CONTEXT_PROJECTED_BYTES - projected_bytes - 2_048)
            if remaining <= 0:
                truncated = True
                continue
            text, item_truncated = _truncate_utf8(
                redacted,
                min(MAX_PLANNING_CONTEXT_ITEM_BYTES, remaining),
            )
            if not text.strip():
                truncated = True
                continue
            selected.append(
                PlanningContextItem(
                    source_index=source_index,
                    role=item.role,
                    text=text,
                    truncated=item_truncated,
                )
            )
            projected_bytes += len(text.encode("utf-8"))
            truncated = truncated or item_truncated
        return PlanningContextEnvelope(
            parent_session_id=parent_session_id,
            source_item_count=len(items),
            items=tuple(selected),
            truncated=truncated,
        )

    def _prompt(self, objective: str, context: PlanningContextEnvelope) -> str:
        prompt = (
            "You are the Neuro Code bounded DAG Planner.\n"
            "You have no tools and cannot access files, shells, terminals, network, MCP, "
            "LSP, worktrees, checkpoints, workers, retries, merges, or sandbox settings.\n"
            "Return exactly one strict JSON object and no markdown.\n"
            "Allowed top-level fields are nodes, max_parallel, and reason. Each node must "
            "contain exactly id, prompt, and depends_on. Do not include authority, tool, "
            "capability, filesystem, provider, retry, merge, shell, or dynamic fields.\n"
            "Preserve node declaration order. Write each depends_on list in that same node "
            "declaration order; do not reorder or invent dependencies. The proposal is data "
            "only and will be checked by the canonical Task DAG service.\n"
            "OBJECTIVE:\n"
            f"{objective}\n\n"
            "PARENT_CONTEXT:\n"
            f"{context.render()}\n\n"
            "JSON_SCHEMA:\n"
            '{"nodes":[{"id":"node","prompt":"bounded worker task","depends_on":[]}],'
            '"max_parallel":1,"reason":"bounded decomposition"}'
        )
        if len(prompt.encode("utf-8")) > MAX_MODEL_PLANNING_PROMPT_BYTES:
            raise ConfigurationError("model planning prompt exceeds its bounded size")
        return prompt

    @staticmethod
    def _verify_attempt_identity(
        attempt: PlanningAttempt,
        *,
        parent_session_id: str,
        objective_fingerprint: str,
        context_fingerprint: str,
    ) -> None:
        if (
            attempt.parent_session_id != parent_session_id
            or attempt.objective_fingerprint != objective_fingerprint
            or attempt.context_fingerprint != context_fingerprint
        ):
            raise ConfigurationError(
                "model planning identity conflicts with the actual parent binding or input"
            )

    @staticmethod
    def _verify_proposal_identity(
        proposal: PlanningProposalRecord,
        attempt: PlanningAttempt,
    ) -> None:
        if (
            proposal.planning_id != attempt.planning_id
            or proposal.parent_session_id != attempt.parent_session_id
            or proposal.intended_dag_id != attempt.intended_dag_id
            or proposal.objective_fingerprint != attempt.objective_fingerprint
            or proposal.context_fingerprint != attempt.context_fingerprint
            or (
                attempt.proposal_fingerprint is not None
                and proposal.proposal_fingerprint != attempt.proposal_fingerprint
            )
        ):
            raise ConfigurationError("model planning proposal identity is inconsistent")

    @staticmethod
    def _verify_dag(
        dag: TaskDag,
        attempt: PlanningAttempt,
        proposal: PlanningProposalRecord,
    ) -> None:
        expected_nodes = proposal.proposal.to_task_dag_nodes()
        if (
            dag.dag_id != attempt.intended_dag_id
            or dag.parent_session_id != attempt.parent_session_id
            or dag.max_parallel != proposal.proposal.max_parallel
            or len(dag.nodes) != len(expected_nodes)
            or any(
                actual.definition_payload != expected.definition_payload
                for actual, expected in zip(dag.nodes, expected_nodes, strict=True)
            )
        ):
            raise ConfigurationError("published Task DAG does not match the immutable proposal")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


ModelPlanningApplicationService = ModelDagPlanningApplicationService
PlannerApplicationService = ModelDagPlanningApplicationService
RunPlannerRequest = RunModelDagPlanningRequest


__all__ = [
    "MAX_MODEL_PLANNING_LEASE_SECONDS",
    "MAX_MODEL_PLANNING_PROMPT_BYTES",
    "ModelDagPlanningApplicationService",
    "ModelDagPlanningResult",
    "ModelPlanningApplicationService",
    "ModelPlanningDagService",
    "PlannerApplicationService",
    "RunModelDagPlanningRequest",
    "RunPlannerRequest",
]
