"""Windows native sandbox identity and capability foundation.

This module deliberately stops before user provisioning, ACL mutation, DPAPI,
or firewall setup.  It provides only the typed synthetic SID representation
and the W1 capability declaration consumed by the restricted-token layer.

Windows native 沙箱 identity 与 capability foundation.

本模块刻意停在用户 provisioning、ACL 修改、DPAPI 和 firewall setup 之前,只提供
受限 token 层使用的 typed synthetic SID 表示和 W1 capability 声明.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from neuro_code.application.ports.sandbox import (
    LocalProcessSecurityCapabilities,
    LocalProcessSecurityStrength,
)

_SYNTHETIC_SID_PATTERN = re.compile(r"^S-1-5-21-(\d+)-(\d+)-(\d+)-(\d+)$")
_UINT32_MAX = (1 << 32) - 1


@dataclass(frozen=True, slots=True)
class SyntheticWindowsSid:
    """A validated synthetic Windows SID string.

    Windows accepts a valid SID in an ACL even when it is not the SID of a
    provisioned user.  W1 keeps this value in memory and does not persist or
    apply ACLs; later setup layers may use the same typed value.

    已验证的 synthetic Windows SID 字符串.即使 SID 不属于已 provisioning 的用户,
    Windows 仍可在 ACL 中使用有效 SID.W1 只在内存中保存该值,不持久化或应用 ACL;
    后续 setup layer 可以复用同一 typed value.
    """

    value: str

    def __post_init__(self) -> None:
        match = _SYNTHETIC_SID_PATTERN.fullmatch(self.value)
        if match is None:
            raise ValueError("synthetic Windows SID must use the S-1-5-21-<u32>x4 form")
        components = tuple(int(component) for component in match.groups())
        if any(component > _UINT32_MAX for component in components):
            raise ValueError("synthetic Windows SID components must be unsigned 32-bit values")
        canonical = "S-1-5-21-" + "-".join(str(component) for component in components)
        object.__setattr__(self, "value", canonical)

    @classmethod
    def from_components(cls, components: tuple[int, int, int, int]) -> SyntheticWindowsSid:
        """Build a SID from four deterministic unsigned 32-bit components."""

        if len(components) != 4 or any(
            not isinstance(component, int)
            or isinstance(component, bool)
            or component < 0
            or component > _UINT32_MAX
            for component in components
        ):
            raise ValueError("synthetic Windows SID requires four unsigned 32-bit components")
        return cls("S-1-5-21-" + "-".join(str(component) for component in components))

    @classmethod
    def generate(cls) -> SyntheticWindowsSid:
        """Generate one non-persistent synthetic SID for a sandbox identity."""

        return cls.from_components(
            (
                secrets.randbits(32),
                secrets.randbits(32),
                secrets.randbits(32),
                secrets.randbits(32),
            )
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class WindowsSandboxIdentity:
    """W1 in-memory identity used for write-restricted token requests."""

    write_sid: SyntheticWindowsSid

    @classmethod
    def generate(cls) -> WindowsSandboxIdentity:
        """Create one fresh identity without provisioning a Windows user."""

        return cls(write_sid=SyntheticWindowsSid.generate())

    @property
    def restricted_sids(self) -> tuple[SyntheticWindowsSid, ...]:
        """Return the restricted SID list contributed by this identity."""

        return (self.write_sid,)


# W1 owns only primitives.  Enabled Windows profiles still fail closed, so no
# filesystem or network security capability is currently provided at runtime.
WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES = LocalProcessSecurityCapabilities()

# This is an architecture target, not a runtime provider declaration.  A
# future profile adapter must prove each axis before exposing this value.
WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES = LocalProcessSecurityCapabilities(
    read_isolation=LocalProcessSecurityStrength.LIMITED,
    write_isolation=LocalProcessSecurityStrength.STRONG,
    network_isolation=LocalProcessSecurityStrength.STRONG,
)


def windows_native_sandbox_actual_capabilities() -> LocalProcessSecurityCapabilities:
    """Return W1's actual provided filesystem/network capability declaration."""

    return WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES


__all__ = [
    "WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES",
    "WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES",
    "SyntheticWindowsSid",
    "WindowsSandboxIdentity",
    "windows_native_sandbox_actual_capabilities",
]
