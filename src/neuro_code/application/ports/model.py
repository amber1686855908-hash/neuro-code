"""Canonical model-provider port.

定义规范的模型 Provider 端口."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelEvent
from neuro_code.domain.tools import ToolDefinition


class ModelToolPolicy(StrEnum):
    """Tool capability policy for one model-provider request.

    定义一次模型 Provider 请求的工具能力策略."""

    ALLOWED = "allowed"
    DISABLED = "disabled"


class ModelCapability(StrEnum):
    """Capability names shared by services, models, and provider adapters."""

    FUNCTION_TOOLS = "function_tools"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    REASONING = "reasoning"
    VISION = "vision"
    STRUCTURED_OUTPUT = "structured_output"
    PROMPT_CACHE = "prompt_cache"
    HOSTED_WEB_SEARCH = "hosted_web_search"
    HOSTED_WEB_FETCH = "hosted_web_fetch"
    HOSTED_CODE_INTERPRETER = "hosted_code_interpreter"
    HOSTED_X_SEARCH = "hosted_x_search"
    MIXED_HOSTED_AND_CLIENT_TOOLS = "mixed_hosted_and_client_tools"


class CapabilityStatus(StrEnum):
    """Fail-closed status of one capability."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


_ALL_MODEL_CAPABILITIES = frozenset(ModelCapability)
_HOSTED_CAPABILITY_BY_TOOL_NAME: Mapping[str, ModelCapability] = MappingProxyType(
    {
        "web_search": ModelCapability.HOSTED_WEB_SEARCH,
        "web_fetch": ModelCapability.HOSTED_WEB_FETCH,
        "code_interpreter": ModelCapability.HOSTED_CODE_INTERPRETER,
        "x_search": ModelCapability.HOSTED_X_SEARCH,
    }
)


