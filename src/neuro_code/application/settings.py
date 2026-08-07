"""Interface-neutral settings for composing one Neuro Code process.

提供组合一个 Neuro Code 进程所需的接口无关设置."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neuro_code.application.permissions.policy import PermissionMode, PermissionRule
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Interface-neutral settings for composing one Neuro Code process.

    提供组合一个 Neuro Code 进程所需的接口无关设置."""

    cwd: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    sandbox: str | None = None
    failover: bool = True
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    permission_rules: tuple[PermissionRule, ...] = ()
    max_steps: int = 24
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    execution_control_mode: ExecutionControlMode = ExecutionControlMode.FINALIZE_TERMINAL
    resume_id: str | None = None
    launch_command: tuple[str, ...] = ()
