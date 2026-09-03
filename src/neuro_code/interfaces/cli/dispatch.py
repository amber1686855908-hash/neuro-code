"""Top-level CLI routing and stable process-level error mapping.

CLI 顶层路由以及稳定的进程级错误映射.

The dispatcher coordinates canonical command handlers but does not define
their parser grammar, application behavior, or presentation implementations.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from neuro_code.interfaces.cli.agent import run_agent
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.interfaces.cli.inspection import (
    run_completions_command,
    run_inspect_command,
    run_providers_command,
    run_version_command,
)
from neuro_code.interfaces.cli.parser import build_parser
from neuro_code.interfaces.cli.session_io import export_session, import_session
from neuro_code.interfaces.cli.sessions import run_sessions_command
from neuro_code.interfaces.cli.settings import _application_settings
from neuro_code.interfaces.cli.subagents import run_subagent, run_subagent_lifecycle
from neuro_code.shared.errors import ConfigurationError, NeuroCodeError


async def _run_acp(args: argparse.Namespace, services: CliServices) -> int:
    return await services.run_acp(args, _application_settings(args))


def run(argv: Sequence[str] | None, *, services: CliServices) -> int:
    """Parse, dispatch, and render CLI responses with injected services.

    解析、分发并使用注入的服务渲染 CLI 响应.
    """
    parser = build_parser()
    args = parser.parse_args(tuple(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "version":
            return run_version_command(args)
        if args.command == "inspect":
            return run_inspect_command(args, services)
        if args.command == "completions":
            return run_completions_command(args)
        if args.command == "providers":
            return run_providers_command(args, services)
        if args.command == "sessions":
            return asyncio.run(run_sessions_command(args, services))
        if args.command == "export":
            return asyncio.run(export_session(args, services))
        if args.command == "import-session":
            return asyncio.run(import_session(args, services))
        if args.command == "acp":
            return asyncio.run(_run_acp(args, services))
        if args.command == "subagent":
            return asyncio.run(run_subagent(args, services))
        if args.command == "subagents":
            return asyncio.run(run_subagent_lifecycle(args, services))
        if args.command in {None, "code"} and args.prompt is None:
            return asyncio.run(services.run_tui(args, _application_settings(args)))
        return asyncio.run(run_agent(args, services))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except NeuroCodeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


__all__ = ["run"]
