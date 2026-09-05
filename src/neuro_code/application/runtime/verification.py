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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from neuro_code.domain.execution import ProgressKind
from neuro_code.domain.permissions import BashCommandFamily, classify_bash_command_family
from neuro_code.shared.redaction import redact_sensitive_text

MAX_VERIFICATION_EVIDENCE_ITEMS = 4
MAX_VERIFICATION_SCOPE_ITEMS = 8
MAX_VERIFICATION_SCOPE_BYTES = 160
MAX_VERIFICATION_SUMMARY_BYTES = 512
MAX_VERIFICATION_TOOL_NAME_BYTES = 80


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


def _bounded_utf8(value: str, *, limit: int, suffix: str = "…") -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= limit:
        return encoded[:limit].decode("utf-8", errors="ignore")
    prefix = encoded[: limit - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"


def _safe_bounded_text(value: str, *, limit: int, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
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

        return bool(self.workspace_generation > 0 or self.verification_required or self.evidence)

    @property
    def confirmed_items(self) -> tuple[str, ...]:
        """Return only evidence that may be presented as successful validation."""

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
        }


class VerificationTracker:
    """Own verification state transitions for one logical runtime task."""

    __slots__ = ("_evidence", "_verification_required", "_workspace_generation")

    def __init__(self, *, verification_required: bool = False) -> None:
        if not isinstance(verification_required, bool):
            raise TypeError("verification_required must be a bool")
        self._verification_required = verification_required
        self._workspace_generation = 0
        self._evidence: list[VerificationEvidence] = []

    @property
    def workspace_generation(self) -> int:
        """Return the number of observed workspace mutations in this task."""

        return self._workspace_generation

    def record_workspace_mutation(self) -> None:
        """Invalidate all earlier verification by advancing the workspace generation."""

        self._workspace_generation += 1

    def record_verification(self, evidence: VerificationEvidence) -> None:
        """Record one result at the current workspace generation."""

        if not isinstance(evidence, VerificationEvidence):
            raise TypeError("verification evidence must be a VerificationEvidence")
        current = replace(evidence, workspace_generation=self._workspace_generation)
        self._evidence.append(current)
        del self._evidence[:-MAX_VERIFICATION_EVIDENCE_ITEMS]

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

    def report(self) -> VerificationReport:
        """Project the only externally consumable verification state."""

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
        redaction_values=redaction_values,
    )


__all__ = [
    "MAX_VERIFICATION_EVIDENCE_ITEMS",
    "MAX_VERIFICATION_SCOPE_BYTES",
    "MAX_VERIFICATION_SCOPE_ITEMS",
    "MAX_VERIFICATION_SUMMARY_BYTES",
    "MAX_VERIFICATION_TOOL_NAME_BYTES",
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
