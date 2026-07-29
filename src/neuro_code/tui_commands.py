from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCompletion:
    value: str
    display: str


@dataclass(frozen=True, slots=True)
class _Command:
    name: str
    parameter: str | None = None
    choices: tuple[str, ...] = ()

    @property
    def base(self) -> str:
        return f"/{self.name}"

    @property
    def syntax(self) -> str:
        if self.parameter is None:
            return self.base
        return f"{self.base} {self.parameter}"


_COMMANDS = (
    _Command("help"),
    _Command("status"),
    _Command("provider", "PROFILE"),
    _Command("model", "PROFILE"),
    _Command("effort", "LEVEL", ("low", "medium", "high", "xhigh", "ultracode")),
    _Command("reasoning", "LEVEL", ("low", "medium", "high", "xhigh", "ultracode")),
    _Command("mode", "MODE", ("normal", "accept-edits", "plan", "auto")),
    _Command("plan", "DESCRIPTION"),
    _Command("view-plan"),
    _Command("show-plan"),
    _Command("comment-plan", "STEP COMMENT"),
    _Command("plan-comment", "STEP COMMENT"),
    _Command("execute-plan"),
    _Command("run-plan"),
    _Command("sessions", "QUERY"),
    _Command("resume", "SESSION_ID"),
    _Command("rename", "TITLE"),
    _Command("title", "TITLE"),
    _Command("tasks"),
    _Command("view-task", "TASK_ID"),
    _Command("settings"),
    _Command("setting"),
    _Command("cancel"),
    _Command("clear"),
    _Command("quit"),
    _Command("exit"),
)


def slash_completions(
    value: str,
    *,
    provider_names: tuple[str, ...] = (),
) -> tuple[SlashCompletion, ...]:
    """Return deterministic slash-command completions for the current draft."""

    if not value.startswith("/") or "\n" in value or "\r" in value:
        return ()
    normalized = value.casefold()
    command_matches = tuple(command for command in _COMMANDS if command.base.startswith(normalized))
    exact = next((command for command in command_matches if command.base == normalized), None)
    if command_matches and (exact is None or exact.parameter is None):
        return tuple(SlashCompletion(command.base, command.syntax) for command in command_matches)

    command_name, separator, argument = normalized.partition(" ")
    command = next((item for item in _COMMANDS if item.base == command_name), None)
    if command is None or command.parameter is None:
        return ()

    choices = command.choices
    if command.name in {"model", "provider"}:
        choices = provider_names
    if choices:
        prefix = argument if separator else ""
        matches = tuple(choice for choice in choices if choice.casefold().startswith(prefix))
        return tuple(
            SlashCompletion(f"{command.base} {choice}", f"{command.base} {choice}")
            for choice in matches
        )

    if separator and argument:
        return ()
    return (SlashCompletion(f"{command.base} ", command.syntax),)


__all__ = ["SlashCompletion", "slash_completions"]
