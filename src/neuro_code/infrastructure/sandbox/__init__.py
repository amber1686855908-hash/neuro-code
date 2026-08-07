"""Sandbox and process infrastructure adapters.

Concrete platform modules stay lazy so importing one platform primitive does not
eagerly load a different platform's process implementation.

沙箱与进程基础设施适配器.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neuro_code.infrastructure.sandbox.process_tree import ProcessTree

__all__ = ["ProcessTree"]


def __getattr__(name: str) -> type[ProcessTree]:
    if name != "ProcessTree":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from neuro_code.infrastructure.sandbox.process_tree import ProcessTree

    return ProcessTree
