"""Windows Firewall authority for installation-scoped sandbox profiles.

Offline mode owns one outbound block rule scoped to the synthetic sandbox SID.
Online mode removes only that exact managed rule; it does not add a global
allow rule and never targets the real controller user.

Windows Firewall authority.

Offline mode 只拥有一个按 synthetic sandbox SID 限定的 outbound block rule.
Online mode 只删除该 exact managed rule,不添加 global allow rule,也绝不作用于
真实 controller user.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from neuro_code.application.ports.windows_sandbox import WindowsSandboxIdentityKind
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.shared.errors import SandboxError


class WindowsFirewallError(SandboxError):
    """A Windows Firewall authority operation failed closed."""


@dataclass(frozen=True, slots=True)
class WindowsFirewallRule:
    """Managed rule metadata without exposing a controller identity."""

    name: str
    identity: WindowsSandboxIdentityKind
    sid: SyntheticWindowsSid
    outbound_block: bool

    def __post_init__(self) -> None:
        if not self.name or "\x00" in self.name or len(self.name) > 240:
            raise ValueError("firewall rule name must be bounded and NUL-free")
        if not isinstance(self.identity, WindowsSandboxIdentityKind):
            raise TypeError("firewall identity must be canonical")
        if not isinstance(self.sid, SyntheticWindowsSid):
            raise TypeError("firewall rule SID must be canonical")
        if type(self.outbound_block) is not bool:
            raise TypeError("firewall outbound_block must be boolean")


class WindowsFirewallApi(Protocol):
    """Minimal setup-time firewall mutation/query surface."""

    def ensure_outbound_block(self, rule: WindowsFirewallRule) -> None: ...

    def remove_rule(self, rule: WindowsFirewallRule) -> None: ...

    def rule_exists(self, rule: WindowsFirewallRule) -> bool: ...


class InMemoryWindowsFirewallApi:
    """Deterministic fake used to verify offline/online control and cleanup."""

    def __init__(self) -> None:
        self.rules: dict[str, WindowsFirewallRule] = {}
        self.calls: list[tuple[str, WindowsFirewallRule]] = []

    def ensure_outbound_block(self, rule: WindowsFirewallRule) -> None:
        self.calls.append(("block", rule))
        self.rules[rule.name] = rule

    def remove_rule(self, rule: WindowsFirewallRule) -> None:
        self.calls.append(("remove", rule))
        self.rules.pop(rule.name, None)

    def rule_exists(self, rule: WindowsFirewallRule) -> bool:
        return rule.name in self.rules


class _NativeWindowsFirewallApi:  # pragma: no cover - exercised by Windows native CI
    """Narrow setup-time netsh adapter; it is never used for child launch."""

    def __init__(self, *, runner: object | None = None) -> None:
        if os.name != "nt":
            raise WindowsFirewallError("native Windows Firewall is available only on Windows")
        self._runner = cast(
            Callable[..., subprocess.CompletedProcess[str]],
            subprocess.run if runner is None else runner,
        )

    @property
    def _netsh(self) -> str:
        system_root = os.environ.get("SYSTEMROOT")
        if not system_root:
            raise WindowsFirewallError("SystemRoot is unavailable for Windows Firewall setup")
        return str(Path(system_root) / "System32" / "netsh.exe")

    def _run(self, arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                [self._netsh, "advfirewall", "firewall", *arguments],
                check=check,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WindowsFirewallError("Windows Firewall setup command failed") from error
        if not isinstance(result, subprocess.CompletedProcess):
            raise WindowsFirewallError("Windows Firewall setup command returned an invalid result")
        return result

    def ensure_outbound_block(self, rule: WindowsFirewallRule) -> None:
        if not rule.outbound_block:
            raise ValueError("ensure_outbound_block requires an outbound block rule")
        self._run(
            [
                "add",
                "rule",
                f"name={rule.name}",
                "dir=out",
                "action=block",
                "enable=yes",
                "profile=any",
                f"localuser={rule.sid.value}",
            ],
            check=True,
        )

    def remove_rule(self, rule: WindowsFirewallRule) -> None:
        self._run(["delete", "rule", f"name={rule.name}"], check=False)

    def rule_exists(self, rule: WindowsFirewallRule) -> bool:
        result = self._run(["show", "rule", f"name={rule.name}"], check=False)
        return result.returncode == 0


def firewall_rule_for_installation(
    installation_id: str,
    identity: WindowsSandboxIdentityKind,
    write_sid: SyntheticWindowsSid,
) -> WindowsFirewallRule:
    """Return a stable, SID-scoped rule name for one installation identity."""

    if not installation_id or "\x00" in installation_id:
        raise ValueError("installation_id must be non-empty and NUL-free")
    name = f"NeuroCode Sandbox W2 {installation_id} {identity.value}"
    return WindowsFirewallRule(
        name=name,
        identity=identity,
        sid=write_sid,
        outbound_block=identity is WindowsSandboxIdentityKind.OFFLINE,
    )


__all__ = [
    "InMemoryWindowsFirewallApi",
    "WindowsFirewallApi",
    "WindowsFirewallError",
    "WindowsFirewallRule",
    "_NativeWindowsFirewallApi",
    "firewall_rule_for_installation",
]
