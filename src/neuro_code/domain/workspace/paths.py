"""Domain path values shared by workspace guidance models.

定义工作区指引领域模型共享的路径值.
"""

from __future__ import annotations

from pathlib import PurePosixPath


def normalize_relative_path(path: PurePosixPath) -> str:
    """Return a normalized POSIX-style relative path string.

    返回规范化的 POSIX 风格相对路径字符串.
    """

    return str(path)


__all__ = ["normalize_relative_path"]
