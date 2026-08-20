"""Deterministic first-pass failure taxonomy for benchmark attempts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind

from .models import TAXONOMY, TaskSpec
from .verifier import VerifierResult


def _tool_names(events: Iterable[AgentEvent]) -> set[str]:
    names: set[str] = set()
    for event in events:
        if event.kind is not AgentEventKind.TOOL_REQUESTED:
            continue
        name = event.data.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def classify_failure(
    task: TaskSpec,
    verifier: VerifierResult | None,
    events: Iterable[AgentEvent],
    *,
    error: BaseException | None = None,
    timed_out: bool = False,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Return ``primary, secondary, evidence`` without using an LLM judge."""

    event_list = tuple(events)
    names = _tool_names(event_list)
    evidence: list[str] = []
    if error is not None:
        error_name = type(error).__name__
        evidence.append(f"harness exception type={error_name}")
        if timed_out or isinstance(error, TimeoutError):
            return "BUDGET_OR_TIMEOUT", (), tuple(evidence)
        if "Provider" in error_name or "Configuration" in error_name:
            return "PROVIDER_PROTOCOL", (), tuple(evidence)
        return "HARNESS_ERROR", (), tuple(evidence)
    if timed_out:
        return "BUDGET_OR_TIMEOUT", (), ("attempt timeout",)
    if verifier is None:
        return "HARNESS_ERROR", (), ("verifier did not produce a result",)
    evidence.extend(f"verifier: {failure}" for failure in verifier.failures[:4])
    if verifier.timed_out:
        return "BUDGET_OR_TIMEOUT", (), tuple(evidence)
    failed_events = [event for event in event_list if event.kind is AgentEventKind.TOOL_FAILED]
    if failed_events:
        evidence.append(f"tool failures={len(failed_events)}")
        return "TOOL_EXECUTION", (), tuple(evidence)
    if task.web and not (names & {"web_search", "web_fetch"}) and not verifier.passed:
        evidence.append("WEB task completed without a web capability event")
        return "WEB_RESEARCH", (), tuple(evidence)
    if not names and not verifier.passed:
        evidence.append("no tool call observed before verifier failure")
        return "MODEL_REASONING", (), tuple(evidence)
    if not verifier.passed:
        if any("required file missing" in item for item in verifier.failures):
            return "EDIT_FAILURE", (), tuple(evidence)
        return "VERIFICATION_FAILURE", (), tuple(evidence)
    return None, (), tuple(evidence)


def validate_taxonomy(primary: str | None, secondary: Iterable[str]) -> tuple[str, ...]:
    errors: list[str] = []
    values = tuple(secondary)
    for value in ((primary,) if primary is not None else ()) + values:
        if value not in TAXONOMY:
            errors.append(f"unknown taxonomy value: {value}")
    if primary is not None and primary in values:
        errors.append("primary taxonomy must not duplicate a secondary value")
    return tuple(errors)


def summarize_failure_distribution(results: Iterable[Mapping[str, object]]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for result in results:
        failure = result.get("failure")
        if not isinstance(failure, Mapping):
            continue
        primary = failure.get("primary")
        if isinstance(primary, str):
            distribution[primary] = distribution.get(primary, 0) + 1
    return dict(sorted(distribution.items()))


__all__ = ["classify_failure", "summarize_failure_distribution", "validate_taxonomy"]
