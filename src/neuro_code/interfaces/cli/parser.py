"""Canonical argparse grammar for the Neuro Code CLI.

Neuro Code CLI 的规范 argparse grammar.

This module owns command registration and argument defaults.  It does not
dispatch commands or construct concrete services.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from neuro_code import __version__
from neuro_code.application.execution_policy import ExecutionProfile
from neuro_code.application.ports.tools import MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions.subagent_lifecycle import SubagentRelationshipAction
from neuro_code.application.workflows.subagent import MAX_SUBAGENT_STEPS
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile

EXECUTION_CONTROL_CHOICES = {
    "finalize-terminal": ExecutionControlMode.FINALIZE_TERMINAL,
    "observe-only": ExecutionControlMode.OBSERVE_ONLY,
}


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--single", "--print", dest="prompt", metavar="PROMPT")
    parser.add_argument("--cwd", type=Path, help="working directory")
    parser.add_argument("-m", "--model", help="model identifier")
    parser.add_argument("--provider", help="named provider profile")
    parser.add_argument("--base-url", help="provider API base URL")
    parser.add_argument(
        "--no-failover",
        action="store_true",
        help="disable configured provider fallbacks for this run",
    )
    parser.add_argument(
        "--output-format",
        choices=("plain", "json", "jsonl"),
        default="plain",
    )
    parser.add_argument("--always-approve", "--yolo", action="store_true")
    parser.add_argument(
        "--sandbox",
        choices=tuple(profile.value for profile in SandboxProfile),
        help="operating-system child sandbox (off explicitly provides no OS isolation)",
    )
    parser.add_argument("--allow", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    parser.add_argument(
        "--permissions-file",
        type=Path,
        help="load persistent permission rules from this JSON file",
    )
    parser.add_argument(
        "--execution-profile",
        choices=tuple(profile.value for profile in ExecutionProfile),
        default=ExecutionProfile.NORMAL.value,
        help="ordinary Agent execution budget profile",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="compatibility override that scales the complete ordinary execution budget",
    )
    parser.add_argument(
        "--execution-control",
        choices=tuple(EXECUTION_CONTROL_CHOICES),
        default="finalize-terminal",
        help="supervision behavior after a terminal execution decision",
    )
    parser.add_argument(
        "--effort",
        choices=tuple(effort.value for effort in ReasoningEffort),
        help="agent review depth (default: high, or the saved TUI preference)",
    )
    parser.add_argument("--resume", metavar="SESSION_ID", help="resume an existing session")


def _add_acp_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--transport",
        choices=("stdio", "websocket"),
        default="stdio",
        help="ACP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="WebSocket bind host")
    parser.add_argument("--port", type=int, default=0, help="WebSocket bind port")
    parser.add_argument("--cwd", type=Path, help="connection workspace")
    parser.add_argument("-m", "--model", help="model identifier")
    parser.add_argument("--provider", help="named provider profile")
    parser.add_argument("--base-url", help="provider API base URL")
    parser.add_argument(
        "--no-failover",
        action="store_true",
        help="disable configured provider fallbacks for this process",
    )
    parser.add_argument(
        "--sandbox",
        choices=tuple(profile.value for profile in SandboxProfile),
        help="operating-system child sandbox (off explicitly provides no OS isolation)",
    )
    parser.add_argument("--allow", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    parser.add_argument(
        "--permissions-file",
        type=Path,
        help="load persistent permission rules from this JSON file",
    )
    parser.add_argument(
        "--execution-profile",
        choices=tuple(profile.value for profile in ExecutionProfile),
        default=ExecutionProfile.NORMAL.value,
        help="ordinary Agent execution budget profile",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="compatibility override that scales the complete ordinary execution budget",
    )
    parser.add_argument(
        "--execution-control",
        choices=tuple(EXECUTION_CONTROL_CHOICES),
        default="finalize-terminal",
        help="supervision behavior after a terminal execution decision",
    )
    parser.add_argument(
        "--effort",
        choices=tuple(effort.value for effort in ReasoningEffort),
        help="agent review depth (default: high)",
    )


def _subagent_steps(value: str) -> int:
    try:
        steps = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("subagent max steps must be an integer") from None
    if not 1 <= steps <= MAX_SUBAGENT_STEPS:
        raise argparse.ArgumentTypeError(
            f"subagent max steps must be between 1 and {MAX_SUBAGENT_STEPS}"
        )
    return steps


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser with the established command tree."""
    parser = argparse.ArgumentParser(
        prog="neuro",
        description="Independent Python terminal coding agent",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_run_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")

    version_parser = subparsers.add_parser("version", help="show version information")
    version_parser.add_argument("--json", action="store_true")

    inspect_parser = subparsers.add_parser("inspect", help="show effective redacted configuration")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.add_argument("--cwd", type=Path, help="working directory")

    completions_parser = subparsers.add_parser(
        "completions", help="generate a basic shell completion script"
    )
    completions_parser.add_argument("shell", choices=("bash", "zsh", "fish", "powershell"))

    agent_parser = subparsers.add_parser("agent", help="run the headless agent")
    _add_run_arguments(agent_parser)

    code_parser = subparsers.add_parser("code", help="launch Neuro Code in this directory")
    _add_run_arguments(code_parser)

    acp_parser = subparsers.add_parser("acp", help="serve partial ACP v1 over stdio")
    _add_acp_arguments(acp_parser)

    subagent_parser = subparsers.add_parser(
        "subagent",
        help="run one explicit bounded read-only repository subagent",
    )
    subagent_parser.add_argument("prompt", metavar="PROMPT")
    subagent_parser.add_argument(
        "--parent-session",
        required=True,
        metavar="SESSION_ID",
        help="existing parent session that owns the child task",
    )
    subagent_parser.add_argument("--cwd", type=Path, help="working directory")
    subagent_parser.add_argument("-m", "--model", help="model identifier")
    subagent_parser.add_argument("--provider", help="named provider profile")
    subagent_parser.add_argument("--base-url", help="provider API base URL")
    subagent_parser.add_argument(
        "--no-failover",
        action="store_true",
        help="disable configured provider fallbacks for this run",
    )
    subagent_parser.add_argument(
        "--sandbox",
        choices=tuple(profile.value for profile in SandboxProfile),
        help="operating-system child sandbox (off explicitly provides no OS isolation)",
    )
    subagent_parser.add_argument("--allow", action="append", default=[], metavar="PATTERN")
    subagent_parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    subagent_parser.add_argument(
        "--max-steps",
        type=_subagent_steps,
        default=8,
        help=f"maximum child model steps (1-{MAX_SUBAGENT_STEPS})",
    )
    subagent_parser.add_argument(
        "--execution-control",
        choices=tuple(EXECUTION_CONTROL_CHOICES),
        default="finalize-terminal",
        help="supervision behavior for the child runtime",
    )
    subagent_parser.add_argument(
        "--effort",
        choices=tuple(effort.value for effort in ReasoningEffort),
        help="agent review depth (default: high)",
    )
    subagent_parser.add_argument("--json", action="store_true")

    subagents_parser = subparsers.add_parser(
        "subagents",
        help="run one explicit lifecycle action for a linked child subagent",
    )
    subagents_parser.add_argument(
        "action",
        choices=tuple(action.value for action in SubagentRelationshipAction),
    )
    subagents_parser.add_argument("task_id", metavar="TASK_ID")
    subagents_parser.add_argument(
        "--parent-session",
        required=True,
        metavar="SESSION_ID",
        help="parent session that owns the child task",
    )
    subagents_parser.add_argument("--cwd", type=Path, help="working directory")
    subagents_parser.add_argument("--json", action="store_true")

    sessions_parser = subparsers.add_parser("sessions", help="list or search persisted sessions")
    sessions_parser.add_argument(
        "session_action",
        nargs="?",
        choices=("list", "search", "rename", "compact", "artifacts", "recover"),
        default="list",
        help="session operation (default: list)",
    )
    sessions_parser.add_argument(
        "query",
        nargs="?",
        metavar="QUERY_OR_SESSION_ID",
        help="search query, session ID, or artifact session ID",
    )
    sessions_parser.add_argument(
        "title",
        nargs="?",
        metavar="TITLE_OR_ARTIFACT_ID",
        help="new title or artifact ID",
    )
    sessions_parser.add_argument("--json", action="store_true")
    sessions_parser.add_argument(
        "--action",
        choices=("inspect", "abandon", "retry"),
        default="inspect",
        help="recovery action (only valid for sessions recover)",
    )
    sessions_parser.add_argument(
        "--reason",
        default="explicit_user_resolution",
        help="bounded reason for an explicit recovery abandon",
    )
    sessions_parser.add_argument("--limit", type=int, default=50)
    sessions_parser.add_argument("--offset", type=int, default=0)
    sessions_parser.add_argument("--include-content", action="store_true")
    sessions_parser.add_argument(
        "--max-bytes",
        type=int,
        default=MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
        help="bounded artifact read size (only valid for sessions artifacts)",
    )
    sessions_parser.add_argument(
        "--prune",
        action="store_true",
        help="prune old artifacts not referenced by any persisted session",
    )
    sessions_parser.add_argument("--cwd", type=Path, help="configuration working directory")

    export_parser = subparsers.add_parser("export", help="export a persisted session")
    export_parser.add_argument("session_id")
    export_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    export_parser.add_argument("--output", type=Path)
    export_parser.add_argument("--cwd", type=Path, help="configuration working directory")

    import_parser = subparsers.add_parser(
        "import-session",
        help="import a read-only upstream Rust JSONL session",
    )
    import_parser.add_argument("source", type=Path, help="session directory or summary.json")
    import_parser.add_argument("--json", action="store_true")
    import_parser.add_argument("--cwd", type=Path, help="configuration working directory")

    providers_parser = subparsers.add_parser(
        "providers", help="list or inspect model provider profiles"
    )
    provider_subparsers = providers_parser.add_subparsers(dest="provider_command", required=True)
    provider_list_parser = provider_subparsers.add_parser("list", help="list provider profiles")
    provider_list_parser.add_argument("--json", action="store_true")
    provider_list_parser.add_argument("--cwd", type=Path, help="working directory")
    provider_inspect_parser = provider_subparsers.add_parser(
        "inspect", help="inspect one redacted provider profile"
    )
    provider_inspect_parser.add_argument("profile", nargs="?")
    provider_inspect_parser.add_argument("--json", action="store_true")
    provider_inspect_parser.add_argument("--cwd", type=Path, help="working directory")
    return parser


__all__ = ["EXECUTION_CONTROL_CHOICES", "build_parser"]
