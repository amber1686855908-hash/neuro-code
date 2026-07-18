from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from neuro_code import __version__
from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.rust_session import load_rust_session
from neuro_code.adapters.sandbox import create_shell_sandbox, enforce_configured_sandbox
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.adapters.ui_preferences import JsonUiPreferencesStore
from neuro_code.async_utils import run_blocking
from neuro_code.config import (
    AppConfig,
    load_config,
    override_provider,
    override_sandbox,
    pin_resumed_sandbox,
)
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_search import SessionSearchHit
from neuro_code.domain.sessions import SessionSummary
from neuro_code.errors import ConfigurationError, NeuroCodeError
from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.ports.approval import PermissionApprover
from neuro_code.ports.background_tasks import BackgroundTaskManager
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.tools import ToolContext
from neuro_code.providers import create_routed_provider
from neuro_code.runtime import (
    AgentConversation,
    AgentRuntime,
    ConversationBinding,
    ProfileConversationController,
    ProviderOption,
    SessionApprovalBroker,
)
from neuro_code.tools import default_tool_registry
from neuro_code.workspace import workspaces_match


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
        help="operating-system sandbox profile for this run",
    )
    parser.add_argument("--allow", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--resume", metavar="SESSION_ID", help="resume an existing session")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuro-code",
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

    sessions_parser = subparsers.add_parser("sessions", help="list or search persisted sessions")
    sessions_parser.add_argument(
        "session_action",
        nargs="?",
        choices=("list", "search", "rename"),
        default="list",
        help="session operation (default: list)",
    )
    sessions_parser.add_argument(
        "query",
        nargs="?",
        metavar="QUERY_OR_SESSION_ID",
        help="search query or session ID to rename",
    )
    sessions_parser.add_argument(
        "title",
        nargs="?",
        metavar="TITLE",
        help="new title for the rename operation",
    )
    sessions_parser.add_argument("--json", action="store_true")
    sessions_parser.add_argument("--limit", type=int, default=50)
    sessions_parser.add_argument("--offset", type=int, default=0)
    sessions_parser.add_argument("--include-content", action="store_true")
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


def _completion_script(shell: str) -> str:
    commands = "version inspect completions agent providers sessions export import-session"
    if shell == "bash":
        return (
            "_neuro_code() { COMPREPLY=( $(compgen -W '"
            + commands
            + '\' -- "${COMP_WORDS[1]}") ); }; complete -F _neuro_code neuro-code'
        )
    if shell == "zsh":
        return f"#compdef neuro-code\n_arguments '1:command:({commands})'"
    if shell == "fish":
        return "\n".join(f"complete -c neuro-code -f -a {command}" for command in commands.split())
    return (
        "Register-ArgumentCompleter -CommandName neuro-code -ScriptBlock { "
        f"'{commands}'.Split(' ') | Where-Object {{ $_ -like \"$wordToComplete*\" }} }}"
    )


async def _run_agent(args: argparse.Namespace) -> int:
    if not args.prompt:
        raise ConfigurationError(
            "the agent subcommand requires -p/--single; run neuro-code without a subcommand "
            "for the interactive TUI"
        )
    background_tasks = LocalBackgroundTaskManager()
    try:
        _, _, conversation = await _prepare_conversation(
            args,
            background_tasks=background_tasks,
        )

        async def stream_event(event: AgentEvent) -> None:
            if args.output_format == "plain" and event.kind is AgentEventKind.TEXT_DELTA:
                text = event.data.get("text")
                if isinstance(text, str):
                    print(text, end="", flush=True)
            elif args.output_format == "jsonl":
                print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)

        result = await conversation.run(args.prompt, sink=stream_event)
        if args.output_format == "plain":
            print()
        elif args.output_format == "json":
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.response,
                        "steps": result.steps,
                        "events": [event.to_dict() for event in result.events],
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        await asyncio.shield(background_tasks.shutdown())


async def _prepare_conversation(
    args: argparse.Namespace,
    *,
    background_tasks: BackgroundTaskManager,
    approver: PermissionApprover | None = None,
) -> tuple[AppConfig, ModelProvider, AgentConversation]:
    config = _effective_config(args)
    store = SqliteSessionStore(config.state_dir / "sessions.db")
    config = await _pin_resume_sandbox(config, args.resume, store)
    _enforce_process_sandbox(config, args)
    await store.initialize()
    provider, conversation = await _compose_conversation(
        args,
        config,
        store=store,
        background_tasks=background_tasks,
        approver=approver,
        resume_id=args.resume,
    )
    return config, provider, conversation


