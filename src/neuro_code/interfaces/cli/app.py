"""Public CLI facade.

CLI 公共 facade.

The command implementation is split into canonical parser, contract,
interaction, command-family, presentation, session-I/O, and dispatch modules.
This module keeps the established import entry point without owning those
implementations.
"""

from neuro_code.interfaces.cli.dispatch import run
from neuro_code.interfaces.cli.parser import build_parser

__all__ = [
    "build_parser",
    "run",
]
