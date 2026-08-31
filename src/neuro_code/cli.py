from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from neuro_code import __version__
from neuro_code.application.execution_policy import ExecutionProfile
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
from neuro_code.application.ports.user_interaction import (
    InteractionUnavailable,
    UserInputRequest,
    UserInputResponse,
)
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions.lifecycle import (
    ImportSessionRequest,
    SessionLifecycleService,
)
from neuro_code.application.sessions.service import (
    ExportSessionRequest,
    ResumeSessionRequest,
    SessionApplicationService,
)
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipAction,
    SubagentRelationshipActionRequest,
)
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools.service import (
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.application.workflows.subagent import (
    MAX_SUBAGENT_STEPS,
    RunSubagentRequest,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.sessions import SessionSnapshot
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult
from neuro_code.interfaces.cli.serialization import (
    render_session_markdown,
    serialize_execution_outcome,
    serialize_subagent_relationship_action,
    serialize_subagent_result,
)
from neuro_code.interfaces.cli.sessions import run_sessions_command as _sessions_command
from neuro_code.shared.errors import ConfigurationError, NeuroCodeError

if TYPE_CHECKING:
    from neuro_code.configuration.app import AppConfig


_EXECUTION_CONTROL_CHOICES = {
    "finalize-terminal": ExecutionControlMode.FINALIZE_TERMINAL,
    "observe-only": ExecutionControlMode.OBSERVE_ONLY,
}


class CliUserInteraction:
    """Interactive stdin adapter used only when stdin is a terminal."""

    def __init__(self, *, interactive: bool = True) -> None:
        self._interactive = interactive

    async def request(self, request: UserInputRequest) -> UserInputResponse:
        if not self._interactive or not sys.stdin.isatty():
            raise InteractionUnavailable(
                "interactive input is unavailable on non-interactive stdin"
            )
        print(f"\n{request.question}")
        for index, option in enumerate(request.options, start=1):
            detail = f" — {option.description}" if option.description else ""
            print(f"{index}. {option.label}{detail}")
        prompt = "Answer: "
        answer = await asyncio.to_thread(input, prompt)
        if not answer.strip():
            raise InteractionUnavailable("an answer is required")
        if request.options and answer.strip().isdigit():
            selected_index = int(answer.strip())
            if 1 <= selected_index <= len(request.options):
                return UserInputResponse(request.request_id, str(selected_index))
        if not request.allow_free_text:
            raise InteractionUnavailable("free-text input is unavailable for this request")
        return UserInputResponse(request.request_id, text=answer.strip())


class ImportedRustSession(Protocol):
    """CLI-facing view of an imported historical session.

    表示 CLI 使用的已导入历史会话视图."""

    @property
    def snapshot(self) -> SessionSnapshot: ...

    @property
    def total_records(self) -> int: ...

    @property
    def invalid_records(self) -> int: ...

    @property
    def unsupported_records(self) -> int: ...

    @property
    def preserved_context_records(self) -> int: ...

    @property
    def recovered_context_records(self) -> int: ...

    @property
    def deduplicated_context_records(self) -> int: ...

    @property
    def invalid_embedded_records(self) -> int: ...

    @property
    def unsupported_embedded_records(self) -> int: ...

    @property
    def imported_messages(self) -> int: ...

    def to_dict(self) -> dict[str, object]: ...


class CliServices(Protocol):
    """Capabilities selected by bootstrap for CLI command handling.

    表示 bootstrap 为 CLI 命令处理选择的能力集合."""

    async def open_application(self, settings: ApplicationSettings) -> Any: ...

    def load_config(self, cwd: Path | None) -> AppConfig: ...

    def discover_instructions(self, cwd: Path) -> InstructionDiscoveryResult: ...

    def discover_skills(self, cwd: Path) -> SkillDiscoveryResult: ...

    async def create_session_store(self, config: AppConfig) -> SessionStore: ...

    def create_tool_output_artifact_service(
        self,
        config: AppConfig,
        store: SessionStore,
    ) -> SessionToolOutputArtifactApplicationService: ...

    async def load_rust_session(self, source: Path) -> ImportedRustSession: ...

    async def run_acp(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int: ...

    async def run_tui(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int: ...


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
        choices=tuple(_EXECUTION_CONTROL_CHOICES),
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
        choices=tuple(_EXECUTION_CONTROL_CHOICES),
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
        choices=tuple(_EXECUTION_CONTROL_CHOICES),
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


def _version_payload() -> dict[str, str]:
    return {
        "name": "neuro-code",
        "version": __version__,
    }


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


def _plain_config(config: AppConfig) -> str:
    payload = config.redacted_dict()
    provider = payload["provider"]
    loaded_files = payload["loaded_files"]
    assert isinstance(loaded_files, list)
    routing = payload["routing"]
    assert isinstance(routing, dict)
    fallbacks = routing["fallbacks"]
    assert isinstance(fallbacks, list)
    sandbox = payload["sandbox"]
    assert isinstance(sandbox, dict)
    lines = [
        f"cwd: {payload['cwd']}",
        f"state_dir: {payload['state_dir']}",
        f"default_provider: {routing['default'] or '(none)'}",
        f"selected_provider: {routing['selected'] or '(none)'}",
        f"fallback_providers: {', '.join(fallbacks) if fallbacks else '(none)'}",
        f"sandbox_profile: {sandbox['profile']}",
        f"sandbox_source: {sandbox['source']}",
    ]
    if isinstance(provider, dict):
        lines.extend(
            (
                f"protocol: {provider['protocol']}",
                f"dialect: {provider['dialect']}",
                f"model: {provider['model']}",
                f"base_url: {provider['base_url']}",
                f"credential_env: {provider['api_key_env'] or '(none)'}",
                f"credential_configured: {str(provider['credential_configured']).lower()}",
                f"proxy_mode: {provider['proxy_mode']}",
                f"proxy_env: {provider['proxy_url_env'] or '(none)'}",
                f"proxy_url_configured: {str(provider['proxy_url_configured']).lower()}",
                f"context_window_tokens: {provider['context_window_tokens'] or '(unknown)'}",
                f"max_output_tokens: {provider['max_output_tokens']}",
            )
        )
    else:
        lines.append("provider: (not configured)")
    lines.extend(("loaded_files:",))
    lines.extend(f"  - {path}" for path in loaded_files)
    if not loaded_files:
        lines.append("  - (none)")
    return "\n".join(lines)


def _instruction_lines(cwd: Path, services: CliServices) -> list[str]:
    """Discover instruction files and format them for inspect output.

    发现指令文件并将其格式化为 inspect 输出."""
    result = services.discover_instructions(cwd)
    lines: list[str] = ["instruction_files:"]
    if result.files:
        for instruction_file in result.files:
            byte_count = len(instruction_file.content.encode("utf-8"))
            lines.append(
                f"  - {instruction_file.relative_path} "
                f"(depth={instruction_file.depth}, bytes={byte_count})"
            )
    else:
        lines.append("  - (none)")
    if result.rejections:
        lines.append("instruction_rejections:")
        for rejection in result.rejections:
            lines.append(f"  - {rejection.relative_path}: {rejection.reason.value}")
    lines.append(f"instruction_fingerprint: {result.fingerprint[:16]}...")
    return lines


def _skill_lines(cwd: Path, services: CliServices) -> list[str]:
    """Discover skill files and format them for inspect output.

    发现技能文件并将其格式化为 inspect 输出."""
    result = services.discover_skills(cwd)
    lines: list[str] = ["skill_files:"]
    if result.files:
        for skill in result.files:
            desc = f": {skill.description}" if skill.description else ""
            lines.append(
                f"  - [{skill.scope.name.lower()}] {skill.name} "
                f"({skill.relative_path}, depth={skill.depth}){desc}"
            )
    else:
        lines.append("  - (none)")
    if result.rejections:
        lines.append("skill_rejections:")
        for rejection in result.rejections:
            scope = rejection.scope.name.lower() if rejection.scope is not None else "unknown"
            lines.append(f"  - [{scope}] {rejection.relative_path}: {rejection.reason.value}")
    lines.append(f"skill_fingerprint: {result.fingerprint[:16]}...")
    return lines


def _completion_script(shell: str) -> str:
    commands = "code version inspect completions agent acp subagent subagents providers sessions export import-session"
    if shell == "bash":
        return (
            "_neuro_code() { COMPREPLY=( $(compgen -W '"
            + commands
            + '\' -- "${COMP_WORDS[1]}") ); }; complete -F _neuro_code neuro neuro-code'
        )
    if shell == "zsh":
        return f"#compdef neuro neuro-code\n_arguments '1:command:({commands})'"
    if shell == "fish":
        return "\n".join(
            f"complete -c {executable} -f -a {command}"
            for executable in ("neuro", "neuro-code")
            for command in commands.split()
        )
    return (
        "Register-ArgumentCompleter -CommandName neuro,neuro-code -ScriptBlock { "
        f"'{commands}'.Split(' ') | Where-Object {{ $_ -like \"$wordToComplete*\" }} }}"
    )


async def _run_agent(args: argparse.Namespace, services: CliServices) -> int:
    if not args.prompt:
        raise ConfigurationError(
            "the agent subcommand requires -p/--single; run neuro without a subcommand "
            "for the interactive TUI"
        )
    application = await services.open_application(_application_settings(args))
    try:
        if args.resume is not None:
            await application.session_service.prepare_resume(ResumeSessionRequest(args.resume))
        binding = await application.create_binding(
            resume_id=args.resume,
            user_interaction=CliUserInteraction(interactive=args.output_format == "plain"),
        )

        async def stream_event(event: AgentEvent) -> None:
            if args.output_format == "plain" and event.kind is AgentEventKind.TEXT_DELTA:
                text = event.data.get("text")
                if isinstance(text, str):
                    print(text, end="", flush=True)
            elif args.output_format == "jsonl":
                if event.kind is not AgentEventKind.MODEL_REQUEST_SNAPSHOT:
                    print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)

        async def ultracode_delegate(
            request: RunTurnRequest,
            sink: EventSink | None,
        ) -> AgentRunResult:
            service = await application.create_ultracode_delegation_service(
                parent_binding=binding,
            )
            return cast(AgentRunResult, await service.run_turn(request, sink=sink))

        if getattr(binding.runner, "reasoning_effort", None) is ReasoningEffort.ULTRACODE:
            turn_service = application.session_service.bind_runner(
                binding.runner,
                ultracode_delegate=ultracode_delegate,
            )
        else:
            turn_service = application.session_service.bind_runner(binding.runner)
        result = await turn_service.run_turn(
            RunTurnRequest(
                args.prompt,
                expected_session_id=args.resume,
            ),
            sink=stream_event,
        )
        if args.output_format == "plain":
            print()
        elif args.output_format == "json":
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.response,
                        "steps": result.steps,
                        "events": [
                            event.to_dict()
                            for event in result.events
                            if event.kind is not AgentEventKind.MODEL_REQUEST_SNAPSHOT
                        ],
                        "outcome": serialize_execution_outcome(result.outcome),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        await asyncio.shield(application.close())


async def _run_acp(args: argparse.Namespace, services: CliServices) -> int:
    return await services.run_acp(args, _application_settings(args))


async def _run_subagent(args: argparse.Namespace, services: CliServices) -> int:
    """Run one explicit read-only child and print its safe projection.

    运行一次明确的只读子代理并输出安全投影.
    """

    application = await services.open_application(_application_settings(args))
    parent_binding = None
    try:
        parent_config = await application.config_for_session_resume(args.parent_session)
        parent_binding = await application.create_binding(
            config=parent_config,
            resume_id=args.parent_session,
        )
        parent_capabilities = parent_binding.capabilities
        if parent_capabilities is None:
            raise ConfigurationError("parent binding capability metadata is missing")
        try:
            request = RunSubagentRequest(
                args.parent_session,
                args.prompt,
                max_steps=args.max_steps,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from None
        service = application.create_read_only_subagent_application_service()
        projection = await service.run_subagent(
            request,
            parent_capabilities=parent_capabilities,
        )
        if args.json:
            print(json.dumps(serialize_subagent_result(projection), ensure_ascii=False))
        else:
            print(projection.response)
        return 0
    finally:
        if parent_binding is not None and parent_binding.background_tasks is not None:
            await asyncio.shield(parent_binding.background_tasks.shutdown())
        await asyncio.shield(application.close())


async def _run_subagent_lifecycle(args: argparse.Namespace, services: CliServices) -> int:
    """Run one explicit lifecycle action for a linked child session.

    对关联子会话执行一次明确的生命周期动作.

    The parent is checked through the composition workspace/resume boundary;
    the lifecycle service then owns relationship and terminal-task validation.
    No model turn is started by this command.
    先通过组合根工作区/恢复边界校验父会话,再由生命周期服务负责关系和终态任务校验.
    本命令不会启动模型回合.
    """

    application = await services.open_application(
        ApplicationSettings(
            cwd=args.cwd,
            resume_id=args.parent_session,
        )
    )
    try:
        await application.config_for_session_resume(args.parent_session)
        action = SubagentRelationshipAction(args.action)
        result = await application.create_subagent_relationship_lifecycle_service().execute(
            SubagentRelationshipActionRequest(
                parent_session_id=args.parent_session,
                parent_task_id=args.task_id,
                action=action,
            )
        )
        payload = serialize_subagent_relationship_action(result)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif action is SubagentRelationshipAction.RESUME:
            print(f"Child session {result.child_session_id} is ready to resume.")
        elif action is SubagentRelationshipAction.FORK:
            assert result.forked_session_id is not None
            print(
                f"Forked child session {result.forked_session_id}; it was not opened automatically."
            )
        else:
            print(f"Deleted child session {result.child_session_id}.")
        return 0
    finally:
        await asyncio.shield(application.close())


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
        return _EXECUTION_CONTROL_CHOICES[value]
    except KeyError:
        raise ConfigurationError("execution control selection is invalid") from None


def _provider_rows(config: AppConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, profile in config.providers.items():
        row = profile.redacted_dict()
        row["default"] = name == config.default_provider
        row["selected"] = name == config.selected_provider
        row["fallback"] = name in config.fallback_providers
        rows.append(row)
    return rows


def _providers_command(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    if args.provider_command == "list":
        rows = _provider_rows(config)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("No provider profiles configured.")
        else:
            for row in rows:
                markers = "".join(
                    (
                        "*" if row["default"] else " ",
                        ">" if row["selected"] else " ",
                        "+" if row["fallback"] else " ",
                    )
                )
                status = "available" if row["available"] else "unavailable"
                print(f"{markers} {row['name']}\t{row['protocol']}\t{row['model']}\t{status}")
        return 0

    profile_name = args.profile or config.selected_provider
    if profile_name is None:
        raise ConfigurationError("no provider profile was selected for inspection")
    try:
        profile = config.providers[profile_name]
    except KeyError as error:
        raise ConfigurationError(f"provider profile does not exist: {profile_name}") from error
    payload = profile.redacted_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            if isinstance(value, list):
                rendered = ", ".join(value) if value else "(none)"
            elif value is None:
                rendered = "(none)"
            elif isinstance(value, bool):
                rendered = str(value).lower()
            else:
                rendered = str(value)
            print(f"{key}: {rendered}")
    return 0


async def _export_session(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    store = await services.create_session_store(config)
    exported = await SessionApplicationService(store).export_session(
        ExportSessionRequest(args.session_id, include_events=args.format == "json")
    )
    summary = exported.snapshot.summary
    items = exported.snapshot.items
    messages = exported.snapshot.messages
    if args.format == "json":
        content = (
            json.dumps(
                {
                    "schema_version": 4,
                    "session": summary.to_dict(),
                    "messages": [message.to_dict() for message in messages],
                    "conversation_items": [item.to_dict() for item in items],
                    "events": [dict(event) for event in exported.events],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        content = render_session_markdown(items)
    if args.output is None:
        print(content, end="")
    else:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(output)
    return 0


async def _import_session(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    imported = await services.load_rust_session(args.source)
    store = await services.create_session_store(config)
    await SessionLifecycleService(store).import_session(ImportSessionRequest(imported.snapshot))
    if args.json:
        print(json.dumps(imported.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"Imported upstream Rust session {imported.snapshot.summary.id}: "
            f"{imported.imported_messages}/{imported.total_records} messages, "
            f"{imported.preserved_context_records} context records preserved "
            f"({imported.recovered_context_records} recovered, "
            f"{imported.deduplicated_context_records} duplicates skipped), "
            f"{imported.invalid_records} invalid and "
            f"{imported.unsupported_records} unsupported records skipped; "
            f"{imported.invalid_embedded_records} invalid and "
            f"{imported.unsupported_embedded_records} unsupported embedded items ignored."
        )
    return 0


def run(argv: Sequence[str] | None, *, services: CliServices) -> int:
    """Parse arguments and render CLI responses with bootstrap-provided services.

    解析参数,并使用 bootstrap 提供的服务渲染 CLI 响应."""
    parser = build_parser()
    args = parser.parse_args(tuple(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "version":
            payload = _version_payload()
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"{payload['name']} {payload['version']}")
            return 0
        if args.command == "inspect":
            config = services.load_config(args.cwd)
            if args.json:
                inspect_payload: dict[str, object] = config.redacted_dict()
                result = services.discover_instructions(config.cwd)
                inspect_payload["instructions"] = {
                    "files": [
                        {
                            "path": f.relative_path,
                            "depth": f.depth,
                            "bytes": len(f.content.encode("utf-8")),
                        }
                        for f in result.files
                    ],
                    "rejections": [
                        {"path": r.relative_path, "reason": r.reason.value}
                        for r in result.rejections
                    ],
                    "fingerprint": result.fingerprint,
                }
                skill_result = services.discover_skills(config.cwd)
                inspect_payload["skills"] = {
                    "files": [
                        {
                            "name": s.name,
                            "path": s.relative_path,
                            "description": s.description,
                            "when_to_use": s.when_to_use,
                            "scope": s.scope.name.lower(),
                            "depth": s.depth,
                        }
                        for s in skill_result.files
                    ],
                    "rejections": [
                        {
                            "path": r.relative_path,
                            "reason": r.reason.value,
                            "scope": r.scope.name.lower() if r.scope is not None else None,
                        }
                        for r in skill_result.rejections
                    ],
                    "fingerprint": skill_result.fingerprint,
                }
                print(json.dumps(inspect_payload, ensure_ascii=False, indent=2))
            else:
                print(_plain_config(config))
                print()
                print("\n".join(_instruction_lines(config.cwd, services)))
                print()
                print("\n".join(_skill_lines(config.cwd, services)))
            return 0
        if args.command == "completions":
            print(_completion_script(args.shell))
            return 0
        if args.command == "providers":
            return _providers_command(args, services)
        if args.command == "sessions":
            return asyncio.run(_sessions_command(args, services))
        if args.command == "export":
            return asyncio.run(_export_session(args, services))
        if args.command == "import-session":
            return asyncio.run(_import_session(args, services))
        if args.command == "acp":
            return asyncio.run(_run_acp(args, services))
        if args.command == "subagent":
            return asyncio.run(_run_subagent(args, services))
        if args.command == "subagents":
            return asyncio.run(_run_subagent_lifecycle(args, services))
        if args.command in {None, "code"} and args.prompt is None:
            return asyncio.run(services.run_tui(args, _application_settings(args)))
        return asyncio.run(_run_agent(args, services))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except NeuroCodeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