async def _pin_resume_sandbox(
    config: AppConfig,
    resume_id: str | None,
    store: SqliteSessionStore,
) -> AppConfig:
    if resume_id is None:
        return config
    saved_profile = await store.peek_session_sandbox_profile(resume_id)
    return pin_resumed_sandbox(config, saved_profile)


def _effective_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.cwd)
    config = override_sandbox(config, args.sandbox)
    return override_provider(
        config,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
    )


def _enforce_process_sandbox(config: AppConfig, args: argparse.Namespace) -> None:
    raw_arguments = tuple(getattr(args, "launch_arguments", ()))
    command = (sys.executable, "-m", "neuro_code", *raw_arguments)
    enforce_configured_sandbox(
        config.sandbox_profile,
        config.cwd,
        config.state_dir,
        command,
    )


async def _compose_conversation(
    args: argparse.Namespace,
    config: AppConfig,
    *,
    store: SqliteSessionStore,
    background_tasks: BackgroundTaskManager,
    approver: PermissionApprover | None,
    resume_id: str | None,
) -> tuple[ModelProvider, AgentConversation]:
    provider = create_routed_provider(config, failover=not args.no_failover)
    shell_sandbox = create_shell_sandbox(
        config.sandbox_profile,
        config.cwd,
        config.state_dir,
    )
    mode = PermissionMode.BYPASS if args.always_approve else PermissionMode.DEFAULT
    permissions = PermissionManager(
        mode=mode,
        rules=_rules(args),
        interactive=approver is not None,
    )
    runtime = AgentRuntime(
        provider=provider,
        tools=default_tool_registry(
            config.sandbox_profile,
            enable_background_tasks=True,
        ),
        permissions=permissions,
        tool_context=ToolContext(
            config.cwd,
            sandbox_profile=config.sandbox_profile,
            shell_sandbox=shell_sandbox,
            protected_environment_variables=config.protected_environment_variables,
            background_tasks=background_tasks,
        ),
        approver=approver,
        session_store=store,
        max_steps=args.max_steps,
    )
    conversation = await AgentConversation.open(
        runtime=runtime,
        store=store,
        cwd=config.cwd,
        resume_id=resume_id,
    )
    return provider, conversation


def _provider_options(config: AppConfig) -> tuple[ProviderOption, ...]:
    options: list[ProviderOption] = []
    for name, profile in config.providers.items():
        redacted = profile.redacted_dict()
        credential_configured = redacted.get("credential_configured") is True
        options.append(
            ProviderOption(
                name=name,
                protocol=profile.protocol,
                model=profile.model,
                available=profile.available,
                credential_configured=credential_configured,
                default=name == config.default_provider,
            )
        )
    return tuple(options)


