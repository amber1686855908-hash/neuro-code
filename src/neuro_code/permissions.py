from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from neuro_code.bash_commands import analyze_bash_command


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(StrEnum):
    DEFAULT = "default"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"
    ACCEPT_EDITS = "acceptEdits"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    effect: PermissionEffect
    pattern: str

    def matches(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        subject = tool_name
        if tool_name == "bash":
            command = arguments.get("command", "")
            subject = f"bash:{command}" if isinstance(command, str) else "bash:"
        return fnmatch.fnmatchcase(subject, self.pattern)

    def matches_subject(self, subject: str) -> bool:
        return fnmatch.fnmatchcase(subject, self.pattern)

    def targets_bash(self) -> bool:
        if self.pattern.startswith("bash:"):
            return True
        if ":" in self.pattern:
            prefix = self.pattern.split(":", 1)[0]
            return fnmatch.fnmatchcase("bash", prefix)
        return fnmatch.fnmatchcase("bash", self.pattern) or fnmatch.fnmatchcase(
            "bash:any", self.pattern
        )


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    effect: PermissionEffect
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW


class PermissionManager:
    """Deterministic permission policy for interactive and headless callers.

    Explicit deny rules always win. An interactive surface may handle ASK;
    headless callers receive a denial for unresolved prompts.
    """

    _READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "grep"})
    _EDIT_TOOLS = frozenset({"search_replace", "apply_patch"})

    def __init__(
        self,
        *,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rules: tuple[PermissionRule, ...] = (),
        interactive: bool = False,
    ) -> None:
        self._mode = mode
        self._rules = rules
        self._interactive = interactive

    def decide(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        side_effecting: bool,
    ) -> PermissionDecision:
        if tool_name == "bash":
            explicit = self._decide_bash(arguments)
            if explicit is not None:
                return explicit
            matches: list[PermissionRule] = []
        else:
            matches = [rule for rule in self._rules if rule.matches(tool_name, arguments)]
        for effect in (PermissionEffect.DENY, PermissionEffect.ASK, PermissionEffect.ALLOW):
            if any(rule.effect is effect for rule in matches):
                if effect is PermissionEffect.ASK and not self._interactive:
                    return PermissionDecision(PermissionEffect.DENY, "headless mode cannot prompt")
                return PermissionDecision(effect, f"matched explicit {effect.value} rule")

        if tool_name in self._READ_ONLY_TOOLS or not side_effecting:
            return PermissionDecision(PermissionEffect.ALLOW, "built-in read-only tool")
        if self._mode is PermissionMode.BYPASS:
            return PermissionDecision(PermissionEffect.ALLOW, "bypassPermissions mode")
        if self._mode is PermissionMode.ACCEPT_EDITS and tool_name in self._EDIT_TOOLS:
            return PermissionDecision(PermissionEffect.ALLOW, "acceptEdits mode")
        if self._mode is PermissionMode.DONT_ASK:
            return PermissionDecision(PermissionEffect.DENY, "dontAsk denies unmatched actions")
        if self._interactive:
            return PermissionDecision(PermissionEffect.ASK, "interactive approval required")
        return PermissionDecision(PermissionEffect.DENY, "headless approval required")

    def _decide_bash(self, arguments: Mapping[str, Any]) -> PermissionDecision | None:
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        rules = [rule for rule in self._rules if rule.targets_bash()]
        if not rules:
            return None
        analysis = analyze_bash_command(command)
        restrictive = any(
            rule.effect in {PermissionEffect.DENY, PermissionEffect.ASK} for rule in rules
        )
        if not analysis.complete:
            if restrictive:
                if self._interactive:
                    return PermissionDecision(
                        PermissionEffect.ASK,
                        "bash command could not be safely decomposed",
                    )
                return PermissionDecision(
                    PermissionEffect.DENY,
                    "bash command could not be safely decomposed in headless mode",
                )
            return None

        matches_by_segment: list[list[PermissionRule]] = []
        for segment in analysis.segments:
            matches_by_segment.append(
                [
                    rule
                    for rule in rules
                    if any(rule.matches_subject(f"bash:{form}") for form in segment.forms)
                ]
            )

        all_matches = [rule for matches in matches_by_segment for rule in matches]
        if any(rule.effect is PermissionEffect.DENY for rule in all_matches):
            return PermissionDecision(
                PermissionEffect.DENY,
                "matched explicit deny rule in bash command sequence",
            )
        if any(rule.effect is PermissionEffect.ASK for rule in all_matches):
            if not self._interactive:
                return PermissionDecision(PermissionEffect.DENY, "headless mode cannot prompt")
            return PermissionDecision(
                PermissionEffect.ASK,
                "matched explicit ask rule in bash command sequence",
            )
        if matches_by_segment and all(
            any(rule.effect is PermissionEffect.ALLOW for rule in matches)
            for matches in matches_by_segment
        ):
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "every bash command segment matched an explicit allow rule",
            )
        return None
