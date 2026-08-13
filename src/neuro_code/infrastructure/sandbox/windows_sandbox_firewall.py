"""Windows Firewall authority for installation-scoped sandbox profiles.

Offline mode owns one outbound block rule scoped to the real Offline account SID.
Online mode removes only that exact managed rule; it does not add a global
allow rule and never targets the real controller user.

Windows Firewall authority.

Offline mode 只拥有一个按真实 Offline account SID 限定的 outbound block rule.
Online mode 只删除该 exact managed rule,不添加 global allow rule,也绝不作用于
真实 controller user.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from neuro_code.application.ports.windows_sandbox import WindowsSandboxIdentityKind
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import WindowsAccountSid
from neuro_code.shared.errors import SandboxError


class WindowsFirewallError(SandboxError):
    """A Windows Firewall authority operation failed closed."""


@dataclass(frozen=True, slots=True)
class WindowsFirewallRule:
    """Complete managed rule semantics without exposing a controller identity."""

    name: str
    identity: WindowsSandboxIdentityKind
    sid: WindowsAccountSid
    outbound_block: bool
    direction: str = "Outbound"
    action: str = "Block"
    enabled: bool = True
    profile: str = "Any"

    def __post_init__(self) -> None:
        if not self.name or "\x00" in self.name or len(self.name) > 240:
            raise ValueError("firewall rule name must be bounded and NUL-free")
        if not isinstance(self.identity, WindowsSandboxIdentityKind):
            raise TypeError("firewall identity must be canonical")
        if not isinstance(self.sid, WindowsAccountSid):
            raise TypeError("firewall rule SID must be a real account SID")
        if type(self.outbound_block) is not bool:
            raise TypeError("firewall outbound_block must be boolean")
        if not all(isinstance(value, str) for value in (self.direction, self.action, self.profile)):
            raise TypeError("firewall rule semantic fields must be strings")
        if self.direction.casefold() != "outbound":
            raise ValueError("managed firewall rule must be outbound")
        if self.action.casefold() != "block":
            raise ValueError("managed firewall rule must block")
        if type(self.enabled) is not bool or not self.enabled:
            raise ValueError("managed firewall rule must be enabled")
        if self.profile.casefold() != "any":
            raise ValueError("managed firewall rule must use the Any profile")


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
        # Equality intentionally includes every managed semantic, not just the
        # stable name and principal.
        return self.rules.get(rule.name) == rule


class _NativeWindowsFirewallApi:  # pragma: no cover - exercised by Windows native CI
    """Narrow setup-time Windows Firewall adapter; never used for child launch.

    ``New-NetFirewallRule -LocalUser`` is the documented user-SID/WFP policy
    surface.  A PowerShell process is only the setup transport; no proxy, PATH,
    or Git-specific behavior is involved.
    """

    def __init__(self, *, runner: object | None = None) -> None:
        if os.name != "nt":
            raise WindowsFirewallError("native Windows Firewall is available only on Windows")
        self._runner = cast(
            Callable[..., subprocess.CompletedProcess[str]],
            subprocess.run if runner is None else runner,
        )

    @property
    def _powershell(self) -> str:
        system_root = os.environ.get("SYSTEMROOT")
        if not system_root:
            raise WindowsFirewallError("SystemRoot is unavailable for Windows Firewall setup")
        return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

    @staticmethod
    def _ps_quote(value: str) -> str:
        if any(control in value for control in ("\x00", "\r", "\n")):
            raise WindowsFirewallError("firewall value contains a control character")
        return "'" + value.replace("'", "''") + "'"

    def _run(self, arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                [
                    self._powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    *arguments,
                ],
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
        script = (
            f"$sid={self._ps_quote(rule.sid.value)}; "
            f"$name={self._ps_quote(rule.name)}; "
            "$sddl=('O:LSD:(A;;CC;;;{0})' -f $sid); "
            "Remove-NetFirewallRule -Name $name -ErrorAction SilentlyContinue; "
            "New-NetFirewallRule -Name $name -DisplayName $name -Direction Outbound "
            "-Action Block -Enabled True -Profile Any -Protocol Any -LocalUser $sddl | Out-Null"
        )
        self._run(["-Command", script], check=True)

    def remove_rule(self, rule: WindowsFirewallRule) -> None:
        script = f"Remove-NetFirewallRule -Name {self._ps_quote(rule.name)} -ErrorAction SilentlyContinue"
        self._run(["-Command", script], check=False)

    def rule_exists(self, rule: WindowsFirewallRule) -> bool:
        script = (
            f"$rule=Get-NetFirewallRule -Name {self._ps_quote(rule.name)} "
            "-ErrorAction SilentlyContinue; "
            "if ($null -eq $rule) { exit 1 }; "
            "$filter=$rule | Get-NetFirewallSecurityFilter; "
            "[pscustomobject]@{ "
            "Direction=[string]$rule.Direction; "
            "Action=[string]$rule.Action; "
            "Enabled=[string]$rule.Enabled; "
            "Profile=[string]$rule.Profile; "
            "LocalUser=([string]($filter.LocalUser -join ',')) "
            "} | ConvertTo-Json -Compress"
        )
        result = self._run(["-Command", script], check=False)
        if result.returncode != 0:
            return False
        try:
            observed = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(observed, dict):
            return False
        if str(observed.get("Direction", "")).casefold() != rule.direction.casefold():
            return False
        if str(observed.get("Action", "")).casefold() != rule.action.casefold():
            return False
        if str(observed.get("Enabled", "")).casefold() not in {
            "true",
            "enabled",
        }:
            return False
        if str(observed.get("Profile", "")).casefold() != rule.profile.casefold():
            return False
        local_user = str(observed.get("LocalUser", ""))
        observed_sids = [sid.casefold() for sid in re.findall(r"S-1-(?:\d+-)+\d+", local_user)]
        return observed_sids == [rule.sid.value.casefold()]


def firewall_rule_for_installation(
    installation_id: str,
    identity: WindowsSandboxIdentityKind,
    offline_user_sid: WindowsAccountSid,
) -> WindowsFirewallRule:
    """Return a stable, SID-scoped rule name for one installation identity."""

    if not installation_id or "\x00" in installation_id:
        raise ValueError("installation_id must be non-empty and NUL-free")
    name = f"NeuroCode Sandbox W2 {installation_id} {identity.value}"
    if not isinstance(offline_user_sid, WindowsAccountSid):
        raise TypeError("offline firewall identity must be a real account SID")
    if identity is not WindowsSandboxIdentityKind.OFFLINE:
        raise ValueError("the managed outbound block rule is owned by Offline only")
    return WindowsFirewallRule(
        name=name,
        identity=identity,
        sid=offline_user_sid,
        outbound_block=True,
    )


__all__ = [
    "InMemoryWindowsFirewallApi",
    "WindowsFirewallApi",
    "WindowsFirewallError",
    "WindowsFirewallRule",
    "_NativeWindowsFirewallApi",
    "firewall_rule_for_installation",
]