async def _run_tui(args: argparse.Namespace) -> int:
    try:
        from neuro_code.tui import NeuroCodeApp
    except ModuleNotFoundError as error:
        if error.name in {"rich", "textual"}:
            raise ConfigurationError(
                "interactive TUI dependencies are missing; install 'neuro-code[tui]'"
            ) from error
        raise

    background_tasks = LocalBackgroundTaskManager()
    try:
        approvals = SessionApprovalBroker()
        config = _effective_config(args)
        store = SqliteSessionStore(config.state_dir / "sessions.db")
        config = await _pin_resume_sandbox(config, args.resume, store)
        _enforce_process_sandbox(config, args)
        ui_preferences = JsonUiPreferencesStore(config.state_dir / "ui-preferences.json")
        language = await ui_preferences.load_language()
        await store.initialize()

        async def compose_scoped(
            selected_config: AppConfig,
            resume_id: str | None,
        ) -> tuple[ModelProvider, AgentConversation, BackgroundTaskManager]:
            task_scope = background_tasks.open_scope()
            try:
                selected_provider, selected_conversation = await _compose_conversation(
                    args,
                    selected_config,
                    store=store,
                    background_tasks=task_scope,
                    approver=approvals,
                    resume_id=resume_id,
                )
            except BaseException:
                await asyncio.shield(task_scope.shutdown())
                raise
            return selected_provider, selected_conversation, task_scope

        provider, conversation, task_scope = await compose_scoped(config, args.resume)
        selected_profile = config.selected_provider
        if selected_profile is None:
            raise ConfigurationError("no provider profile is selected")

        async def bind_profile(profile_name: str) -> ConversationBinding:
            selected_config = override_provider(config, provider=profile_name)
            selected_provider, selected_conversation, selected_tasks = await compose_scoped(
                selected_config,
                None,
            )
            return ConversationBinding(
                selected_conversation,
                selected_provider,
                selected_tasks,
            )

        async def list_workspace_sessions() -> tuple[SessionSummary, ...]:
            sessions = await store.list_sessions(limit=1000)
            return tuple(
                session for session in sessions if workspaces_match(session.cwd, config.cwd)
            )[:50]

        async def search_workspace_sessions(query: str) -> tuple[SessionSearchHit, ...]:
            page = await store.search_sessions(
                query,
                limit=1000,
                include_content=True,
            )
            return tuple(
                hit for hit in page.results if workspaces_match(hit.summary.cwd, config.cwd)
            )[:50]

        async def bind_session(profile_name: str, session_id: str) -> ConversationBinding:
            selected_config = override_provider(config, provider=profile_name)
            selected_provider, selected_conversation, selected_tasks = await compose_scoped(
                selected_config,
                session_id,
            )
            return ConversationBinding(
                selected_conversation,
                selected_provider,
                selected_tasks,
            )

        async def rename_workspace_session(
            session_id: str,
            title: str,
        ) -> SessionSummary:
            summary = await store.get_session(session_id)
            if not workspaces_match(summary.cwd, config.cwd):
                raise ConfigurationError(
                    f"session does not exist in the current workspace: {session_id}"
                )
            return await store.update_session_title(session_id, title)

        controller = ProfileConversationController(
            options=_provider_options(config),
            selected_profile=selected_profile,
            binding=ConversationBinding(conversation, provider, task_scope),
            binding_factory=bind_profile,
            session_catalog=list_workspace_sessions,
            session_search=search_workspace_sessions,
            session_binding_factory=bind_session,
            session_rename=rename_workspace_session,
            sandbox_profile=config.sandbox_profile,
        )
        app = NeuroCodeApp(
            controller,
            approval_controller=approvals,
            provider_controller=controller,
            session_controller=controller,
            task_controller=controller,
            ui_preferences=ui_preferences,
            language=language,
            initial_items=controller.items,
            provider_name=controller.provider_name,
            model_name=controller.model_name,
            cwd=config.cwd,
        )
        await app.run_async()
        return 0
    finally:
        await asyncio.shield(background_tasks.shutdown())


