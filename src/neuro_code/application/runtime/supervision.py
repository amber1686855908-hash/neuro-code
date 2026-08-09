"""Pure, per-turn execution supervision and stable tool fingerprints.

This module intentionally does not import :mod:`agent`.  A later integration
phase can adapt ``AgentRuntime`` tool results into ``ToolExecutionObservation``
without making the runtime depend on untyped event payloads.

提供纯函数式的逐回合执行监督和稳定工具指纹. 模块不依赖 Agent,避免运行时依赖无类型事件载荷.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Protocol

from neuro_code.application.execution_policy import (
    NORMAL_EXECUTION_BUDGET,
    ExecutionBudgetPolicy,
)
from neuro_code.domain.execution import (
    AgentExecutionStatus,
    ExecutionBudget,
    ExecutionBudgetUsage,
    ExecutionCounters,
    ExecutionSnapshot,
    ProgressKind,
    SupervisionThresholds,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    ToolCallCount,
    ToolInteractionFingerprint,
)
from neuro_code.shared.redaction import redact_sensitive_text

MAX_OBSERVATION_SUMMARY_BYTES = 512


class SupervisionMode(StrEnum):
    """Whether decisions enforce budgets or only describe observed execution.

    表示决策是执行预算约束,还是仅描述已观察到的执行."""

    ENFORCE = "enforce"
    OBSERVE = "observe"


class ExecutionControlMode(StrEnum):
    """Whether AgentRuntime only observes decisions or finalizes terminal ones.

    表示 AgentRuntime 仅观察决策,还是执行终态最终化."""

    OBSERVE_ONLY = "observe_only"
    FINALIZE_TERMINAL = "finalize_terminal"


class SupervisionCheckpoint(StrEnum):
    """A stable boundary at which one supervision trace is captured.

    表示捕获一条监督轨迹的稳定检查点."""

    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    AFTER_TOOL_BATCH = "after_tool_batch"
    AFTER_TOOL = "after_tool"


def _validate_tool_name(value: object, *, field_name: str = "tool_name") -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical tool name")
    return value


def _validate_sha256(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _validate_token_name(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty safe name")
    return value


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_text(value: str, *, redaction_values: Sequence[str] = ()) -> str:
    return redact_sensitive_text(value, explicit_values=redaction_values)


def _normalized_observation_text(value: str, *, redaction_values: Sequence[str] = ()) -> str:
    return " ".join(_safe_text(value, redaction_values=redaction_values).split())


def _bounded_summary(value: str, *, redaction_values: Sequence[str] = ()) -> str:
    safe = _safe_text(value, redaction_values=redaction_values)
    encoded = safe.encode("utf-8")
    if len(encoded) <= MAX_OBSERVATION_SUMMARY_BYTES:
        return safe
    bounded = encoded[:MAX_OBSERVATION_SUMMARY_BYTES].decode("utf-8", "ignore")
    return f"{bounded}…"


def _stable_token(value: str, *, redaction_values: Sequence[str] = ()) -> str:
    return _sha256(_normalized_observation_text(value, redaction_values=redaction_values))


def _canonical_path(value: Path, context: PathNormalizationContext | None) -> str:
    if context is None:
        return f"path:{value.as_posix()}"

    candidate = value
    if context.workspace_root is not None and not candidate.is_absolute():
        candidate = context.workspace_root / candidate

    for index, root in enumerate(context.ephemeral_roots):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        return f"ephemeral-{index}:{relative.as_posix()}"

    if context.workspace_root is not None:
        try:
            relative = candidate.relative_to(context.workspace_root)
        except ValueError:
            pass
        else:
            return f"workspace:{relative.as_posix()}"
    return f"path:{candidate.as_posix()}"


def _canonical_argument_value(
    value: object,
    *,
    path_context: PathNormalizationContext | None,
    redaction_values: Sequence[str],
) -> object:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool arguments must not contain non-finite floats")
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", _safe_text(value, redaction_values=redaction_values)]
    if isinstance(value, Path):
        return ["path", _canonical_path(value, path_context)]
    if isinstance(value, Mapping):
        pairs: list[list[object]] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("tool argument mapping keys must be strings")
            pairs.append(
                [
                    _safe_text(key, redaction_values=redaction_values),
                    _canonical_argument_value(
                        value[key],
                        path_context=path_context,
                        redaction_values=redaction_values,
                    ),
                ]
            )
        return ["mapping", pairs]
    if isinstance(value, list):
        return [
            "list",
            [
                _canonical_argument_value(
                    item,
                    path_context=path_context,
                    redaction_values=redaction_values,
                )
                for item in value
            ],
        ]
    if isinstance(value, tuple):
        return [
            "tuple",
            [
                _canonical_argument_value(
                    item,
                    path_context=path_context,
                    redaction_values=redaction_values,
                )
                for item in value
            ],
        ]
    raise TypeError(f"unsupported tool argument type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class PathNormalizationContext:
    """Explicit path roots used while hashing Path argument values.

    Only paths explicitly supplied as ``Path`` values receive this treatment.
    Strings are retained as strings, preventing a broad ``/tmp`` rule from
    accidentally merging meaningful literal values.

    表示对 Path 参数值进行哈希时使用的显式路径根.
    """

    workspace_root: Path | None = None
    ephemeral_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.workspace_root is not None and not isinstance(self.workspace_root, Path):
            raise ValueError("workspace_root must be a Path or None")
        roots = tuple(self.ephemeral_roots)
        if not all(isinstance(root, Path) for root in roots):
            raise ValueError("ephemeral_roots must contain Path values")
        if len(set(roots)) != len(roots):
            raise ValueError("ephemeral_roots must not contain duplicates")
        object.__setattr__(self, "ephemeral_roots", roots)


@dataclass(frozen=True, slots=True)
class StableMetadataFact:
    """An allowlisted metadata value represented only by its safe digest.

    表示列入允许列表的元数据值,只保存其安全摘要."""

    name: str
    value_digest: str

    def __post_init__(self) -> None:
        _validate_token_name(self.name, field_name="stable metadata fact name")
        _validate_sha256(self.value_digest, field_name="stable metadata fact value_digest")


def stable_metadata_fact(
    name: str,
    value: str,
    *,
    redaction_values: Sequence[str] = (),
) -> StableMetadataFact:
    """Create a metadata fact without retaining its original value.

    创建一个元数据事实,不保留其原始值."""

    _validate_token_name(name, field_name="stable metadata fact name")
    if not isinstance(value, str):
        raise ValueError("stable metadata fact value must be a string")
    return StableMetadataFact(name, _stable_token(value, redaction_values=redaction_values))


def stable_action_digest(
    tool_name: str,
    arguments: Mapping[str, object],
    *,
    path_context: PathNormalizationContext | None = None,
    redaction_values: Sequence[str] = (),
) -> str:
    """Hash typed, ordered canonical arguments without retaining their source text.

    对类型化且有序的规范参数进行哈希,不保留参数原文."""

    _validate_tool_name(tool_name)
    canonical = {
        "tool_name": _safe_text(tool_name, redaction_values=redaction_values),
        "arguments": _canonical_argument_value(
            arguments,
            path_context=path_context,
            redaction_values=redaction_values,
        ),
    }
    return _sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def stable_observation_digest(
    *,
    is_error: bool,
    content: str,
    metadata_facts: Sequence[StableMetadataFact] = (),
    workspace_progress_token: str | None = None,
    plan_fingerprint: str | None = None,
    verification_token: str | None = None,
    external_state_token: str | None = None,
    redaction_values: Sequence[str] = (),
) -> str:
    """Hash a redacted, bounded observation without serializing arbitrary metadata.

    对脱敏且有界的观察结果进行哈希,不序列化任意元数据."""

    if not isinstance(is_error, bool):
        raise ValueError("is_error must be a bool")
    if not isinstance(content, str):
        raise ValueError("observation content must be a string")
    facts = tuple(metadata_facts)
    if not all(isinstance(fact, StableMetadataFact) for fact in facts):
        raise ValueError("metadata_facts must contain StableMetadataFact values")
    if len({fact.name for fact in facts}) != len(facts):
        raise ValueError("metadata_facts must not contain duplicate names")

    payload = {
        "is_error": is_error,
        "content_digest": _stable_token(content, redaction_values=redaction_values),
        "metadata_facts": [
            [fact.name, fact.value_digest] for fact in sorted(facts, key=lambda fact: fact.name)
        ],
        "workspace_progress_token": _optional_token_digest(
            workspace_progress_token,
            redaction_values=redaction_values,
        ),
        "plan_fingerprint": _optional_token_digest(
            plan_fingerprint,
            redaction_values=redaction_values,
        ),
        "verification_token": _optional_token_digest(
            verification_token,
            redaction_values=redaction_values,
        ),
        "external_state_token": _optional_token_digest(
            external_state_token,
            redaction_values=redaction_values,
        ),
    }
    return _sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _optional_token_digest(
    value: str | None,
    *,
    redaction_values: Sequence[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("stable progress tokens must be strings or None")
    return _stable_token(value, redaction_values=redaction_values)


def _safe_digest_token(value: str | None, *, redaction_values: Sequence[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("stable progress tokens must be strings or None")
    return _stable_token(value, redaction_values=redaction_values)


@dataclass(frozen=True, slots=True)
class ToolExecutionObservation:
    """Typed, redacted input to the future runtime supervision boundary.

    表示传递给运行时监督边界的类型化脱敏输入."""

    tool_name: str
    action_digest: str
    observation_digest: str
    result_summary: str
    is_error: bool
    metadata_facts: tuple[StableMetadataFact, ...] = ()
    workspace_changed: bool = False
    workspace_progress_token: str | None = None
    plan_fingerprint: str | None = None
    verification_token: str | None = None
    external_state_token: str | None = None
    progress_kind: ProgressKind = ProgressKind.NONE

    def __post_init__(self) -> None:
        _validate_tool_name(self.tool_name)
        _validate_sha256(self.action_digest, field_name="action_digest")
        _validate_sha256(self.observation_digest, field_name="observation_digest")
        if not isinstance(self.result_summary, str):
            raise ValueError("result_summary must be a string")
        object.__setattr__(self, "result_summary", _bounded_summary(self.result_summary))
        if not isinstance(self.is_error, bool):
            raise ValueError("is_error must be a bool")
        facts = tuple(self.metadata_facts)
        if not all(isinstance(fact, StableMetadataFact) for fact in facts):
            raise ValueError("metadata_facts must contain StableMetadataFact values")
        if len({fact.name for fact in facts}) != len(facts):
            raise ValueError("metadata_facts must not contain duplicate names")
        object.__setattr__(self, "metadata_facts", tuple(sorted(facts, key=lambda fact: fact.name)))
        if not isinstance(self.workspace_changed, bool):
            raise ValueError("workspace_changed must be a bool")
        for field_name, value in (
            ("workspace_progress_token", self.workspace_progress_token),
            ("plan_fingerprint", self.plan_fingerprint),
            ("verification_token", self.verification_token),
            ("external_state_token", self.external_state_token),
        ):
            if value is not None:
                _validate_sha256(value, field_name=field_name)
        if not isinstance(self.progress_kind, ProgressKind):
            raise ValueError("progress_kind must be canonical")

    @property
    def fingerprint(self) -> ToolInteractionFingerprint:
        return ToolInteractionFingerprint(
            self.tool_name,
            self.action_digest,
            self.observation_digest,
            self.is_error,
            self.progress_kind,
        )

    @classmethod
    def from_result(
        cls,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        result_content: str,
        is_error: bool,
        metadata_facts: Sequence[StableMetadataFact] = (),
        workspace_changed: bool = False,
        workspace_progress_token: str | None = None,
        plan_fingerprint: str | None = None,
        verification_token: str | None = None,
        external_state_token: str | None = None,
        progress_kind: ProgressKind = ProgressKind.NONE,
        path_context: PathNormalizationContext | None = None,
        redaction_values: Sequence[str] = (),
        tool_call_id: str | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
        duration_seconds: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> ToolExecutionObservation:
        """Build an observation while discarding raw arguments and full output.

        Transport identifiers and volatile usage metrics are accepted only to make
        their exclusion explicit at the future runtime boundary.  They are never
        retained or included in either digest.

        构建观察结果并丢弃原始参数和完整输出. 传输标识和易变用量只用于明确排除,不会进入摘要.
        """

        if not isinstance(result_content, str):
            raise ValueError("result_content must be a string")
        action_digest = stable_action_digest(
            tool_name,
            arguments,
            path_context=path_context,
            redaction_values=redaction_values,
        )
        observation_digest = stable_observation_digest(
            is_error=is_error,
            content=result_content,
            metadata_facts=metadata_facts,
            workspace_progress_token=workspace_progress_token,
            plan_fingerprint=plan_fingerprint,
            verification_token=verification_token,
            external_state_token=external_state_token,
            redaction_values=redaction_values,
        )
        return cls(
            tool_name=tool_name,
            action_digest=action_digest,
            observation_digest=observation_digest,
            result_summary=_bounded_summary(result_content, redaction_values=redaction_values),
            is_error=is_error,
            metadata_facts=tuple(metadata_facts),
            workspace_changed=workspace_changed,
            workspace_progress_token=_safe_digest_token(
                workspace_progress_token,
                redaction_values=redaction_values,
            ),
            plan_fingerprint=_safe_digest_token(
                plan_fingerprint,
                redaction_values=redaction_values,
            ),
            verification_token=_safe_digest_token(
                verification_token,
                redaction_values=redaction_values,
            ),
            external_state_token=_safe_digest_token(
                external_state_token,
                redaction_values=redaction_values,
            ),
            progress_kind=progress_kind,
        )


@dataclass(frozen=True, slots=True)
class SupervisionTraceRecord:
    """One safe, in-memory observation of a supervised runtime boundary.

    表示监督运行时边界的一条安全内存观察记录."""

    checkpoint: SupervisionCheckpoint
    model_step: int
    tool_name: str | None
    snapshot: ExecutionSnapshot
    decision: SupervisorDecision

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, SupervisionCheckpoint):
            raise ValueError("supervision checkpoint must be canonical")
        if (
            not isinstance(self.model_step, int)
            or isinstance(self.model_step, bool)
            or self.model_step < 1
        ):
            raise ValueError("supervision model_step must be a positive integer")
        if self.tool_name is not None:
            _validate_tool_name(self.tool_name)
        if not isinstance(self.snapshot, ExecutionSnapshot):
            raise ValueError("supervision trace snapshot must be canonical")
        if not isinstance(self.decision, SupervisorDecision):
            raise ValueError("supervision trace decision must be canonical")


class SupervisionObserver(Protocol):
    """Receives a redacted, typed trace without affecting agent events.

    接收脱敏的类型化轨迹,且不影响 Agent 事件."""

    def __call__(self, record: SupervisionTraceRecord) -> None: ...


DEFAULT_OBSERVATION_BUDGET = NORMAL_EXECUTION_BUDGET


def create_observing_supervisor(
    *,
    budget: ExecutionBudget | None = None,
    max_model_calls: int | None = None,
) -> AgentExecutionSupervisor:
    """Create the default non-enforcing supervisor for one runtime turn.

    为一个运行时回合创建默认的非强制监督器.

    ``max_model_calls`` is retained as a compatibility spelling for the old
    ``max_steps`` setting.  It now scales the complete count-based ordinary
    execution budget instead of changing only model calls. Finalizer attempts
    remain a separate bounded resource and are never reserved from this value.
    """

    if budget is not None and max_model_calls is not None:
        raise ValueError("budget and max_model_calls are mutually exclusive")
    resolved = budget
    if resolved is None:
        resolved = (
            DEFAULT_OBSERVATION_BUDGET
            if max_model_calls is None
            else ExecutionBudgetPolicy.from_max_steps(max_model_calls)
        )
    return AgentExecutionSupervisor(resolved, mode=SupervisionMode.OBSERVE)


class AgentExecutionSupervisor:
    """Stateful, deterministic supervision for exactly one future agent turn.

    为一个 Agent 回合提供有状态且确定性的监督,每个实例只服务一个回合."""

    def __init__(
        self,
        budget: ExecutionBudget,
        *,
        thresholds: SupervisionThresholds | None = None,
        clock: Callable[[], float] = monotonic,
        mode: SupervisionMode = SupervisionMode.ENFORCE,
    ) -> None:
        if not isinstance(budget, ExecutionBudget):
            raise TypeError("budget must be an ExecutionBudget")
        if thresholds is not None and not isinstance(thresholds, SupervisionThresholds):
            raise TypeError("thresholds must be SupervisionThresholds or None")
        if not isinstance(mode, SupervisionMode):
            raise TypeError("mode must be a SupervisionMode")
        self._budget = budget
        self._thresholds = thresholds or SupervisionThresholds()
        self._clock = clock
        self._mode = mode
        self._started_at: float | None = None
        self._snapshot: ExecutionSnapshot | None = None
        self._reserved_tool_names: list[str] = []
        self._model_completion_pending = False
        self._batch_had_progress = False
        self._last_workspace_token: str | None = None
        self._last_plan_fingerprint: str | None = None
        self._last_verification_token: str | None = None
        self._last_external_state_token: str | None = None

    @property
    def snapshot(self) -> ExecutionSnapshot:
        return self._require_started()

    @property
    def mode(self) -> SupervisionMode:
        return self._mode

    def budget_usage(self, *, include_model_reserve: bool = False) -> ExecutionBudgetUsage:
        """Return a live, non-sensitive projection of ordinary-turn budget use.

        ``include_model_reserve`` previews the request that is about to be
        authorized so request-scoped guidance can describe the same counts the
        model call will consume without mutating supervision early.

        返回普通回合预算实时且不含敏感内容的投影. 可预览即将授权的模型请求,
        使请求范围指引与实际消费计数一致,而无需提前修改监督状态.
        """

        if not isinstance(include_model_reserve, bool):
            raise TypeError("include_model_reserve must be a bool")
        snapshot = self._require_started()
        counters = snapshot.counters
        if include_model_reserve:
            counters = replace(counters, model_requests=counters.model_requests + 1)
        return ExecutionBudgetUsage(self._budget, counters, self._elapsed_seconds())

    def start_turn(self) -> ExecutionSnapshot:
        """Initialize the isolated state used for one execution.

        初始化一次执行所需的隔离状态."""

        if self._snapshot is not None:
            raise RuntimeError("supervisor turn has already started")
        started_at = self._clock()
        if not math.isfinite(started_at):
            raise RuntimeError("supervisor clock must return a finite value")
        self._started_at = started_at
        self._snapshot = ExecutionSnapshot(
            AgentExecutionStatus.RUNNING,
            ExecutionCounters(),
            0.0,
            (),
            0,
            0,
            0,
        )
        return self._snapshot

    def authorize_model_request(self) -> SupervisorDecision:
        """Reserve one ordinary model request from the ordinary-turn budget.

        从普通回合预算中预留一次普通模型请求."""

        snapshot = self._require_started()
        if self._reserved_tool_names:
            raise RuntimeError("cannot authorize a model request while tool calls are pending")
        if self._model_completion_pending:
            raise RuntimeError("cannot authorize a model request before handling its completion")
        decision = self._evaluate(include_model_reserve=True)
        if self._mode is SupervisionMode.ENFORCE and not _decision_allows_continuation(decision):
            return decision
        counters = replace(snapshot.counters, model_requests=snapshot.counters.model_requests + 1)
        self._replace_snapshot(counters=counters)
        return decision

    def observe_model_completion(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> SupervisorDecision:
        """Record one logical model completion and its optional usage values.

        记录一次逻辑模型完成及其可选用量值."""

        snapshot = self._require_started()
        counters = snapshot.counters
        if counters.model_completions >= counters.model_requests:
            raise RuntimeError("model completion requires an authorized model request")
        next_input = _accumulate_tokens(
            counters.input_tokens, input_tokens, field_name="input_tokens"
        )
        next_output = _accumulate_tokens(
            counters.output_tokens,
            output_tokens,
            field_name="output_tokens",
        )
        self._replace_snapshot(
            counters=replace(
                counters,
                model_completions=counters.model_completions + 1,
                input_tokens=next_input,
                output_tokens=next_output,
            )
        )
        self._model_completion_pending = True
        return self._evaluate(include_model_reserve=False)

    def assess_tool_batch(self, tool_names: Sequence[str]) -> SupervisorDecision:
        """Atomically reserve one tool round and all calls returned by one model step.

        原子预留一个工具轮次及一次模型步骤返回的全部调用."""

        snapshot = self._require_started()
        if not self._model_completion_pending:
            raise RuntimeError("tool batch requires a pending completed model request")
        if self._reserved_tool_names:
            raise RuntimeError("cannot reserve a new tool batch while calls are pending")
        names = tuple(tool_names)
        if not names:
            raise ValueError("tool batch must contain at least one tool name")
        for name in names:
            _validate_tool_name(name)

        decision = self._evaluate(include_model_reserve=False)
        if self._mode is SupervisionMode.ENFORCE and not _decision_allows_continuation(decision):
            return decision

        counters = snapshot.counters
        next_counts = _increment_tool_counts(counters.per_tool_counts, names)
        budget_decision = self._tool_batch_budget_decision(
            counters,
            names,
            next_counts,
        )
        if budget_decision is not None and self._mode is SupervisionMode.ENFORCE:
            return budget_decision

        self._replace_snapshot(
            counters=replace(
                counters,
                tool_rounds=counters.tool_rounds + 1,
                tool_calls_requested=counters.tool_calls_requested + len(names),
                per_tool_counts=next_counts,
            )
        )
        self._reserved_tool_names.extend(names)
        self._model_completion_pending = False
        self._batch_had_progress = False
        return budget_decision or decision

    def observe_tool_outcome(self, observation: ToolExecutionObservation) -> SupervisorDecision:
        """Record one reserved tool result and evaluate deterministic detectors.

        记录一个已预留的工具结果,并评估确定性检测器."""

        snapshot = self._require_started()
        if not isinstance(observation, ToolExecutionObservation):
            raise TypeError("observation must be a ToolExecutionObservation")
        if not self._reserved_tool_names:
            raise RuntimeError("tool outcome requires a reserved tool call")
        expected_name = self._reserved_tool_names[0]
        if observation.tool_name != expected_name:
            raise RuntimeError("tool outcome does not match the next reserved tool call")

        self._reserved_tool_names.pop(0)
        fingerprint = observation.fingerprint
        recent = (*snapshot.recent_interactions, fingerprint)[
            -self._thresholds.recent_interaction_window :
        ]
        made_progress, workspace_progress = self._observe_progress(observation, recent[:-1])
        self._batch_had_progress = self._batch_had_progress or made_progress
        no_progress_rounds = snapshot.consecutive_no_progress_rounds
        if not self._reserved_tool_names:
            no_progress_rounds = 0 if self._batch_had_progress else no_progress_rounds + 1

        error_count = snapshot.consecutive_error_count + 1 if observation.is_error else 0
        self._replace_snapshot(
            counters=replace(
                snapshot.counters,
                tool_calls_executed=snapshot.counters.tool_calls_executed + 1,
            ),
            recent_interactions=recent,
            consecutive_error_count=error_count,
            consecutive_no_progress_rounds=no_progress_rounds,
            workspace_progress_count=(snapshot.workspace_progress_count + int(workspace_progress)),
            plan_fingerprint=observation.plan_fingerprint or snapshot.plan_fingerprint,
        )
        return self._evaluate(include_model_reserve=not self._reserved_tool_names)

    def evaluate(self) -> SupervisorDecision:
        """Evaluate all current detectors without mutating a normal running state.

        评估当前所有检测器,不改变正常运行状态."""

        self._require_started()
        return self._evaluate(include_model_reserve=not self._reserved_tool_names)

    def _observe_progress(
        self,
        observation: ToolExecutionObservation,
        prior_recent: Sequence[ToolInteractionFingerprint],
    ) -> tuple[bool, bool]:
        same_as_previous = bool(
            prior_recent
            and prior_recent[-1].behavior_signature == observation.fingerprint.behavior_signature
        )
        workspace_token_changed = _changed_token(
            observation.workspace_progress_token,
            self._last_workspace_token,
        )
        plan_changed = _changed_token(observation.plan_fingerprint, self._last_plan_fingerprint)
        verification_changed = _changed_token(
            observation.verification_token,
            self._last_verification_token,
        )
        external_changed = _changed_token(
            observation.external_state_token,
            self._last_external_state_token,
        )
        if observation.workspace_progress_token is not None:
            self._last_workspace_token = observation.workspace_progress_token
        if observation.plan_fingerprint is not None:
            self._last_plan_fingerprint = observation.plan_fingerprint
        if observation.verification_token is not None:
            self._last_verification_token = observation.verification_token
        if observation.external_state_token is not None:
            self._last_external_state_token = observation.external_state_token

        prior_observation_changed = not any(
            prior.observation_digest == observation.observation_digest
            and prior.is_error == observation.is_error
            for prior in prior_recent
        )
        explicit_progress = (
            observation.progress_kind in {ProgressKind.EVIDENCE, ProgressKind.VERIFICATION}
            and prior_observation_changed
        )
        workspace_progress = (
            observation.workspace_changed
            or workspace_token_changed
            or (observation.progress_kind is ProgressKind.WORKSPACE and not same_as_previous)
        )
        made_progress = (
            workspace_progress
            or plan_changed
            or verification_changed
            or external_changed
            or explicit_progress
        )
        return made_progress, workspace_progress

    def _evaluate(self, *, include_model_reserve: bool) -> SupervisorDecision:
        snapshot = self._require_started()
        if snapshot.status is not AgentExecutionStatus.RUNNING:
            return self._status_decision(snapshot)

        budget_decision = self._usage_budget_decision(snapshot)
        if budget_decision is not None:
            return budget_decision

        recent = snapshot.recent_interactions
        if recent:
            exact_repeat_count = _trailing_count(
                recent,
                recent[-1].behavior_signature,
            )
            error_repeat_count = 0
            if recent[-1].is_error:
                error_repeat_count = _trailing_count(
                    recent,
                    recent[-1].behavior_signature,
                )
            if error_repeat_count >= self._thresholds.repeating_action_error_stuck:
                return self._mark_stuck(
                    SupervisorReasonCode.REPEATED_ACTION_ERROR,
                    "the same tool action and error repeated",
                )
            if exact_repeat_count >= self._thresholds.repeating_action_observation_stuck:
                return self._mark_stuck(
                    SupervisorReasonCode.REPEATED_ACTION_OBSERVATION,
                    "the same tool action and observation repeated",
                )
            if self._has_periodic_cycle(recent):
                return self._mark_stuck(
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    "a repeated tool interaction cycle was detected",
                )
            if snapshot.consecutive_no_progress_rounds >= self._thresholds.no_progress_stuck_rounds:
                return self._mark_stuck(
                    SupervisorReasonCode.NO_PROGRESS,
                    "tool rounds stopped producing meaningful progress",
                )
            if error_repeat_count >= self._thresholds.repeating_action_error:
                return self._replan(
                    SupervisorReasonCode.REPEATED_ACTION_ERROR,
                    "the same tool error requires a new approach",
                )
            if exact_repeat_count >= self._thresholds.repeating_action_observation:
                return self._replan(
                    SupervisorReasonCode.REPEATED_ACTION_OBSERVATION,
                    "the same tool result requires a new approach",
                )
            if (
                snapshot.consecutive_no_progress_rounds
                >= self._thresholds.no_progress_replan_rounds
            ):
                return self._replan(
                    SupervisorReasonCode.NO_PROGRESS,
                    "tool rounds are not producing meaningful progress",
                )

        if include_model_reserve:
            counters = snapshot.counters
            if counters.model_requests >= self._budget.max_model_calls:
                return self._budget_limited(
                    SupervisorReasonCode.MODEL_CALL_BUDGET,
                    "model call budget is exhausted",
                )
        return self._continue_decision()

    def _tool_batch_budget_decision(
        self,
        counters: ExecutionCounters,
        tool_names: Sequence[str],
        next_counts: tuple[ToolCallCount, ...],
    ) -> SupervisorDecision | None:
        if counters.tool_rounds + 1 > self._budget.max_tool_rounds:
            return self._budget_limited(
                SupervisorReasonCode.TOOL_ROUND_BUDGET,
                "tool round budget is exhausted",
            )
        if counters.tool_calls_requested + len(tool_names) > self._budget.max_tool_calls:
            return self._budget_limited(
                SupervisorReasonCode.TOOL_CALL_BUDGET,
                "tool call budget is exhausted",
            )
        for count in next_counts:
            if count.count > self._budget.limit_for_tool(count.tool_name):
                return self._budget_limited(
                    SupervisorReasonCode.PER_TOOL_CALL_BUDGET,
                    "per-tool call budget is exhausted",
                )
        return None

    def _usage_budget_decision(self, snapshot: ExecutionSnapshot) -> SupervisorDecision | None:
        elapsed = self._elapsed_seconds()
        if self._budget.max_wall_seconds is not None and elapsed >= self._budget.max_wall_seconds:
            return self._budget_limited(
                SupervisorReasonCode.WALL_TIME_BUDGET,
                "wall-clock execution budget is exhausted",
            )
        counters = snapshot.counters
        if (
            self._budget.max_input_tokens is not None
            and counters.input_tokens is not None
            and counters.input_tokens >= self._budget.max_input_tokens
        ):
            return self._budget_limited(
                SupervisorReasonCode.INPUT_TOKEN_BUDGET,
                "input token budget is exhausted",
            )
        if (
            self._budget.max_output_tokens is not None
            and counters.output_tokens is not None
            and counters.output_tokens >= self._budget.max_output_tokens
        ):
            return self._budget_limited(
                SupervisorReasonCode.OUTPUT_TOKEN_BUDGET,
                "output token budget is exhausted",
            )
        total_tokens = _total_tokens(counters)
        if (
            self._budget.max_total_tokens is not None
            and total_tokens is not None
            and total_tokens >= self._budget.max_total_tokens
        ):
            return self._budget_limited(
                SupervisorReasonCode.TOTAL_TOKEN_BUDGET,
                "total token budget is exhausted",
            )
        return None

    def _has_periodic_cycle(self, recent: Sequence[ToolInteractionFingerprint]) -> bool:
        repetitions = self._thresholds.alternating_cycle_repetitions
        for period in range(2, self._thresholds.max_cycle_period + 1):
            required = period * repetitions
            if len(recent) < required:
                continue
            pattern = tuple(item.behavior_signature for item in recent[-period:])
            repeated = tuple(item.behavior_signature for item in recent[-required:-period])
            if pattern == repeated:
                return True
        return False

    def _continue_decision(self) -> SupervisorDecision:
        return SupervisorDecision(
            SupervisorDecisionKind.CONTINUE,
            "execution may continue",
            AgentExecutionStatus.RUNNING,
            False,
        )

    def _replan(self, code: SupervisorReasonCode, reason: str) -> SupervisorDecision:
        return SupervisorDecision(
            SupervisorDecisionKind.REPLAN,
            reason,
            AgentExecutionStatus.RUNNING,
            False,
            code,
        )

    def _finalize(self, reason: str) -> SupervisorDecision:
        if self._mode is SupervisionMode.ENFORCE:
            self._replace_snapshot(
                status=AgentExecutionStatus.FINALIZING,
                termination_reason=SupervisorReasonCode.MODEL_CALL_RESERVE,
            )
        return SupervisorDecision(
            SupervisorDecisionKind.FINALIZE,
            reason,
            AgentExecutionStatus.FINALIZING,
            True,
            SupervisorReasonCode.MODEL_CALL_RESERVE,
        )

    def _mark_stuck(self, code: SupervisorReasonCode, reason: str) -> SupervisorDecision:
        if self._mode is SupervisionMode.ENFORCE:
            self._replace_snapshot(status=AgentExecutionStatus.STUCK, termination_reason=code)
        return SupervisorDecision(
            SupervisorDecisionKind.MARK_STUCK,
            reason,
            AgentExecutionStatus.STUCK,
            False,
            code,
        )

    def _budget_limited(self, code: SupervisorReasonCode, reason: str) -> SupervisorDecision:
        if self._mode is SupervisionMode.ENFORCE:
            self._replace_snapshot(
                status=AgentExecutionStatus.BUDGET_LIMITED, termination_reason=code
            )
        return SupervisorDecision(
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
            reason,
            AgentExecutionStatus.BUDGET_LIMITED,
            False,
            code,
        )

    def _status_decision(self, snapshot: ExecutionSnapshot) -> SupervisorDecision:
        code = snapshot.termination_reason or SupervisorReasonCode.NONE
        if snapshot.status is AgentExecutionStatus.FINALIZING:
            return SupervisorDecision(
                SupervisorDecisionKind.FINALIZE,
                "execution is awaiting finalization",
                AgentExecutionStatus.FINALIZING,
                True,
                code,
            )
        if snapshot.status is AgentExecutionStatus.STUCK:
            return SupervisorDecision(
                SupervisorDecisionKind.MARK_STUCK,
                "execution is marked stuck",
                AgentExecutionStatus.STUCK,
                False,
                code,
            )
        if snapshot.status is AgentExecutionStatus.BUDGET_LIMITED:
            return SupervisorDecision(
                SupervisorDecisionKind.MARK_BUDGET_LIMITED,
                "execution is budget limited",
                AgentExecutionStatus.BUDGET_LIMITED,
                False,
                code,
            )
        if snapshot.status is AgentExecutionStatus.BLOCKED:
            return SupervisorDecision(
                SupervisorDecisionKind.BLOCK,
                "execution is blocked",
                AgentExecutionStatus.BLOCKED,
                False,
                code,
            )
        return SupervisorDecision(
            SupervisorDecisionKind.FAIL,
            "execution supervisor reached an unsupported terminal state",
            AgentExecutionStatus.FAILED,
            False,
            SupervisorReasonCode.INTERNAL_FAILURE,
        )

    def _replace_snapshot(
        self,
        *,
        status: AgentExecutionStatus | None = None,
        counters: ExecutionCounters | None = None,
        recent_interactions: tuple[ToolInteractionFingerprint, ...] | None = None,
        consecutive_error_count: int | None = None,
        consecutive_no_progress_rounds: int | None = None,
        workspace_progress_count: int | None = None,
        plan_fingerprint: str | None = None,
        termination_reason: SupervisorReasonCode | None = None,
    ) -> None:
        snapshot = self._require_started()
        self._snapshot = ExecutionSnapshot(
            status=status if status is not None else snapshot.status,
            counters=counters if counters is not None else snapshot.counters,
            elapsed_seconds=self._elapsed_seconds(),
            recent_interactions=(
                recent_interactions
                if recent_interactions is not None
                else snapshot.recent_interactions
            ),
            consecutive_error_count=(
                consecutive_error_count
                if consecutive_error_count is not None
                else snapshot.consecutive_error_count
            ),
            consecutive_no_progress_rounds=(
                consecutive_no_progress_rounds
                if consecutive_no_progress_rounds is not None
                else snapshot.consecutive_no_progress_rounds
            ),
            workspace_progress_count=(
                workspace_progress_count
                if workspace_progress_count is not None
                else snapshot.workspace_progress_count
            ),
            plan_fingerprint=(
                plan_fingerprint if plan_fingerprint is not None else snapshot.plan_fingerprint
            ),
            termination_reason=(
                termination_reason
                if termination_reason is not None
                else snapshot.termination_reason
            ),
        )

    def _elapsed_seconds(self) -> float:
        if self._started_at is None:
            raise RuntimeError("supervisor turn has not started")
        now = self._clock()
        if not math.isfinite(now):
            raise RuntimeError("supervisor clock must return a finite value")
        elapsed = now - self._started_at
        if elapsed < 0:
            raise RuntimeError("supervisor clock moved backwards")
        return elapsed

    def _require_started(self) -> ExecutionSnapshot:
        if self._snapshot is None:
            raise RuntimeError("supervisor turn has not started")
        return self._snapshot


def _accumulate_tokens(
    accumulated: int | None,
    observed: int | None,
    *,
    field_name: str,
) -> int | None:
    if observed is None or accumulated is None:
        return None
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        raise ValueError(f"{field_name} must be a non-negative integer or None")
    return accumulated + observed


def _total_tokens(counters: ExecutionCounters) -> int | None:
    if counters.input_tokens is None or counters.output_tokens is None:
        return None
    return counters.input_tokens + counters.output_tokens


def _increment_tool_counts(
    current: Sequence[ToolCallCount],
    tool_names: Sequence[str],
) -> tuple[ToolCallCount, ...]:
    names = tuple(tool_names)
    result: list[ToolCallCount] = []
    for count in current:
        increment = sum(name == count.tool_name for name in names)
        result.append(ToolCallCount(count.tool_name, count.count + increment))
    current_names = {count.tool_name for count in current}
    for tool_name in sorted(set(names) - current_names):
        result.append(ToolCallCount(tool_name, sum(name == tool_name for name in names)))
    return tuple(sorted(result, key=lambda count: count.tool_name))


def _changed_token(value: str | None, previous: str | None) -> bool:
    return value is not None and value != previous


def _decision_allows_continuation(decision: SupervisorDecision) -> bool:
    """Keep advisory REPLAN decisions observable without blocking Phase 1 telemetry.

    保持建议性的 REPLAN 决策可观察,但不阻塞阶段 1 遥测."""

    return decision.kind in {SupervisorDecisionKind.CONTINUE, SupervisorDecisionKind.REPLAN}


def _trailing_count(
    values: Sequence[ToolInteractionFingerprint],
    expected: tuple[str, str, bool],
) -> int:
    count = 0
    for value in reversed(values):
        if value.behavior_signature != expected:
            break
        count += 1
    return count


__all__ = [
    "DEFAULT_OBSERVATION_BUDGET",
    "MAX_OBSERVATION_SUMMARY_BYTES",
    "AgentExecutionSupervisor",
    "ExecutionControlMode",
    "PathNormalizationContext",
    "StableMetadataFact",
    "SupervisionCheckpoint",
    "SupervisionMode",
    "SupervisionObserver",
    "SupervisionTraceRecord",
    "ToolExecutionObservation",
    "create_observing_supervisor",
    "stable_action_digest",
    "stable_metadata_fact",
    "stable_observation_digest",
]
