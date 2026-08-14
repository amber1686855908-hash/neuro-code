"""Windows Firewall authority for installation-scoped sandbox profiles.

The installation owns one persistent outbound block rule scoped to the real
Offline account SID.  Online identity use does not mutate this rule; only
explicit cleanup removes it.  The rule never targets the real controller user.

Windows Firewall authority.

安装实例只拥有一个按真实 Offline account SID 限定的持久 outbound block rule.
Online identity 的使用不会修改该规则,只有显式 cleanup 才会删除它,也绝不作用于
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
from neuro_code.infrastructure.sandbox.windows_sandbox_diagnostics import (
    WindowsSandboxOperationDiagnostic,
    diagnostic_for_exception,
)
from neuro_code.shared.errors import SandboxError


class WindowsFirewallError(SandboxError):
    """A Windows Firewall authority operation failed closed."""

    def __init__(
        self,
        message: str,
        *,
        safe_diagnostic: WindowsSandboxOperationDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_diagnostic = safe_diagnostic or WindowsSandboxOperationDiagnostic(
            None,
            type(self).__name__,
        )


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
    # A managed Offline rule is deliberately broad in every filter that could
    # otherwise narrow the claim from "all Offline outbound traffic" to a
    # subset.  These fields are part of the persisted semantic contract and
    # are also included in the in-memory equality model used by tests.
    protocol: str = "Any"
    local_port: str = "Any"
    remote_port: str = "Any"
    local_address: str = "Any"
    remote_address: str = "Any"
    program: str = "Any"
    service: str = "Any"
    interface_alias: str = "Any"
    interface_type: str = "Any"

    def __post_init__(self) -> None:
        if not self.name or "\x00" in self.name or len(self.name) > 240:
            raise ValueError("firewall rule name must be bounded and NUL-free")
        if not isinstance(self.identity, WindowsSandboxIdentityKind):
            raise TypeError("firewall identity must be canonical")
        if not isinstance(self.sid, WindowsAccountSid):
            raise TypeError("firewall rule SID must be a real account SID")
        if type(self.outbound_block) is not bool:
            raise TypeError("firewall outbound_block must be boolean")
        semantic_values = (
            self.direction,
            self.action,
            self.profile,
            self.protocol,
            self.local_port,
            self.remote_port,
            self.local_address,
            self.remote_address,
            self.program,
            self.service,
            self.interface_alias,
            self.interface_type,
        )
        if not all(isinstance(value, str) for value in semantic_values):
            raise TypeError("firewall rule semantic fields must be strings")
        if self.direction.casefold() != "outbound":
            raise ValueError("managed firewall rule must be outbound")
        if self.action.casefold() != "block":
            raise ValueError("managed firewall rule must block")
        if type(self.enabled) is not bool or not self.enabled:
            raise ValueError("managed firewall rule must be enabled")
        if self.profile.casefold() != "any":
            raise ValueError("managed firewall rule must use the Any profile")
        if any(value.casefold() != "any" for value in semantic_values[3:]):
            raise ValueError("managed firewall rule filters must remain broad")


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
        observed = self.rules.get(rule.name)
        if observed is None:
            return False
        try:
            return observed == rule
        except AttributeError:
            # A malformed/drifted test or persisted projection must fail
            # closed rather than turn readiness inspection into an exception.
            return False


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

    def _run(
        self,
        arguments: list[str],
        *,
        check: bool,
        operation: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
            raise WindowsFirewallError(
                "Windows Firewall setup command failed",
                safe_diagnostic=diagnostic_for_exception(error, operation=operation),
            ) from error
        if not isinstance(result, subprocess.CompletedProcess):
            raise WindowsFirewallError(
                "Windows Firewall setup command returned an invalid result",
                safe_diagnostic=WindowsSandboxOperationDiagnostic(
                    operation,
                    "InvalidCompletedProcess",
                ),
            )
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
            "-Action Block -Enabled True -Profile Any -Protocol Any "
            "-LocalPort Any -RemotePort Any -LocalAddress Any -RemoteAddress Any "
            "-Program Any -Service Any -InterfaceType Any -LocalUser $sddl | Out-Null"
        )
        self._run(["-Command", script], check=True, operation="ENSURE")

    def remove_rule(self, rule: WindowsFirewallRule) -> None:
        script = f"Remove-NetFirewallRule -Name {self._ps_quote(rule.name)} -ErrorAction SilentlyContinue"
        self._run(["-Command", script], check=False, operation="REMOVE")

    def rule_exists(self, rule: WindowsFirewallRule) -> bool:
        script = (
            f"$rule=Get-NetFirewallRule -Name {self._ps_quote(rule.name)} "
            "-ErrorAction SilentlyContinue; "
            "if ($null -eq $rule) { exit 1 }; "
            "$security=@($rule | Get-NetFirewallSecurityFilter); "
            "$port=@($rule | Get-NetFirewallPortFilter); "
            "$address=@($rule | Get-NetFirewallAddressFilter); "
            "$application=@($rule | Get-NetFirewallApplicationFilter); "
            "$service=@($rule | Get-NetFirewallServiceFilter); "
            "$interface=@($rule | Get-NetFirewallInterfaceFilter); "
            "function FilterValues([object[]]$items,[string]$property) { "
            "  if ($null -eq $items -or $items.Count -eq 0) { return @('Any') }; "
            "  $values=@(); "
            "  foreach ($item in $items) { "
            "    $propertyValue=$item.PSObject.Properties[$property]; "
            "    if ($null -eq $propertyValue -or $null -eq $propertyValue.Value) { "
            "      $values += 'Any' "
            "    } else { "
            "      foreach ($value in @($propertyValue.Value)) { "
            "        if ($null -eq $value -or [string]$value -eq '') { $values += 'Any' } "
            "        else { $values += [string]$value } "
            "      } "
            "    } "
            "  }; "
            "  if ($values.Count -eq 0) { return @('Any') }; "
            "  return @($values) "
            "}; "
            "[pscustomobject]@{ "
            "Direction=[string]$rule.Direction; "
            "Action=[string]$rule.Action; "
            "Enabled=[string]$rule.Enabled; "
            "Profile=[string]$rule.Profile; "
            "LocalUser=([string]($security.LocalUser -join ',')); "
            "Protocol=@(FilterValues $port 'Protocol'); "
            "LocalPort=@(FilterValues $port 'LocalPort'); "
            "RemotePort=@(FilterValues $port 'RemotePort'); "
            "LocalAddress=@(FilterValues $address 'LocalAddress'); "
            "RemoteAddress=@(FilterValues $address 'RemoteAddress'); "
            "Program=@(FilterValues $application 'Program'); "
            "Service=@(FilterValues $service 'Service'); "
            "InterfaceAlias=@(FilterValues $interface 'InterfaceAlias'); "
            "InterfaceType=@(FilterValues $interface 'InterfaceType') "
            "} | ConvertTo-Json -Compress"
        )
        result = self._run(["-Command", script], check=False, operation="INSPECT")
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
        if observed_sids != [rule.sid.value.casefold()]:
            return False

        # Values are read from the native filter objects above rather than
        # inferred from the rule object.  Unknown/missing representations fail
        # closed; only the native broad tokens emitted by NetSecurity are
        # accepted as an unrestricted filter.
        expected_filters = {
            "Protocol": rule.protocol,
            "LocalPort": rule.local_port,
            "RemotePort": rule.remote_port,
            "LocalAddress": rule.local_address,
            "RemoteAddress": rule.remote_address,
            "Program": rule.program,
            "Service": rule.service,
            "InterfaceAlias": rule.interface_alias,
            "InterfaceType": rule.interface_type,
        }
        for field, expected in expected_filters.items():
            if expected.casefold() != "any":
                return False
            value = observed.get(field)
            if isinstance(value, list):
                values = value
            elif isinstance(value, str):
                values = [value]
            else:
                return False
            if not values or any(
                not isinstance(item, str) or item.casefold() not in {"any", "*"} for item in values
            ):
                return False
        return True


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