def _provider_rows(config: AppConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, profile in config.providers.items():
        row = profile.redacted_dict()
        row["default"] = name == config.default_provider
        row["selected"] = name == config.selected_provider
        row["fallback"] = name in config.fallback_providers
        rows.append(row)
    return rows


def _providers_command(args: argparse.Namespace) -> int:
    config = load_config(args.cwd)
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


async def _sessions_command(args: argparse.Namespace) -> int:
    config = load_config(args.cwd)
    store = SqliteSessionStore(config.state_dir / "sessions.db")
    await store.initialize()
    if args.session_action == "search":
        if args.title is not None:
            raise ConfigurationError("sessions search accepts exactly one query")
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions search requires a non-empty query")
        page = await store.search_sessions(
            args.query,
            limit=args.limit,
            offset=args.offset,
            include_content=args.include_content,
        )
        if args.json:
            print(json.dumps(page.to_dict(), ensure_ascii=False))
        elif not page.results:
            print("No matching sessions found.")
        else:
            for hit in page.results:
                session = hit.summary
                title = session.title or "New session"
                fields = ",".join(hit.matched_fields)
                print(
                    f"{session.id}\t{session.updated_at.isoformat()}\t"
                    f"{session.provider}/{session.model}\t{title}\tmatch={fields}"
                )
                if hit.snippet is not None:
                    print(f"  {hit.snippet}")
        return 0
    if args.session_action == "rename":
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions rename requires a session ID")
        if args.title is None or not args.title.strip():
            raise ConfigurationError("sessions rename requires a non-empty title")
        if args.limit != 50 or args.offset != 0 or args.include_content:
            raise ConfigurationError(
                "--limit, --offset and --include-content are not valid for sessions rename"
            )
        summary = await store.update_session_title(args.query, args.title)
        if args.json:
            print(json.dumps(summary.to_dict(), ensure_ascii=False))
        else:
            print(f"Renamed session {summary.id} to {summary.title!r}.")
        return 0
    if args.query is not None:
        raise ConfigurationError("sessions list does not accept a query")
    if args.title is not None:
        raise ConfigurationError("sessions list does not accept a title")
    if args.offset != 0 or args.include_content:
        raise ConfigurationError(
            "--offset and --include-content are only valid for sessions search"
        )
    sessions = await store.list_sessions(limit=args.limit)
    if args.json:
        print(json.dumps([session.to_dict() for session in sessions], ensure_ascii=False))
    elif not sessions:
        print("No sessions found.")
    else:
        for session in sessions:
            print(
                f"{session.id}\t{session.updated_at.isoformat()}\t"
                f"{session.provider}/{session.model}\t"
                f"sandbox={session.sandbox_profile.value if session.sandbox_profile else 'legacy'}"
                f"\t{session.title or 'New session'}\t{session.cwd}"
            )
    return 0


def _reasoning_markdown(item: PreservedContextItem) -> str:
    payload = item.to_dict()
    text_parts: list[str] = []
    for field in ("content", "summary"):
        blocks = payload.get(field)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
    if text_parts:
        return "\n\n".join(text_parts)
    if payload.get("encrypted_content") is not None:
        return "_(encrypted reasoning preserved in JSON export)_"
    return "_(reasoning metadata preserved in JSON export)_"


def _backend_tool_markdown(item: PreservedContextItem) -> str:
    payload = item.to_dict()
    kind = payload.get("kind")
    if not isinstance(kind, dict):
        return "_(backend tool metadata preserved in JSON export)_"
    tool_type = kind.get("tool_type", "unknown")
    identifier = kind.get("id")
    lines = [f"Type: `{tool_type}`"]
    if isinstance(identifier, str) and identifier:
        lines.append(f"ID: `{identifier}`")
    action = kind.get("action")
    if isinstance(action, dict):
        action_type = action.get("type")
        if isinstance(action_type, str):
            lines.append(f"Action: `{action_type}`")
        for field in ("query", "url", "pattern"):
            value = action.get(field)
            if isinstance(value, str) and value:
                lines.append(f"{field.replace('_', ' ').title()}: {value}")
    return "\n\n".join(lines)


def _session_markdown(items: Sequence[SessionItem]) -> str:
    sections = ["# Neuro Code session export", ""]
    for item in items:
        if isinstance(item, PreservedContextItem):
            if item.kind is ContextItemKind.REASONING:
                sections.extend(("## Reasoning", "", _reasoning_markdown(item), ""))
            else:
                sections.extend(("## Backend tool call", "", _backend_tool_markdown(item), ""))
            continue
        message = item
        if message.role is Role.SYSTEM:
            continue
        title = {
            Role.USER: "User",
            Role.ASSISTANT: "Assistant",
            Role.TOOL: f"Tool: {message.name or 'unknown'}",
        }[message.role]
        sections.extend((f"## {title}", "", message.model_content() or "_(no text)_", ""))
        for call in message.tool_calls:
            sections.extend(
                (
                    f"### Tool call: `{call.name}`",
                    "",
                    "```json",
                    json.dumps(dict(call.arguments), ensure_ascii=False, indent=2),
                    "```",
                    "",
                )
            )
    return "\n".join(sections).rstrip() + "\n"


async def _export_session(args: argparse.Namespace) -> int:
    config = load_config(args.cwd)
    store = SqliteSessionStore(config.state_dir / "sessions.db")
    await store.initialize()
    summary = await store.get_session(args.session_id)
    items = await store.load_session_items(args.session_id)
    messages = [item for item in items if isinstance(item, Message)]
    if args.format == "json":
        events = await store.load_events(args.session_id)
        content = (
            json.dumps(
                {
                    "schema_version": 4,
                    "session": summary.to_dict(),
                    "messages": [message.to_dict() for message in messages],
                    "conversation_items": [item.to_dict() for item in items],
                    "events": events,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        content = _session_markdown(items)
    if args.output is None:
        print(content, end="")
    else:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(output)
    return 0


async def _import_session(args: argparse.Namespace) -> int:
    config = load_config(args.cwd)
    imported = await run_blocking(load_rust_session, args.source)
    store = SqliteSessionStore(config.state_dir / "sessions.db")
    await store.initialize()
    await store.import_session(imported.snapshot)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    launch_arguments = tuple(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(launch_arguments)
    args.launch_arguments = launch_arguments
    try:
        if args.command == "version":
            payload = _version_payload()
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"{payload['name']} {payload['version']}")
            return 0
        if args.command == "inspect":
            config = load_config(args.cwd)
            if args.json:
                print(json.dumps(config.redacted_dict(), ensure_ascii=False, indent=2))
            else:
                print(_plain_config(config))
            return 0
        if args.command == "completions":
            print(_completion_script(args.shell))
            return 0
        if args.command == "providers":
            return _providers_command(args)
        if args.command == "sessions":
            return asyncio.run(_sessions_command(args))
        if args.command == "export":
            return asyncio.run(_export_session(args))
        if args.command == "import-session":
            return asyncio.run(_import_session(args))
        if args.command is None and args.prompt is None:
            return asyncio.run(_run_tui(args))
        return asyncio.run(_run_agent(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except NeuroCodeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
