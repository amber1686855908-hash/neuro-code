"""Compatibility facade for canonical Bash command analysis.

提供 Bash 命令分析的兼容门面,并转发到规范实现."""

from neuro_code.domain.permissions.bash_commands import (
    BashCommandAnalysis,
    BashCommandSegment,
    analyze_bash_command,
)

__all__ = ["BashCommandAnalysis", "BashCommandSegment", "analyze_bash_command"]
