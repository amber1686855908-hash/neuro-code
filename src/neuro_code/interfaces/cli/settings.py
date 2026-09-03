"""Translate parsed CLI options into application-facing settings.

将已解析的 CLI 选项转换为面向应用的 settings.

Only input normalization belongs here.  Configuration loading and concrete
environment/filesystem resolution remain bootstrap-owned.
"""

from __future__ import annotations

import argparse

from neuro_code.application.execution_policy import ExecutionProfile
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.settings import ApplicationSettings
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.cli.parser import EXECUTION_CONTROL_CHOICES
from neuro_code.shared.errors import ConfigurationError


def _normalize_rule(pattern: str) -> str:
    stripped = pattern.strip()
    if stripped == "Bash":
        return "bash:*"
    if stripped.startswith("Bash(") and stripped.endswith(")"):
        content = stripped[5:-1].strip()
        if not content or content == "*":
            return "bash:*"
        if content.endswith(":*"):
            content = f"{content[:-2]}*"
        return f"bash:{content}"
    return stripped


def _rules(args: argparse.Namespace) -> tuple[PermissionRule, ...]:
    deny = tuple(
        PermissionRule(PermissionEffect.DENY, _normalize_rule(pattern)) for pattern in args.deny
    )
    allow = tuple(
        PermissionRule(PermissionEffect.ALLOW, _normalize_rule(pattern)) for pattern in args.allow
    )
    return deny + allow


def _application_settings(
    args: argparse.Namespace,
    *,
    reasoning_effort: ReasoningEffort | None = None,
) -> ApplicationSettings:
    return ApplicationSettings(
        cwd=args.cwd,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        sandbox=args.sandbox,
        failover=not args.no_failover,
        permission_mode=(
            PermissionMode.BYPASS
            if getattr(args, "always_approve", False)
            else PermissionMode.DEFAULT
        ),
        permission_rules=_rules(args),
        permission_rules_path=getattr(args, "permissions_file", None),
        max_steps=args.max_steps,
        execution_profile=ExecutionProfile(
            getattr(args, "execution_profile", ExecutionProfile.NORMAL.value)
        ),
        execution_control_mode=_execution_control_mode(
            getattr(args, "execution_control", "finalize-terminal")
        ),
        reasoning_effort=(
            ReasoningEffort(args.effort)
            if getattr(args, "effort", None) is not None
            else reasoning_effort or ReasoningEffort.HIGH
        ),
        resume_id=getattr(args, "resume", None),
    )


def _execution_control_mode(value: object) -> ExecutionControlMode:
    if not isinstance(value, str):
        raise ConfigurationError("execution control selection is invalid")
    try:
        return EXECUTION_CONTROL_CHOICES[value]
    except KeyError:
        raise ConfigurationError("execution control selection is invalid") from None


__all__ = ["_application_settings", "_execution_control_mode", "_normalize_rule", "_rules"]
