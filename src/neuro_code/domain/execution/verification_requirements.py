"""Immutable, provider-independent verification requirement declarations.

Requirements describe what a task must verify, not how a tool discovers or
executes that verification.  Their identity and bounded snapshot are owned by
the execution domain so application runtime code can consume a stable
declaration without importing providers, tools, filesystems, or persistence.

定义不可变且与 Provider 无关的验证要求声明。

要求描述任务必须验证的内容,而不是工具如何发现或执行验证。其身份和有界
snapshot 由执行领域拥有,使应用运行时可以消费稳定声明而不依赖 Provider、工具、
文件系统或持久化实现。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from neuro_code.shared.redaction import redact_sensitive_text

MAX_VERIFICATION_REQUIREMENTS = 32
MAX_REQUIREMENT_CRITERION_BYTES = 1_024
MAX_REQUIREMENT_SCOPE_ITEMS = 8
MAX_REQUIREMENT_SCOPE_BYTES = 160
MAX_REQUIREMENT_PROVENANCE_ITEMS = 8
MAX_REQUIREMENT_ORIGIN_ID_BYTES = 128
MAX_REQUIREMENT_SNAPSHOT_BYTES = 32 * 1024
MAX_REQUIREMENT_ID_BYTES = len("req-v1-") + 64

_REQUIREMENT_ID = re.compile(r"^req-v1-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1


class RequirementStrength(StrEnum):
    """Whether satisfying a requirement is necessary for task success."""

    REQUIRED = "required"
    ADVISORY = "advisory"


class RequirementActivation(StrEnum):
    """The workspace condition that activates one requirement."""

    ALWAYS = "always"
    ON_WORKSPACE_MUTATION = "on_workspace_mutation"


class RequirementSource(StrEnum):
    """Bounded provenance categories for one requirement declaration."""

    EXPLICIT_USER = "explicit_user"
    TASK_INTENT = "task_intent"
    SAVED_PLAN = "saved_plan"
    WORKSPACE_MUTATION = "workspace_mutation"
    POLICY_INFERENCE = "policy_inference"
    PLANNER_INFERENCE = "planner_inference"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_text(
    value: object,
    *,
    field_name: str,
    limit: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        "\x00" in value
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
        or any(ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must not contain control characters")
    normalized = " ".join(unicodedata.normalize("NFKC", redact_sensitive_text(value)).split())
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} exceeds its UTF-8 byte bound")
    return normalized


def _safe_digest(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or _SHA256.fullmatch(value) is None
        or len(value.encode("utf-8")) != 64
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def require_requirement_id(value: object, *, field_name: str = "requirement_id") -> str:
    """Validate and return a domain-generated canonical requirement ID."""

    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) != MAX_REQUIREMENT_ID_BYTES
        or _REQUIREMENT_ID.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a canonical req-v1 SHA-256 ID")
    return value


def is_requirement_id(value: object) -> bool:
    """Return whether a value has the canonical requirement-ID shape."""

    try:
        require_requirement_id(value)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_scope(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("requirement scope must be a sequence of strings")
    items = tuple(value)
    if len(items) > MAX_REQUIREMENT_SCOPE_ITEMS:
        raise ValueError(
            f"requirement scope must contain at most {MAX_REQUIREMENT_SCOPE_ITEMS} items"
        )
    normalized = {
        _safe_text(
            item,
            field_name="requirement scope item",
            limit=MAX_REQUIREMENT_SCOPE_BYTES,
            allow_empty=True,
        )
        for item in items
    }
    normalized.discard("")
    if len(normalized) > MAX_REQUIREMENT_SCOPE_ITEMS:
        raise ValueError(
            f"requirement scope must contain at most {MAX_REQUIREMENT_SCOPE_ITEMS} items"
        )
    return tuple(sorted(normalized))


def canonical_requirement_payload(
    criterion: str,
    scope: Sequence[str] = (),
    activation: RequirementActivation = RequirementActivation.ALWAYS,
) -> dict[str, object]:
    """Return the exact payload used for canonical requirement identity."""

    normalized_criterion = _safe_text(
        criterion,
        field_name="requirement criterion",
        limit=MAX_REQUIREMENT_CRITERION_BYTES,
    )
    if not isinstance(activation, RequirementActivation):
        raise TypeError("requirement activation must be a RequirementActivation")
    return {
        "activation": activation.value,
        "criterion": normalized_criterion,
        "scope": list(_normalize_scope(scope)),
    }


def canonical_requirement_id(
    criterion: str,
    scope: Sequence[str] = (),
    activation: RequirementActivation = RequirementActivation.ALWAYS,
) -> str:
    """Compute the stable identity, excluding strength and provenance."""

    digest = hashlib.sha256(
        _canonical_json(canonical_requirement_payload(criterion, scope, activation))
    ).hexdigest()
    return f"req-v1-{digest}"


@dataclass(frozen=True, slots=True)
class RequirementProvenance:
    """Bounded source metadata that never participates in requirement identity."""

    source: RequirementSource
    origin_id: str | None = None
    origin_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, RequirementSource):
            raise TypeError("requirement provenance source must be canonical")
        if self.origin_id is not None:
            object.__setattr__(
                self,
                "origin_id",
                _safe_text(
                    self.origin_id,
                    field_name="requirement provenance origin_id",
                    limit=MAX_REQUIREMENT_ORIGIN_ID_BYTES,
                ),
            )
        if self.origin_fingerprint is not None:
            object.__setattr__(
                self,
                "origin_fingerprint",
                _safe_digest(
                    self.origin_fingerprint,
                    field_name="requirement provenance origin_fingerprint",
                ),
            )

    def to_dict(self) -> dict[str, str]:
        value = {"source": self.source.value}
        if self.origin_id is not None:
            value["origin_id"] = self.origin_id
        if self.origin_fingerprint is not None:
            value["origin_fingerprint"] = self.origin_fingerprint
        return value

    @classmethod
    def from_dict(cls, value: object) -> RequirementProvenance:
        if not isinstance(value, Mapping):
            raise ValueError("requirement provenance must be an object")
        try:
            return cls(
                source=RequirementSource(value["source"]),
                origin_id=value.get("origin_id"),
                origin_fingerprint=value.get("origin_fingerprint"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("requirement provenance is invalid") from error


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    """One immutable requirement with a domain-owned canonical identity."""

    criterion: str
    scope: tuple[str, ...] = ()
    strength: RequirementStrength = RequirementStrength.REQUIRED
    activation: RequirementActivation = RequirementActivation.ALWAYS
    provenance: tuple[RequirementProvenance, ...] = ()
    _requirement_id: str = field(init=False, repr=False, compare=True)

    def __post_init__(self) -> None:
        normalized_criterion = _safe_text(
            self.criterion,
            field_name="requirement criterion",
            limit=MAX_REQUIREMENT_CRITERION_BYTES,
        )
        if not isinstance(self.strength, RequirementStrength):
            raise TypeError("requirement strength must be a RequirementStrength")
        if not isinstance(self.activation, RequirementActivation):
            raise TypeError("requirement activation must be a RequirementActivation")
        scope = _normalize_scope(self.scope)
        provenance = tuple(self.provenance)
        if len(provenance) > MAX_REQUIREMENT_PROVENANCE_ITEMS:
            raise ValueError(
                "requirement provenance must contain "
                f"at most {MAX_REQUIREMENT_PROVENANCE_ITEMS} items"
            )
        if not all(isinstance(item, RequirementProvenance) for item in provenance):
            raise TypeError("requirement provenance must contain RequirementProvenance values")
        provenance = tuple(
            sorted(
                set(provenance),
                key=lambda item: (
                    item.source.value,
                    item.origin_id or "",
                    item.origin_fingerprint or "",
                ),
            )
        )
        object.__setattr__(self, "criterion", normalized_criterion)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(
            self,
            "_requirement_id",
            canonical_requirement_id(normalized_criterion, scope, self.activation),
        )

    @classmethod
    def create(
        cls,
        *,
        criterion: str,
        scope: Sequence[str] = (),
        strength: RequirementStrength = RequirementStrength.REQUIRED,
        activation: RequirementActivation = RequirementActivation.ALWAYS,
        provenance: Sequence[RequirementProvenance] = (),
    ) -> VerificationRequirement:
        """Create a requirement while keeping identity generation in this owner."""

        return cls(criterion, tuple(scope), strength, activation, tuple(provenance))

    @property
    def requirement_id(self) -> str:
        return self._requirement_id

    def identity_payload(self) -> dict[str, object]:
        return canonical_requirement_payload(self.criterion, self.scope, self.activation)

    def merge(self, other: VerificationRequirement) -> VerificationRequirement:
        """Merge declarations with one identity, strengthening when necessary."""

        if not isinstance(other, VerificationRequirement):
            raise TypeError("requirement merge needs a VerificationRequirement")
        if (
            self.requirement_id != other.requirement_id
            or self.identity_payload() != other.identity_payload()
        ):
            raise ValueError("requirements with different canonical identities cannot merge")
        provenance = tuple(dict.fromkeys((*self.provenance, *other.provenance)))
        if len(provenance) > MAX_REQUIREMENT_PROVENANCE_ITEMS:
            raise ValueError("merged requirement provenance exceeds its bound")
        return VerificationRequirement.create(
            criterion=self.criterion,
            scope=self.scope,
            strength=(
                RequirementStrength.REQUIRED
                if RequirementStrength.REQUIRED in {self.strength, other.strength}
                else RequirementStrength.ADVISORY
            ),
            activation=self.activation,
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "criterion": self.criterion,
            "scope": list(self.scope),
            "strength": self.strength.value,
            "activation": self.activation.value,
            "provenance": [item.to_dict() for item in self.provenance],
        }

    @classmethod
    def from_dict(cls, value: object) -> VerificationRequirement:
        if not isinstance(value, Mapping):
            raise ValueError("verification requirement must be an object")
        raw_scope = value.get("scope", ())
        raw_provenance = value.get("provenance", ())
        if not isinstance(raw_scope, Sequence) or isinstance(raw_scope, (str, bytes, bytearray)):
            raise ValueError("verification requirement scope must be a sequence")
        if not isinstance(raw_provenance, Sequence) or isinstance(
            raw_provenance,
            (str, bytes, bytearray),
        ):
            raise ValueError("verification requirement provenance must be a sequence")
        try:
            requirement = cls.create(
                criterion=value["criterion"],
                scope=tuple(raw_scope),
                strength=RequirementStrength(value.get("strength", RequirementStrength.REQUIRED)),
                activation=RequirementActivation(
                    value.get("activation", RequirementActivation.ALWAYS)
                ),
                provenance=tuple(RequirementProvenance.from_dict(item) for item in raw_provenance),
            )
            supplied_id = value["requirement_id"]
            require_requirement_id(supplied_id)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("verification requirement is invalid") from error
        if supplied_id != requirement.requirement_id:
            raise ValueError("verification requirement ID does not match its canonical payload")
        return requirement


@dataclass(frozen=True, slots=True)
class VerificationRequirementsSnapshot:
    """A bounded immutable declaration set captured for one logical task."""

    requirements: tuple[VerificationRequirement, ...] = ()

    def __post_init__(self) -> None:
        requirements = tuple(self.requirements)
        if len(requirements) > MAX_VERIFICATION_REQUIREMENTS:
            raise ValueError("verification requirements snapshot contains too many requirements")
        if not all(isinstance(item, VerificationRequirement) for item in requirements):
            raise TypeError(
                "verification requirements snapshot must contain VerificationRequirement values"
            )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ValueError("verification requirements snapshot must not contain duplicate IDs")
        ordered = tuple(sorted(requirements, key=lambda item: item.requirement_id))
        object.__setattr__(self, "requirements", ordered)
        if len(self._canonical_bytes()) > MAX_REQUIREMENT_SNAPSHOT_BYTES:
            raise ValueError("verification requirements snapshot exceeds its byte bound")

    @classmethod
    def from_requirements(
        cls,
        requirements: Sequence[VerificationRequirement],
    ) -> VerificationRequirementsSnapshot:
        """Build a snapshot, merging same-identity declarations deterministically."""

        merged: dict[str, VerificationRequirement] = {}
        for requirement in requirements:
            if not isinstance(requirement, VerificationRequirement):
                raise TypeError("requirements must contain VerificationRequirement values")
            previous = merged.get(requirement.requirement_id)
            merged[requirement.requirement_id] = (
                requirement if previous is None else previous.merge(requirement)
            )
        return cls(tuple(merged.values()))

    @classmethod
    def create(
        cls,
        requirements: Sequence[VerificationRequirement],
    ) -> VerificationRequirementsSnapshot:
        return cls.from_requirements(requirements)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "requirements": [item.to_dict() for item in self.requirements],
        }

    def _canonical_bytes(self) -> bytes:
        return _canonical_json(self._canonical_payload())

    @property
    def fingerprint(self) -> str:
        """Return the bounded digest of the complete declaration snapshot."""

        return hashlib.sha256(self._canonical_bytes()).hexdigest()

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        return tuple(item.requirement_id for item in self.requirements)

    def active_for_generation(
        self, workspace_generation: int
    ) -> tuple[VerificationRequirement, ...]:
        if (
            not isinstance(workspace_generation, int)
            or isinstance(workspace_generation, bool)
            or workspace_generation < 0
        ):
            raise ValueError("workspace_generation must be non-negative")
        return tuple(
            requirement
            for requirement in self.requirements
            if requirement.activation is RequirementActivation.ALWAYS or workspace_generation > 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self._canonical_payload(),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> VerificationRequirementsSnapshot:
        if not isinstance(value, Mapping):
            raise ValueError("verification requirements snapshot must be an object")
        raw_requirements = value.get("requirements", ())
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements,
            (str, bytes, bytearray),
        ):
            raise ValueError("verification requirements must be a sequence")
        try:
            schema_version = value.get("schema_version", _SCHEMA_VERSION)
            if schema_version != _SCHEMA_VERSION:
                raise ValueError("unsupported verification requirements schema version")
            snapshot = cls.from_requirements(
                tuple(VerificationRequirement.from_dict(item) for item in raw_requirements)
            )
            supplied_fingerprint = value.get("fingerprint")
            if supplied_fingerprint is not None:
                _safe_digest(
                    supplied_fingerprint,
                    field_name="verification requirements fingerprint",
                )
        except (TypeError, ValueError) as error:
            raise ValueError("verification requirements snapshot is invalid") from error
        if supplied_fingerprint is not None and supplied_fingerprint != snapshot.fingerprint:
            raise ValueError("verification requirements snapshot fingerprint is inconsistent")
        return snapshot


__all__ = [
    "MAX_REQUIREMENT_CRITERION_BYTES",
    "MAX_REQUIREMENT_ID_BYTES",
    "MAX_REQUIREMENT_ORIGIN_ID_BYTES",
    "MAX_REQUIREMENT_PROVENANCE_ITEMS",
    "MAX_REQUIREMENT_SCOPE_BYTES",
    "MAX_REQUIREMENT_SCOPE_ITEMS",
    "MAX_REQUIREMENT_SNAPSHOT_BYTES",
    "MAX_VERIFICATION_REQUIREMENTS",
    "RequirementActivation",
    "RequirementProvenance",
    "RequirementSource",
    "RequirementStrength",
    "VerificationRequirement",
    "VerificationRequirementsSnapshot",
    "canonical_requirement_id",
    "canonical_requirement_payload",
    "is_requirement_id",
    "require_requirement_id",
]
