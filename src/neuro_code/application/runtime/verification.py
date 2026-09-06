"""Typed verification evidence and per-task freshness state.

Verification is an application-runtime concern: tool adapters report a bounded
terminal result, while this module decides whether that result is still valid
after later workspace mutations.  It deliberately does not execute tests or
discover test suites.

定义类型化的验证证据以及按任务维护的 freshness 状态。

验证属于应用运行时职责: 工具适配器报告有界的终态结果, 本模块判断后续工作区
修改是否使该结果失效。本模块不会执行测试, 也不会发现测试套件。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from neuro_code.domain.execution import (
    MAX_VERIFICATION_REQUIREMENTS,
    ProgressKind,
    RequirementStrength,
    VerificationRequirementsSnapshot,
    require_requirement_id,
)
from neuro_code.domain.permissions import BashCommandFamily, classify_bash_command_family
from neuro_code.shared.redaction import redact_sensitive_text

MAX_VERIFICATION_EVIDENCE_ITEMS = 4
MAX_VERIFICATION_SCOPE_ITEMS = 8
MAX_VERIFICATION_SCOPE_BYTES = 160
MAX_VERIFICATION_SUMMARY_BYTES = 512
MAX_VERIFICATION_TOOL_NAME_BYTES = 80
MAX_VERIFICATION_COVERED_REQUIREMENTS = MAX_VERIFICATION_REQUIREMENTS
MAX_VERIFICATION_BLOCKER_DETAIL_BYTES = 400
MAX_VERIFICATION_EVALUATION_REASON_BYTES = 240
MAX_VERIFICATION_EVALUATIONS = MAX_VERIFICATION_REQUIREMENTS
MAX_VERIFICATION_REFERENCE_BYTES = 64


class VerificationOutcome(StrEnum):
    """Whether one completed verification reported success or failure."""

    SUCCESS = "success"
    FAILURE = "failure"


class VerificationState(StrEnum):
    """The current verification state of one logical runtime task."""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    NOT_APPLICABLE = "not_applicable"


class VerificationFreshness(StrEnum):
    """Whether an evidence item was produced for the current workspace generation."""

    CURRENT = "current"
    STALE = "stale"


class VerificationBlockReason(StrEnum):
    """A typed fact explaining why required verification could not run."""

    USER_CONSTRAINT = "user_constraint"
    PERMISSION_DENIED = "permission_denied"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    POLICY_RESTRICTION = "policy_restriction"


class RequirementEvaluationState(StrEnum):
    """The bounded evaluation of one active verification requirement."""

    SATISFIED = "satisfied"
    FAILED = "failed"
    NO_EVIDENCE = "no_evidence"
    STALE = "stale"
    BLOCKED = "blocked"


def _reference_digest(kind: str, value: object) -> str:
    payload = json.dumps(
        [kind, value],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_utf8(value: str, *, limit: int, suffix: str = "…") -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= limit:
        return encoded[:limit].decode("utf-8", errors="ignore")
    prefix = encoded[: limit - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"


def _safe_bounded_text(
    value: str,
    *,
    limit: int,
    field_name: str,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in value or any(
        ord(character) < 32 and character not in "\n\t" for character in value
    ):
        raise ValueError(f"{field_name} must not contain control characters")
    return _bounded_utf8(redact_sensitive_text(value), limit=limit)


def _validate_tool_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > MAX_VERIFICATION_TOOL_NAME_BYTES
    ):
        raise ValueError("verification tool_name must be a bounded canonical name")
    return value


def _validated_requirement_ids(
    values: Sequence[str],
    *,
    field_name: str,
    require_non_empty: bool = False,
) -> tuple[str, ...]:
    items = tuple(values)
    if require_non_empty and not items:
        raise ValueError(f"{field_name} must contain at least one requirement ID")
    if len(items) > MAX_VERIFICATION_COVERED_REQUIREMENTS:
        raise ValueError(
            f"{field_name} must contain at most {MAX_VERIFICATION_COVERED_REQUIREMENTS} IDs"
        )
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} must contain strings")
    if len(set(items)) != len(items):
        raise ValueError(f"{field_name} must not contain duplicate IDs")
    return tuple(require_requirement_id(item, field_name=field_name) for item in items)


def _validate_reference(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > MAX_VERIFICATION_REFERENCE_BYTES
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded lowercase SHA-256 reference")
    return value


@dataclass(frozen=True, slots=True)
class VerificationBlocker:
    """A typed, bounded fact that verification could not be performed."""

    affected_requirement_ids: tuple[str, ...]
    reason: VerificationBlockReason
    workspace_generation: int
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "affected_requirement_ids",
            _validated_requirement_ids(
                self.affected_requirement_ids,
                field_name="verification blocker affected_requirement_ids",
                require_non_empty=True,
            ),
        )
        if not isinstance(self.reason, VerificationBlockReason):
            raise TypeError("verification blocker reason must be a VerificationBlockReason")
        if (
            not isinstance(self.workspace_generation, int)
            or isinstance(self.workspace_generation, bool)
            or self.workspace_generation < 0
        ):
            raise ValueError("verification blocker workspace_generation must be non-negative")
        if self.detail is not None:
            object.__setattr__(
                self,
                "detail",
                _safe_bounded_text(
                    self.detail,
                    limit=MAX_VERIFICATION_BLOCKER_DETAIL_BYTES,
                    field_name="verification blocker detail",
                ),
            )


@dataclass(frozen=True, slots=True)
class RequirementEvaluation:
    """A bounded projection of one requirement's current runtime truth."""

    requirement_id: str
    state: RequirementEvaluationState
    workspace_generation: int
    strength: RequirementStrength = RequirementStrength.REQUIRED
    evidence_reference: str | None = None
    blocker_reference: str | None = None
    blocker_reason: VerificationBlockReason | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        require_requirement_id(self.requirement_id)
        if not isinstance(self.state, RequirementEvaluationState):
            raise TypeError("requirement evaluation state must be canonical")
        if (
            not isinstance(self.workspace_generation, int)
            or isinstance(self.workspace_generation, bool)
            or self.workspace_generation < 0
        ):
            raise ValueError("requirement evaluation workspace_generation must be non-negative")
        if not isinstance(self.strength, RequirementStrength):
            raise TypeError("requirement evaluation strength must be canonical")
        _validate_reference(self.evidence_reference, field_name="evidence_reference")
        _validate_reference(self.blocker_reference, field_name="blocker_reference")
        if self.blocker_reason is not None and not isinstance(
            self.blocker_reason,
            VerificationBlockReason,
        ):
            raise TypeError("requirement evaluation blocker_reason must be canonical or None")
        object.__setattr__(
            self,
            "reason",
            _safe_bounded_text(
                self.reason,
                limit=MAX_VERIFICATION_EVALUATION_REASON_BYTES,
                field_name="requirement evaluation reason",
                allow_empty=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """One bounded, redacted terminal verification observation.

    ``workspace_generation`` is assigned by :class:`VerificationTracker` when
    the observation is accepted.  It is therefore an ordering marker, not a
    filesystem revision or a claim about the contents of a workspace.

    一条有界且脱敏的终态验证观察。``workspace_generation`` 由
    :class:`VerificationTracker` 在接受观察时分配, 因此它是顺序标记, 不是文件系统
    版本, 也不声称工作区内容本身。
    """

    tool_name: str
    outcome: VerificationOutcome
    summary: str
    scope: tuple[str, ...] = ()
    workspace_generation: int = 0
    covered_requirement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_tool_name(self.tool_name)
        if not isinstance(self.outcome, VerificationOutcome):
            raise TypeError("verification outcome must be a VerificationOutcome")
        object.__setattr__(
            self,
            "summary",
            _safe_bounded_text(
                self.summary,
                limit=MAX_VERIFICATION_SUMMARY_BYTES,
                field_name="verification summary",
            ),
        )
        scope = tuple(self.scope)
        if len(scope) > MAX_VERIFICATION_SCOPE_ITEMS:
            raise ValueError(
                f"verification scope must contain at most {MAX_VERIFICATION_SCOPE_ITEMS} items"
            )
        if not all(isinstance(item, str) for item in scope):
            raise TypeError("verification scope must contain strings")
        if len(set(scope)) != len(scope):
            raise ValueError("verification scope must not contain duplicate items")
        object.__setattr__(
            self,
            "scope",
            tuple(
                _safe_bounded_text(
                    item,
                    limit=MAX_VERIFICATION_SCOPE_BYTES,
                    field_name="verification scope item",
                )
                for item in scope
                if item
            ),
        )
        if (
            not isinstance(self.workspace_generation, int)
            or isinstance(self.workspace_generation, bool)
            or self.workspace_generation < 0
        ):
            raise ValueError("verification workspace_generation must be non-negative")
        object.__setattr__(
            self,
            "covered_requirement_ids",
            _validated_requirement_ids(
                self.covered_requirement_ids,
                field_name="verification covered_requirement_ids",
            ),
        )

    @classmethod
    def from_result(
        cls,
        *,
        tool_name: str,
        result_content: str,
        is_error: bool,
        scope: Sequence[str] = (),
        workspace_generation: int = 0,
        redaction_values: Sequence[str] = (),
        covered_requirement_ids: Sequence[str] = (),
    ) -> VerificationEvidence:
        """Create evidence from a completed tool result without retaining raw output."""

        if not isinstance(is_error, bool):
            raise ValueError("verification result is_error must be a bool")
        if not isinstance(result_content, str):
            raise TypeError("verification result content must be a string")
        safe_content = redact_sensitive_text(
            result_content,
            explicit_values=redaction_values,
        )
        return cls(
            tool_name,
            VerificationOutcome.FAILURE if is_error else VerificationOutcome.SUCCESS,
            safe_content or "(verification completed without output)",
            tuple(scope),
            workspace_generation,
            tuple(covered_requirement_ids),
        )

    def freshness_for(self, workspace_generation: int) -> VerificationFreshness:
        """Return freshness against one current workspace generation."""

        if (
            not isinstance(workspace_generation, int)
            or isinstance(workspace_generation, bool)
            or workspace_generation < 0
        ):
            raise ValueError("workspace_generation must be non-negative")
        return (
            VerificationFreshness.CURRENT
            if self.workspace_generation == workspace_generation
            else VerificationFreshness.STALE
        )


class VerificationObservation(Protocol):
    """Structural runtime observation consumed by the verification tracker."""

    @property
    def tool_name(self) -> str: ...

    @property
    def result_summary(self) -> str: ...

    @property
    def is_error(self) -> bool: ...

    @property
    def workspace_changed(self) -> bool: ...

    @property
    def progress_kind(self) -> ProgressKind: ...

    @property
    def verification(self) -> VerificationEvidence | None: ...


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Bounded projection of current verification state and its evidence."""

    state: VerificationState
    evidence: tuple[VerificationEvidence, ...]
    workspace_generation: int
    verification_required: bool
    requirement_evaluations: tuple[RequirementEvaluation, ...] = ()
    requirements_fingerprint: str | None = None
    required_count: int = 0
    advisory_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, VerificationState):
            raise TypeError("verification state must be a VerificationState")
        evidence = tuple(self.evidence)
        if len(evidence) > MAX_VERIFICATION_EVIDENCE_ITEMS:
            raise ValueError(
                f"verification evidence must contain at most {MAX_VERIFICATION_EVIDENCE_ITEMS} items"
            )
        if not all(isinstance(item, VerificationEvidence) for item in evidence):
            raise TypeError("verification evidence must contain VerificationEvidence values")
        object.__setattr__(self, "evidence", evidence)
        if (
            not isinstance(self.workspace_generation, int)
            or isinstance(self.workspace_generation, bool)
            or self.workspace_generation < 0
        ):
            raise ValueError("verification workspace_generation must be non-negative")
        if not isinstance(self.verification_required, bool):
            raise TypeError("verification_required must be a bool")
        evaluations = tuple(self.requirement_evaluations)
        if len(evaluations) > MAX_VERIFICATION_EVALUATIONS:
            raise ValueError(
                f"requirement evaluations must contain at most {MAX_VERIFICATION_EVALUATIONS} items"
            )
        if not all(isinstance(item, RequirementEvaluation) for item in evaluations):
            raise TypeError("requirement_evaluations must contain RequirementEvaluation values")
        if len({item.requirement_id for item in evaluations}) != len(evaluations):
            raise ValueError("requirement evaluations must not contain duplicate IDs")
        object.__setattr__(self, "requirement_evaluations", evaluations)
        _validate_reference(
            self.requirements_fingerprint,
            field_name="requirements_fingerprint",
        )
        derived_required_count = sum(
            item.strength is RequirementStrength.REQUIRED for item in evaluations
        )
        derived_advisory_count = sum(
            item.strength is RequirementStrength.ADVISORY for item in evaluations
        )
        if evaluations:
            if self.requirements_fingerprint is None:
                raise ValueError("requirement evaluations require a requirements fingerprint")
            if self.required_count not in {0, derived_required_count}:
                raise ValueError("required_count does not match requirement evaluations")
            if self.advisory_count not in {0, derived_advisory_count}:
                raise ValueError("advisory_count does not match requirement evaluations")
            object.__setattr__(self, "required_count", derived_required_count)
            object.__setattr__(self, "advisory_count", derived_advisory_count)
        else:
            for field_name, value in (
                ("required_count", self.required_count),
                ("advisory_count", self.advisory_count),
            ):
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"{field_name} must be non-negative")
            if self.requirements_fingerprint is None and (
                self.required_count or self.advisory_count
            ):
                raise ValueError("legacy verification reports cannot contain requirement counts")

    @property
    def latest(self) -> VerificationEvidence | None:
        """Return the latest observed verification, if any."""

        return self.evidence[-1] if self.evidence else None

    @property
    def latest_freshness(self) -> VerificationFreshness | None:
        """Return freshness for the latest evidence, if any."""

        latest = self.latest
        return latest.freshness_for(self.workspace_generation) if latest is not None else None

    @property
    def final_output_gate_active(self) -> bool:
        """Return whether terminal model output must await verification truth.

        The gate is sticky for the lifetime of a report: a workspace mutation,
        an explicit verification requirement, or any recorded evidence is
        enough to activate it.  This is a derived projection of the existing
        verification owner rather than a second runtime state machine.

        返回终态模型输出是否必须等待验证事实。

        在一个报告生命周期内, gate 一旦激活便保持激活: 工作区修改、显式验证要求或
        已记录的任意证据均会触发它。这是现有验证所有者的派生投影,不是第二个运行时状态机。
        """

        return bool(
            self.workspace_generation > 0
            or self.verification_required
            or self.evidence
            or self.required_count > 0
        )

    @property
    def confirmed_items(self) -> tuple[str, ...]:
        """Return only evidence that may be presented as successful validation."""

        if self.requirements_fingerprint is not None:
            required = tuple(
                item
                for item in self.requirement_evaluations
                if item.strength is RequirementStrength.REQUIRED
            )
            if (
                self.state is VerificationState.PASS
                and required
                and all(item.state is RequirementEvaluationState.SATISFIED for item in required)
            ):
                return ("All required verification requirements are satisfied.",)
            return ()
        latest = self.latest
        if (
            self.state is not VerificationState.PASS
            or latest is None
            or latest.outcome is not VerificationOutcome.SUCCESS
            or self.latest_freshness is not VerificationFreshness.CURRENT
        ):
            return ()
        scope = ", ".join(latest.scope) if latest.scope else "scope unspecified"
        return (f"Verification passed via {latest.tool_name} ({scope}).",)

    @property
    def unverified_items(self) -> tuple[str, ...]:
        """Return conservative language for non-success states."""

        if self.requirements_fingerprint is not None:
            if self.state is VerificationState.FAIL:
                return (
                    "At least one required verification requirement failed; validation was not successful.",
                )
            if self.state is VerificationState.INCOMPLETE:
                states = tuple(
                    item.state.value
                    for item in self.requirement_evaluations
                    if item.strength is RequirementStrength.REQUIRED
                )
                if states:
                    return (
                        "Required verification is incomplete; current requirement states: "
                        + ", ".join(states)
                        + ".",
                    )
                return ("Required verification is incomplete; no current result is available.",)
            if self.advisory_count:
                return (
                    "Advisory verification requirements are not sufficient to claim a fully verified completion.",
                )
            return ()
        latest = self.latest
        if self.state is VerificationState.FAIL:
            return ("The latest verification failed; validation was not successful.",)
        if self.state is VerificationState.INCOMPLETE:
            if latest is not None and self.latest_freshness is VerificationFreshness.STALE:
                return ("Verification is stale because the workspace changed afterward.",)
            if self.workspace_generation > 0:
                return ("Workspace changes do not have a current verification result.",)
            return ("Verification was required but no result was recorded.",)
        return ()

    def to_event_data(self) -> dict[str, object]:
        """Return a bounded diagnostic projection without raw arguments or output."""

        return {
            "state": self.state.value,
            "workspace_generation": self.workspace_generation,
            "verification_required": self.verification_required,
            "latest_freshness": (
                self.latest_freshness.value if self.latest_freshness is not None else None
            ),
            "evidence_count": len(self.evidence),
            "requirements_fingerprint": self.requirements_fingerprint,
            "required_count": self.required_count,
            "advisory_count": self.advisory_count,
            "requirement_state_counts": {
                state.value: sum(item.state is state for item in self.requirement_evaluations)
                for state in RequirementEvaluationState
            },
        }


class VerificationTracker:
    """Own verification state transitions for one logical runtime task."""

    __slots__ = (
        "_evidence",
        "_fact_sequence",
        "_latest_blocker_by_requirement",
        "_latest_evidence_by_requirement",
        "_requirements",
        "_verification_required",
        "_workspace_generation",
    )

    def __init__(
        self,
        *,
        verification_required: bool = False,
        requirements: VerificationRequirementsSnapshot | None = None,
    ) -> None:
        if not isinstance(verification_required, bool):
            raise TypeError("verification_required must be a bool")
        if requirements is not None and not isinstance(
            requirements,
            VerificationRequirementsSnapshot,
        ):
            raise TypeError("requirements must be a VerificationRequirementsSnapshot or None")
        self._verification_required = verification_required
        self._workspace_generation = 0
        self._evidence: list[VerificationEvidence] = []
        self._requirements = requirements
        self._latest_evidence_by_requirement: dict[str, tuple[VerificationEvidence, str]] = {}
        self._latest_blocker_by_requirement: dict[str, tuple[VerificationBlocker, str]] = {}
        self._fact_sequence = 0

    @property
    def workspace_generation(self) -> int:
        """Return the number of observed workspace mutations in this task."""

        return self._workspace_generation

    @property
    def requirements(self) -> VerificationRequirementsSnapshot | None:
        """Return the immutable declaration snapshot used by this tracker."""

        return self._requirements

    def record_workspace_mutation(self) -> None:
        """Invalidate all earlier verification by advancing the workspace generation."""

        self._workspace_generation += 1

    def record_verification(self, evidence: VerificationEvidence) -> None:
        """Record one result at the current workspace generation."""

        if not isinstance(evidence, VerificationEvidence):
            raise TypeError("verification evidence must be a VerificationEvidence")
        covered_ids = evidence.covered_requirement_ids
        if self._requirements is not None:
            known_ids = set(self._requirements.requirement_ids)
            unknown_ids = set(covered_ids) - known_ids
            if unknown_ids:
                raise ValueError("verification evidence covers an unknown requirement ID")
        current = replace(evidence, workspace_generation=self._workspace_generation)
        self._evidence.append(current)
        del self._evidence[:-MAX_VERIFICATION_EVIDENCE_ITEMS]
        self._fact_sequence += 1
        if covered_ids:
            reference = _reference_digest(
                "verification-evidence",
                {
                    "sequence": self._fact_sequence,
                    "tool_name": current.tool_name,
                    "outcome": current.outcome.value,
                    "workspace_generation": current.workspace_generation,
                    "covered_requirement_ids": covered_ids,
                },
            )
            fact = (current, reference)
            for requirement_id in covered_ids:
                self._latest_evidence_by_requirement[requirement_id] = fact

    def record_blocker(self, blocker: VerificationBlocker) -> None:
        """Record one explicit typed inability to verify requirements."""

        if self._requirements is None:
            raise ValueError("a requirements snapshot is required to record a blocker")
        if not isinstance(blocker, VerificationBlocker):
            raise TypeError("verification blocker must be a VerificationBlocker")
        known_ids = set(self._requirements.requirement_ids)
        if set(blocker.affected_requirement_ids) - known_ids:
            raise ValueError("verification blocker affects an unknown requirement ID")
        self._fact_sequence += 1
        reference = _reference_digest(
            "verification-blocker",
            {
                "sequence": self._fact_sequence,
                "affected_requirement_ids": blocker.affected_requirement_ids,
                "reason": blocker.reason.value,
                "workspace_generation": blocker.workspace_generation,
                "detail": blocker.detail,
            },
        )
        fact = (blocker, reference)
        for requirement_id in blocker.affected_requirement_ids:
            self._latest_blocker_by_requirement[requirement_id] = fact

    def observe(self, observation: VerificationObservation) -> None:
        """Apply one terminal observation in deterministic execution order.

        A combined observation is interpreted as mutation first and terminal
        verification second.  This reflects the fact that a tool result is
        reported after all effects of that tool call.  Separate later mutation
        observations still invalidate the recorded result.
        """

        if not isinstance(observation.workspace_changed, bool):
            raise TypeError("observation workspace_changed must be a bool")
        if observation.workspace_changed:
            self.record_workspace_mutation()

        evidence = observation.verification
        if evidence is None and observation.progress_kind is ProgressKind.VERIFICATION:
            evidence = VerificationEvidence.from_result(
                tool_name=observation.tool_name,
                result_content=observation.result_summary,
                is_error=observation.is_error,
            )
        if evidence is not None:
            self.record_verification(evidence)

    def _evaluate_requirement(
        self,
        requirement_id: str,
        strength: RequirementStrength,
    ) -> RequirementEvaluation:
        evidence_fact = self._latest_evidence_by_requirement.get(requirement_id)
        blocker_fact = self._latest_blocker_by_requirement.get(requirement_id)
        current_evidence = (
            evidence_fact
            if evidence_fact is not None
            and evidence_fact[0].workspace_generation == self._workspace_generation
            else None
        )
        current_blocker = (
            blocker_fact
            if blocker_fact is not None
            and blocker_fact[0].workspace_generation == self._workspace_generation
            else None
        )
        if current_evidence is not None:
            evidence, reference = current_evidence
            state = (
                RequirementEvaluationState.SATISFIED
                if evidence.outcome is VerificationOutcome.SUCCESS
                else RequirementEvaluationState.FAILED
            )
            reason = (
                "current successful verification evidence"
                if state is RequirementEvaluationState.SATISFIED
                else "current verification evidence reported failure"
            )
            return RequirementEvaluation(
                requirement_id,
                state,
                self._workspace_generation,
                strength,
                evidence_reference=reference,
                reason=reason,
            )
        if current_blocker is not None:
            blocker, reference = current_blocker
            return RequirementEvaluation(
                requirement_id,
                RequirementEvaluationState.BLOCKED,
                self._workspace_generation,
                strength,
                blocker_reference=reference,
                blocker_reason=blocker.reason,
                reason=f"verification blocked: {blocker.reason.value}",
            )
        if evidence_fact is not None:
            evidence, reference = evidence_fact
            return RequirementEvaluation(
                requirement_id,
                RequirementEvaluationState.STALE,
                self._workspace_generation,
                strength,
                evidence_reference=reference,
                reason=("verification evidence belongs to an earlier workspace generation"),
            )
        return RequirementEvaluation(
            requirement_id,
            RequirementEvaluationState.NO_EVIDENCE,
            self._workspace_generation,
            strength,
            reason="no verification evidence recorded for this requirement",
        )

    def _structured_report(self) -> VerificationReport:
        assert self._requirements is not None
        active_requirements = self._requirements.active_for_generation(self._workspace_generation)
        evaluations = tuple(
            self._evaluate_requirement(requirement.requirement_id, requirement.strength)
            for requirement in active_requirements
        )
        required = tuple(
            item for item in evaluations if item.strength is RequirementStrength.REQUIRED
        )
        if required and any(item.state is RequirementEvaluationState.FAILED for item in required):
            state = VerificationState.FAIL
        elif required and all(
            item.state is RequirementEvaluationState.SATISFIED for item in required
        ):
            state = VerificationState.PASS
        elif required or self._verification_required or self._workspace_generation > 0:
            state = VerificationState.INCOMPLETE
        else:
            state = VerificationState.NOT_APPLICABLE
        return VerificationReport(
            state,
            tuple(self._evidence),
            self._workspace_generation,
            self._verification_required,
            evaluations,
            self._requirements.fingerprint,
            sum(item.strength is RequirementStrength.REQUIRED for item in evaluations),
            sum(item.strength is RequirementStrength.ADVISORY for item in evaluations),
        )

    def report(self) -> VerificationReport:
        """Project the only externally consumable verification state."""

        if self._requirements is not None:
            return self._structured_report()
        latest = self._evidence[-1] if self._evidence else None
        if (
            latest is not None
            and latest.freshness_for(self._workspace_generation) is VerificationFreshness.STALE
        ):
            state = VerificationState.INCOMPLETE
        elif latest is not None and latest.outcome is VerificationOutcome.SUCCESS:
            state = VerificationState.PASS
        elif latest is not None:
            state = VerificationState.FAIL
        elif self._workspace_generation > 0 or self._verification_required:
            state = VerificationState.INCOMPLETE
        else:
            state = VerificationState.NOT_APPLICABLE
        return VerificationReport(
            state,
            tuple(self._evidence),
            self._workspace_generation,
            self._verification_required,
        )


def verification_scope_for_tool(
    tool_name: str,
    arguments: Mapping[str, object],
) -> tuple[str, ...]:
    """Return a bounded semantic scope for an explicitly safe verification command.

    The existing conservative Bash command classifier is reused.  This only
    recognizes a command already being executed; it never searches for tests
    or launches a test runner.
    """

    if tool_name != "bash":
        return ()
    command = arguments.get("command")
    if not isinstance(command, str):
        return ()
    family = classify_bash_command_family(command)
    if family in {BashCommandFamily.TEST, BashCommandFamily.STATIC_CHECK}:
        return (f"bash:{family.value}",)
    return ()


def build_verification_evidence(
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    result_content: str,
    is_error: bool,
    redaction_values: Sequence[str] = (),
    covered_requirement_ids: Sequence[str] = (),
) -> VerificationEvidence | None:
    """Build evidence for a completed command with an explicit verification shape."""

    scope = verification_scope_for_tool(tool_name, arguments)
    if not scope:
        return None
    return VerificationEvidence.from_result(
        tool_name=tool_name,
        result_content=result_content,
        is_error=is_error,
        scope=scope,
        covered_requirement_ids=covered_requirement_ids,
        redaction_values=redaction_values,
    )


__all__ = [
    "MAX_VERIFICATION_BLOCKER_DETAIL_BYTES",
    "MAX_VERIFICATION_COVERED_REQUIREMENTS",
    "MAX_VERIFICATION_EVALUATIONS",
    "MAX_VERIFICATION_EVALUATION_REASON_BYTES",
    "MAX_VERIFICATION_EVIDENCE_ITEMS",
    "MAX_VERIFICATION_REFERENCE_BYTES",
    "MAX_VERIFICATION_SCOPE_BYTES",
    "MAX_VERIFICATION_SCOPE_ITEMS",
    "MAX_VERIFICATION_SUMMARY_BYTES",
    "MAX_VERIFICATION_TOOL_NAME_BYTES",
    "RequirementEvaluation",
    "RequirementEvaluationState",
    "VerificationBlockReason",
    "VerificationBlocker",
    "VerificationEvidence",
    "VerificationFreshness",
    "VerificationObservation",
    "VerificationOutcome",
    "VerificationReport",
    "VerificationState",
    "VerificationTracker",
    "build_verification_evidence",
    "verification_scope_for_tool",
]
