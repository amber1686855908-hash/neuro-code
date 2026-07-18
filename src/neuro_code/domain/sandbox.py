from __future__ import annotations

from enum import StrEnum


class SandboxProfile(StrEnum):
    """Canonical process sandbox profiles exposed at configuration boundaries."""

    OFF = "off"
    WORKSPACE = "workspace"
    READ_ONLY = "read-only"
    STRICT = "strict"

    @classmethod
    def parse(cls, value: str) -> SandboxProfile:
        normalized = value.strip().casefold()
        aliases = {"none": cls.OFF, "readonly": cls.READ_ONLY}
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as error:
            supported = ", ".join(profile.value for profile in cls)
            raise ValueError(
                f"unsupported sandbox profile {value!r}; choose one of: {supported}"
            ) from error

    @property
    def enabled(self) -> bool:
        return self is not self.OFF

    @property
    def workspace_writable(self) -> bool:
        return self is not self.READ_ONLY

    @property
    def restricts_child_network(self) -> bool:
        return self in {self.READ_ONLY, self.STRICT}