@dataclass(frozen=True, slots=True)
class ModelCapabilitySet:
    """Immutable tri-state capability set with fail-closed support checks.

    Unknown capabilities are intentionally retained.  ``supports`` returns
    ``True`` only for explicitly supported capabilities, so a new provider or
    model never receives a feature merely because its protocol happens to be
    compatible.
    """

    supported: frozenset[ModelCapability] = field(default_factory=frozenset)
    unsupported: frozenset[ModelCapability] = field(default_factory=frozenset)
    unknown: frozenset[ModelCapability] = field(
        default_factory=lambda: frozenset(_ALL_MODEL_CAPABILITIES)
    )

    def __post_init__(self) -> None:
        supported = frozenset(self.supported)
        unsupported = frozenset(self.unsupported)
        unknown = frozenset(self.unknown)
        all_values = supported | unsupported | unknown
        if not all(isinstance(value, ModelCapability) for value in all_values):
            raise TypeError("capability sets must contain ModelCapability values")
        if supported & unsupported:
            raise ValueError("capability status sets must be disjoint")
        unknown -= supported | unsupported
        missing = _ALL_MODEL_CAPABILITIES - all_values
        unknown |= missing
        object.__setattr__(self, "supported", supported)
        object.__setattr__(self, "unsupported", unsupported)
        object.__setattr__(self, "unknown", unknown)

    @classmethod
    def all_unknown(cls) -> ModelCapabilitySet:
        """Return a set with every capability unresolved."""

        return cls(unknown=_ALL_MODEL_CAPABILITIES)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str | ModelCapability, str | CapabilityStatus],
    ) -> ModelCapabilitySet:
        """Build a set from bounded, non-secret metadata."""

        supported: set[ModelCapability] = set()
        unsupported: set[ModelCapability] = set()
        unknown: set[ModelCapability] = set()
        for raw_capability, raw_status in values.items():
            try:
                capability = (
                    raw_capability
                    if isinstance(raw_capability, ModelCapability)
                    else ModelCapability(raw_capability)
                )
                status = (
                    raw_status
                    if isinstance(raw_status, CapabilityStatus)
                    else CapabilityStatus(raw_status)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("invalid model capability metadata") from error
            {
                CapabilityStatus.SUPPORTED: supported,
                CapabilityStatus.UNSUPPORTED: unsupported,
                CapabilityStatus.UNKNOWN: unknown,
            }[status].add(capability)
        return cls(frozenset(supported), frozenset(unsupported), frozenset(unknown))

    @classmethod
    def from_supported(cls, *capabilities: ModelCapability) -> ModelCapabilitySet:
        return cls(supported=frozenset(capabilities))

    def status(self, capability: ModelCapability) -> CapabilityStatus:
        if capability in self.supported:
            return CapabilityStatus.SUPPORTED
        if capability in self.unsupported:
            return CapabilityStatus.UNSUPPORTED
        return CapabilityStatus.UNKNOWN

    def supports(self, capability: ModelCapability) -> bool:
        """Return true only for an explicitly supported capability."""

        return self.status(capability) is CapabilityStatus.SUPPORTED

    def meet(self, other: ModelCapabilitySet) -> ModelCapabilitySet:
        """Return the fail-closed intersection of two capability facts.

        A capability is supported only when both sides explicitly support it.
        Any unsupported side wins; all other disagreements remain unknown.
        """

        statuses: dict[str | ModelCapability, str | CapabilityStatus] = {}
        for capability in _ALL_MODEL_CAPABILITIES:
            left = self.status(capability)
            right = other.status(capability)
            statuses[capability] = (
                CapabilityStatus.UNSUPPORTED
                if CapabilityStatus.UNSUPPORTED in {left, right}
                else CapabilityStatus.SUPPORTED
                if left is CapabilityStatus.SUPPORTED and right is CapabilityStatus.SUPPORTED
                else CapabilityStatus.UNKNOWN
            )
        return self.from_mapping(statuses)

    @classmethod
    def intersection(cls, values: Iterable[ModelCapabilitySet]) -> ModelCapabilitySet:
        """Return the fail-closed intersection of a non-empty capability sequence."""

        iterator = iter(values)
        try:
            result = next(iterator)
        except StopIteration as error:
            raise ValueError("capability intersection requires at least one value") from error
        for value in iterator:
            result = result.meet(value)
        return result

    def restrict(self, restrictions: ModelCapabilitySet) -> ModelCapabilitySet:
        """Apply explicit configuration disables without allowing elevation.

        ``SUPPORTED`` and ``UNKNOWN`` configuration values are preferences or
        claims only.  They cannot turn an upstream/implementation unknown or
        unsupported capability into a supported one.
        """

        statuses: dict[str | ModelCapability, str | CapabilityStatus] = {
            capability: self.status(capability) for capability in _ALL_MODEL_CAPABILITIES
        }
        for capability in _ALL_MODEL_CAPABILITIES:
            if restrictions.status(capability) is CapabilityStatus.UNSUPPORTED:
                statuses[capability] = CapabilityStatus.UNSUPPORTED
        return self.from_mapping(statuses)

    def overlay(self, override: ModelCapabilitySet) -> ModelCapabilitySet:
        """Refine a broader fact without allowing unsupported elevation."""

        statuses: dict[str | ModelCapability, str | CapabilityStatus] = {
            capability: self.status(capability) for capability in _ALL_MODEL_CAPABILITIES
        }
        for capability in _ALL_MODEL_CAPABILITIES:
            status = override.status(capability)
            if status is CapabilityStatus.UNSUPPORTED or (
                status is CapabilityStatus.SUPPORTED
                and statuses[capability] is not CapabilityStatus.UNSUPPORTED
            ):
                statuses[capability] = status
        return self.from_mapping(statuses)

    def with_supported(self, *capabilities: ModelCapability) -> ModelCapabilitySet:
        statuses: dict[str | ModelCapability, str | CapabilityStatus] = {
            capability: self.status(capability) for capability in _ALL_MODEL_CAPABILITIES
        }
        for capability in capabilities:
            statuses[capability] = CapabilityStatus.SUPPORTED
        return self.from_mapping(statuses)

    def with_hosted_tool_names(self, names: Iterable[str]) -> ModelCapabilitySet:
        """Expose only canonical hosted capabilities named by a wire adapter."""

        capabilities = tuple(
            capability
            for name in names
            if (capability := _HOSTED_CAPABILITY_BY_TOOL_NAME.get(name)) is not None
        )
        return self.with_supported(*capabilities) if capabilities else self

    def to_mapping(self, *, include_unknown: bool = True) -> dict[str, str]:
        statuses = {
            capability.value: self.status(capability).value
            for capability in sorted(_ALL_MODEL_CAPABILITIES, key=lambda value: value.value)
        }
        if not include_unknown:
            statuses = {
                name: status
                for name, status in statuses.items()
                if status != CapabilityStatus.UNKNOWN.value
            }
        return statuses


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Provenance-preserving capability resolution across trusted boundaries."""

    upstream: ModelCapabilitySet
    implementation: ModelCapabilitySet
    configuration: ModelCapabilitySet
    effective: ModelCapabilitySet

    def status(self, capability: ModelCapability) -> CapabilityStatus:
        return self.effective.status(capability)

    def to_mapping(self, *, include_unknown: bool = True) -> dict[str, dict[str, str]]:
        return {
            "upstream": self.upstream.to_mapping(include_unknown=include_unknown),
            "implementation": self.implementation.to_mapping(include_unknown=include_unknown),
            "configuration": self.configuration.to_mapping(include_unknown=include_unknown),
            "effective": self.effective.to_mapping(include_unknown=include_unknown),
        }


def resolve_capabilities(
    *,
    upstream: ModelCapabilitySet,
    implementation: ModelCapabilitySet,
    configuration: ModelCapabilitySet | None = None,
) -> CapabilityResolution:
    """Resolve executable capabilities without trusting metadata as runtime proof.

    The effective result is the intersection of upstream facts and trusted
    adapter expressibility, followed by explicit configuration restrictions.
    Configuration metadata can disable a capability but cannot elevate one.
    """

    resolved_configuration = configuration or ModelCapabilitySet.all_unknown()
    implementation_result = upstream.meet(implementation)
    effective = implementation_result.restrict(resolved_configuration)
    return CapabilityResolution(
        upstream=upstream,
        implementation=implementation,
        configuration=resolved_configuration,
        effective=effective,
    )


class ModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def context_affinity(self) -> str | None: ...

    @property
    def capabilities(self) -> ModelCapabilitySet: ...

    def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]: ...


__all__ = [
    "CapabilityResolution",
    "CapabilityStatus",
    "ModelCapability",
    "ModelCapabilitySet",
    "ModelProvider",
    "ModelToolPolicy",
    "resolve_capabilities",
]
