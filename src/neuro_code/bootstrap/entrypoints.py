"""Process launchers for the Neuro Code distribution.

进程入口只负责把命令行交给 bootstrap-selected services.
"""

from __future__ import annotations


def main(argv: list[str] | tuple[str, ...] | None = None) -> int:
    """Run the canonical command-line entry point."""

    from neuro_code.bootstrap.cli import BootstrapCliServices
    from neuro_code.interfaces.cli.app import run

    return run(argv, services=BootstrapCliServices())


__all__ = ["main"]
