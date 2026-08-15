"""Windows native sandbox identity and capability foundation.

This module owns the typed synthetic SID representation and the W1 capability
declarations consumed by the restricted-token layer.  A synthetic SID is not a
Windows user and must never be used as a filesystem read principal or firewall
identity.  Installation persistence, real local users, ACL mutation, and
firewall setup live in the separate W2 setup authority; this module
intentionally keeps the runtime actual capability declaration fail closed.

Windows native 沙箱 identity 与 capability foundation.

本模块负责受限 token 层使用的 typed synthetic SID 表示和 W1 capability 声明.
synthetic SID 不是 Windows user, 不能作为文件读取 principal 或 firewall identity.
installation persistence、真实 local user、ACL 修改和 firewall setup 属于独立的 W2
setup authority; 本模块刻意保持 runtime actual capability 失败关闭.
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
    provisioned user.  This SID is reserved for the restricted-token
    ``WRITE_RESTRICTED`` membership and the corresponding write-side ACL
    entry.  It is not an account, read principal, or network identity.

    已验证的 synthetic Windows SID 字符串.即使 SID 不属于已 provisioning 的用户,
    Windows 仍可在 ACL 中使用有效 SID.该 SID 仅用于 restricted token 的
    ``WRITE_RESTRICTED`` membership 和对应写 ACL,不能作为账户,读取 principal 或
    网络 identity.
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


# W1/W2 own only primitives and setup authority.  Their canonical actual
# declaration remains fail-closed until a concrete runtime provider is
# certified; this value must not be used to admit a W3 process.
WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES = LocalProcessSecurityCapabilities()

# Architecture target for the eventual Windows native backend.  It is not a
# runtime provider declaration and must not be used by W1/W2 callers.
WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES = LocalProcessSecurityCapabilities(
    read_isolation=LocalProcessSecurityStrength.LIMITED,
    write_isolation=LocalProcessSecurityStrength.STRONG,
    network_isolation=LocalProcessSecurityStrength.STRONG,
)

# W3's concrete runtime provider declaration.  Native acceptance validates
# this provider by exercising the real child boundary; it is deliberately
# separate from the W1/W2 actual declaration and the architecture target.
WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES = LocalProcessSecurityCapabilities(
    read_isolation=LocalProcessSecurityStrength.LIMITED,
    write_isolation=LocalProcessSecurityStrength.STRONG,
    network_isolation=LocalProcessSecurityStrength.STRONG,
)


def windows_native_sandbox_actual_capabilities() -> LocalProcessSecurityCapabilities:
    """Return W1/W2's fail-closed actual capability declaration."""

    return WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES


def windows_native_sandbox_w3_capabilities() -> LocalProcessSecurityCapabilities:
    """Return the concrete W3 provider declaration under native acceptance."""

    return WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES


__all__ = [
    "WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES",
    "WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES",
    "WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES",
    "SyntheticWindowsSid",
    "WindowsSandboxIdentity",
    "windows_native_sandbox_actual_capabilities",
    "windows_native_sandbox_w3_capabilities",
]
