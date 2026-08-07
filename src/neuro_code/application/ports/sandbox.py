"""Canonical shell-sandbox port.

定义规范的 Shell 沙箱端口."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.sandbox.models import SandboxProfile


@dataclass(frozen=True, slots=True)
class ShellLaunch:
    """An argv-safe shell launch prepared by a platform sandbox adapter.

    表示由平台沙箱适配器准备的 argv 安全 Shell 启动请求."""

    executable: str
    arguments: tuple[str, ...]


class ShellSandbox(Protocol):
    @property
    def profile(self) -> SandboxProfile: ...

    def shell_launch(self, command: str) -> ShellLaunch: ...

    def exec_launch(self, executable: str, arguments: tuple[str, ...]) -> ShellLaunch: ...
