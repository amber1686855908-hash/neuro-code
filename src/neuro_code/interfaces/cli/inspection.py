"""Read-only configuration, provider, and command metadata commands.

只读 configuration、provider 与命令元数据命令.

This owner contains presentation for commands that do not start an Agent turn
or mutate a session.  Discovery is requested through the bootstrap-selected
CLI contract; concrete configuration loading remains outside the interface.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code import __version__
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.ports.configuration import AppConfig


def _version_payload() -> dict[str, str]:
    return {
        "name": "neuro-code",
        "version": __version__,
    }


def run_version_command(args: argparse.Namespace) -> int:
    payload = _version_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['name']} {payload['version']}")
    return 0


def _plain_config(config: AppConfig) -> str:
    payload = config.redacted_dict(os.environ)
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

    发现指令文件并将其格式化为 inspect 输出.
    """
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

    发现技能文件并将其格式化为 inspect 输出.
    """
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


def run_inspect_command(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    if args.json:
        inspect_payload: dict[str, object] = config.redacted_dict(os.environ)
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
                {"path": r.relative_path, "reason": r.reason.value} for r in result.rejections
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


def run_completions_command(args: argparse.Namespace) -> int:
    print(_completion_script(args.shell))
    return 0


def _provider_rows(config: AppConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, profile in config.providers.items():
        row = profile.redacted_dict(os.environ)
        row["default"] = name == config.default_provider
        row["selected"] = name == config.selected_provider
        row["fallback"] = name in config.fallback_providers
        rows.append(row)
    return rows


def run_providers_command(args: argparse.Namespace, services: CliServices) -> int:
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
    payload = profile.redacted_dict(os.environ)
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


__all__ = [
    "run_completions_command",
    "run_inspect_command",
    "run_providers_command",
    "run_version_command",
]
