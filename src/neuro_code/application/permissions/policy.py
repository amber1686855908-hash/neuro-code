"""Application-owned deterministic permission policy.

定义由应用层拥有的确定性权限策略."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from neuro_code.application.permissions.scopes import (
    PermissionCommandFamily,
    PermissionScopeCandidate,
    PermissionScopeKind,
)
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessTarget,
)
from neuro_code.domain.permissions.bash_commands import (
    analyze_bash_command,
    classify_bash_command_family,
)

__all__ = [
    "PermissionDecision",
    "PermissionDecisionSource",
    "PermissionEffect",
    "PermissionManager",
    "PermissionMode",
    "PermissionRule",
    "PermissionRuleStore",
]


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDecisionSource(StrEnum):
    """Identify which trusted policy layer produced a decision.

    标识一个权限决定由哪个可信策略层产生. 只有默认的 interactive ASK
    才能产生本轮的候选范围;显式规则和模式决定不会被范围授权绕过.
    """

    DEFAULT = "default"
    EXPLICIT_RULE = "explicit_rule"
    MODE = "mode"


class PermissionMode(StrEnum):
    DEFAULT = "default"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"
    ACCEPT_EDITS = "acceptEdits"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    effect: PermissionEffect
    pattern: str
    path_pattern: str | None = None
    operation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect, PermissionEffect):
            raise TypeError("permission rule effect must be a PermissionEffect")
        if not isinstance(self.pattern, str) or not self.pattern.strip():
            raise ValueError("permission rule pattern must be non-empty")
        if self.path_pattern is not None and not self.path_pattern.strip():
            raise ValueError("permission rule path pattern must be non-empty")
        if self.operation is not None and not self.operation.strip():
            raise ValueError("permission rule operation must be non-empty")

    def matches(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        if self.operation is not None:
            requested_operation = arguments.get("operation", tool_name)
            if not isinstance(requested_operation, str) or not fnmatch.fnmatchcase(
                requested_operation,
                self.operation,
            ):
                return False
        paths = _argument_paths(arguments)
        if self.path_pattern is not None and not any(
            fnmatch.fnmatchcase(path, self.path_pattern) for path in paths
        ):
            return False
        subject = tool_name
        if tool_name == "bash":
            command = arguments.get("command", "")
            subject = f"bash:{command}" if isinstance(command, str) else "bash:"
        subjects = [subject]
        subjects.extend(f"{tool_name}:{path}" for path in paths)
        subjects.extend(f"path:{path}" for path in paths)
        return any(fnmatch.fnmatchcase(candidate, self.pattern) for candidate in subjects)

    def matches_target(self, tool_name: str, target: FilesystemAccessTarget) -> bool:
        """Match one canonical target without consulting its raw spelling."""

        if self.operation is not None and not _path_or_text_match(
            target.operation.value,
            self.operation,
        ):
            return False
        if self.path_pattern is not None and not _path_or_text_match(
            target.policy_path,
            self.path_pattern,
        ):
            return False
        subjects = (
            tool_name,
            f"{tool_name}:{target.policy_path}",
            f"path:{target.policy_path}",
        )
        return any(_path_or_text_match(subject, self.pattern) for subject in subjects)

    def matches_tool_operation(self, tool_name: str, target: FilesystemAccessTarget) -> bool:
        """Return whether this rule could apply before its path qualifier."""

        if self.operation is not None and not _path_or_text_match(
            target.operation.value,
            self.operation,
        ):
            return False
        prefix, separator, _suffix = self.pattern.partition(":")
        return (
            _path_or_text_match(tool_name, self.pattern)
            or (bool(separator) and _path_or_text_match(tool_name, prefix))
            or prefix == "path"
        )

    @property
    def is_path_scoped(self) -> bool:
        return self.path_pattern is not None or ":" in self.pattern

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

    def to_dict(self) -> dict[str, str]:
        result = {"effect": self.effect.value, "pattern": self.pattern}
        if self.path_pattern is not None:
            result["path_pattern"] = self.path_pattern
        if self.operation is not None:
            result["operation"] = self.operation
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PermissionRule:
        effect = value.get("effect")
        pattern = value.get("pattern")
        if not isinstance(effect, str) or not isinstance(pattern, str):
            raise ValueError("permission rule requires effect and pattern")
        try:
            parsed_effect = PermissionEffect(effect)
        except ValueError as error:
            raise ValueError("permission rule effect is invalid") from error
        path_pattern = value.get("path_pattern")
        operation = value.get("operation")
        if path_pattern is not None and not isinstance(path_pattern, str):
            raise ValueError("permission rule path_pattern must be text")
        if operation is not None and not isinstance(operation, str):
            raise ValueError("permission rule operation must be text")
        return cls(parsed_effect, pattern, path_pattern, operation)


class PermissionRuleStore:
    """Load and save bounded permission rules without storing credentials."""

    schema_version = 1

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)

    def load(self) -> tuple[PermissionRule, ...]:
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("permission rule file is unreadable") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != self.schema_version:
            raise ValueError("permission rule file schema is unsupported")
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list) or len(raw_rules) > 512:
            raise ValueError("permission rule file has too many rules")
        rules: list[PermissionRule] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, Mapping):
                raise ValueError("permission rule entry is invalid")
            rules.append(PermissionRule.from_dict(raw_rule))
        return tuple(rules)

    def save(self, rules: tuple[PermissionRule, ...] | list[PermissionRule]) -> None:
        normalized = tuple(rules)
        if len(normalized) > 512 or not all(
            isinstance(rule, PermissionRule) for rule in normalized
        ):
            raise ValueError("permission rules exceed the bounded store contract")
        payload = {
            "schema_version": self.schema_version,
            "rules": [rule.to_dict() for rule in normalized],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    effect: PermissionEffect
    reason: str
    source: PermissionDecisionSource = PermissionDecisionSource.DEFAULT

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW


class PermissionManager:
    """Deterministic permission policy for interactive and headless callers.

    Explicit deny rules always win. An interactive surface may handle ASK;
    headless callers receive a denial for unresolved prompts.

    为交互式和无头调用方提供确定性权限策略. 显式拒绝规则始终优先,无头调用方对未解决的询问直接拒绝.
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

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return self._rules

    def replace_rules(self, rules: tuple[PermissionRule, ...]) -> None:
        if not all(isinstance(rule, PermissionRule) for rule in rules):
            raise TypeError("rules must contain PermissionRule values")
        self._rules = tuple(rules)

    def load_rules(self, store: PermissionRuleStore) -> None:
        self.replace_rules(store.load())

    def save_rules(self, store: PermissionRuleStore) -> None:
        store.save(self._rules)

    def set_mode(self, mode: PermissionMode) -> None:
        if not isinstance(mode, PermissionMode):
            raise TypeError("permission mode must be a PermissionMode")
        self._mode = mode

    def scope_candidates(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        decision: PermissionDecision,
        targets: tuple[FilesystemAccessTarget, ...] = (),
        workspace_root: Path | None = None,
    ) -> tuple[PermissionScopeCandidate, ...]:
        """Build only safe broad-scope candidates for one unresolved ASK.

        Candidate generation is deliberately downstream of policy evaluation
        and upstream of the UI.  It is never based on a model-provided scope
        string.  Explicit rules, mode decisions, headless decisions, and
        anything other than the ordinary interactive ASK produce no broad
        candidate.

        为一个未解决的 ASK 生成安全的宽范围候选. 候选生成发生在策略评估之后、UI
        之前,绝不读取模型伪造的范围字符串. 显式规则、模式决定、无头决定以及非默认
        interactive ASK 都不会产生宽范围候选.
        """

        if (
            not isinstance(decision, PermissionDecision)
            or decision.effect is not PermissionEffect.ASK
            or decision.source is not PermissionDecisionSource.DEFAULT
            or not self._interactive
        ):
            return ()

        root = _normalized_scope_path(workspace_root)
        if root is None:
            return ()

        if tool_name in self._EDIT_TOOLS and targets:
            if all(
                target.is_primary_workspace
                and target.owning_workspace_root == root
                and not target.contains_link_like_component
                and target.operation
                in {
                    FilesystemAccessOperation.CREATE,
                    FilesystemAccessOperation.UPDATE,
                }
                and not _protected_scope_path(target.policy_path)
                for target in targets
            ):
                return (
                    PermissionScopeCandidate(
                        PermissionScopeKind.WORKSPACE_EDITS,
                        workspace_root=os.fspath(root),
                    ),
                )
            return ()

        if tool_name == "bash":
            command = arguments.get("command")
            if not isinstance(command, str):
                return ()
            family = classify_bash_command_family(command)
            if family is None:
                return ()
            return (
                PermissionScopeCandidate(
                    PermissionScopeKind.COMMAND_FAMILY,
                    workspace_root=os.fspath(root),
                    command_family=PermissionCommandFamily(family.value),
                ),
            )
        return ()

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
                    return PermissionDecision(
                        PermissionEffect.DENY,
                        "headless mode cannot prompt",
                        PermissionDecisionSource.EXPLICIT_RULE,
                    )
                return PermissionDecision(
                    effect,
                    f"matched explicit {effect.value} rule",
                    PermissionDecisionSource.EXPLICIT_RULE,
                )

        if tool_name in self._READ_ONLY_TOOLS or not side_effecting:
            return PermissionDecision(PermissionEffect.ALLOW, "built-in read-only tool")
        if self._mode is PermissionMode.BYPASS:
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "bypassPermissions mode",
                PermissionDecisionSource.MODE,
            )
        if self._mode is PermissionMode.ACCEPT_EDITS and tool_name in self._EDIT_TOOLS:
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "acceptEdits mode",
                PermissionDecisionSource.MODE,
            )
        if self._mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                PermissionEffect.DENY,
                "dontAsk denies unmatched actions",
                PermissionDecisionSource.MODE,
            )
        if self._interactive:
            return PermissionDecision(PermissionEffect.ASK, "interactive approval required")
        return PermissionDecision(PermissionEffect.DENY, "headless approval required")

    def decide_targets(
        self,
        tool_name: str,
        targets: tuple[FilesystemAccessTarget, ...],
        *,
        side_effecting: bool,
    ) -> PermissionDecision:
        """Authorize every canonical target independently, then aggregate.

        A path-scoped allow rule is an allowlist for the targets it covers;
        one matching target never authorizes an unrelated target in the same
        structured call.

        独立授权每个规范目标后再聚合. 路径范围 allow 规则只允许它覆盖的目标;一次
        调用中命中的一个目标不能替另一个无关目标授权.
        """

        if not targets:
            return PermissionDecision(PermissionEffect.DENY, "filesystem call has no targets")
        decisions = [
            self._decide_target(tool_name, target, side_effecting=side_effecting)
            for target in targets
        ]
        for effect in (PermissionEffect.DENY, PermissionEffect.ASK, PermissionEffect.ALLOW):
            matching = [decision for decision in decisions if decision.effect is effect]
            if not matching:
                continue
            if effect is PermissionEffect.DENY:
                return PermissionDecision(
                    PermissionEffect.DENY,
                    f"structured target authorization failed: {matching[0].reason}",
                    _combined_source(decisions),
                )
            if effect is PermissionEffect.ASK:
                if not self._interactive:
                    return PermissionDecision(
                        PermissionEffect.DENY,
                        "headless mode cannot prompt for every structured target",
                        _combined_source(decisions),
                    )
                return PermissionDecision(
                    PermissionEffect.ASK,
                    "one or more structured targets require approval",
                    _combined_source(decisions),
                )
        return PermissionDecision(
            PermissionEffect.ALLOW,
            "every structured target was authorized independently",
            _combined_source(decisions),
        )

    def _decide_target(
        self,
        tool_name: str,
        target: FilesystemAccessTarget,
        *,
        side_effecting: bool,
    ) -> PermissionDecision:
        matches = [rule for rule in self._rules if rule.matches_target(tool_name, target)]
        for effect in (PermissionEffect.DENY, PermissionEffect.ASK, PermissionEffect.ALLOW):
            if any(rule.effect is effect for rule in matches):
                if effect is PermissionEffect.ASK and not self._interactive:
                    return PermissionDecision(
                        PermissionEffect.DENY,
                        f"headless mode cannot prompt for target {target.policy_path!r}",
                        PermissionDecisionSource.EXPLICIT_RULE,
                    )
                return PermissionDecision(
                    effect,
                    f"target {target.policy_path!r} matched explicit {effect.value} rule",
                    PermissionDecisionSource.EXPLICIT_RULE,
                )

        if any(
            rule.effect is PermissionEffect.ALLOW
            and rule.is_path_scoped
            and rule.matches_tool_operation(tool_name, target)
            for rule in self._rules
        ):
            return PermissionDecision(
                PermissionEffect.DENY,
                f"target {target.policy_path!r} is outside explicit path allow rules",
                PermissionDecisionSource.EXPLICIT_RULE,
            )
        if not side_effecting:
            return PermissionDecision(PermissionEffect.ALLOW, "built-in read-only target")
        if self._mode is PermissionMode.BYPASS:
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "bypassPermissions mode",
                PermissionDecisionSource.MODE,
            )
        if self._mode is PermissionMode.ACCEPT_EDITS and tool_name in self._EDIT_TOOLS:
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "acceptEdits mode",
                PermissionDecisionSource.MODE,
            )
        if self._mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                PermissionEffect.DENY,
                f"dontAsk denies unmatched target {target.policy_path!r}",
                PermissionDecisionSource.MODE,
            )
        if self._interactive:
            return PermissionDecision(
                PermissionEffect.ASK,
                f"interactive approval required for target {target.policy_path!r}",
            )
        return PermissionDecision(
            PermissionEffect.DENY,
            f"headless approval required for target {target.policy_path!r}",
        )

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
                        PermissionDecisionSource.EXPLICIT_RULE,
                    )
                return PermissionDecision(
                    PermissionEffect.DENY,
                    "bash command could not be safely decomposed in headless mode",
                    PermissionDecisionSource.EXPLICIT_RULE,
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
                PermissionDecisionSource.EXPLICIT_RULE,
            )
        if any(rule.effect is PermissionEffect.ASK for rule in all_matches):
            if not self._interactive:
                return PermissionDecision(
                    PermissionEffect.DENY,
                    "headless mode cannot prompt",
                    PermissionDecisionSource.EXPLICIT_RULE,
                )
            return PermissionDecision(
                PermissionEffect.ASK,
                "matched explicit ask rule in bash command sequence",
                PermissionDecisionSource.EXPLICIT_RULE,
            )
        if matches_by_segment and all(
            any(rule.effect is PermissionEffect.ALLOW for rule in matches)
            for matches in matches_by_segment
        ):
            return PermissionDecision(
                PermissionEffect.ALLOW,
                "every bash command segment matched an explicit allow rule",
                PermissionDecisionSource.EXPLICIT_RULE,
            )
        return None


def _argument_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract only conventional file targets for path-scoped rules."""

    values: list[str] = []
    keys = frozenset({"path", "paths", "file", "files", "source", "target", "destination"})

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, str) and key in keys:
            if value and "\x00" not in value and len(value) <= 4_096:
                values.append(value)
            return
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                visit(nested_value, str(nested_key))
        elif isinstance(value, (list, tuple)):
            for nested_value in value:
                visit(nested_value, key)

    for name, value in arguments.items():
        visit(value, str(name))
    return tuple(dict.fromkeys(values))


def _path_or_text_match(value: str, pattern: str) -> bool:
    normalized_value = value.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/")
    if os.name == "nt":
        normalized_value = normalized_value.casefold()
        normalized_pattern = normalized_pattern.casefold()
    return fnmatch.fnmatchcase(normalized_value, normalized_pattern)


def _normalized_scope_path(value: Path | None) -> Path | None:
    if value is None or not isinstance(value, Path):
        return None
    try:
        result = value.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    return result if result.is_absolute() else None


def _protected_scope_path(policy_path: str) -> bool:
    """Keep broad edit grants away from Neuro and obvious secret targets."""

    normalized = policy_path.replace("\\", "/")
    parts = tuple(part.casefold() for part in normalized.split("/") if part not in {"", "."})
    protected_components = frozenset(
        {
            ".git",
            ".neuro",
            ".neuro-code",
            ".neuro-code-state",
            ".neuro-state",
            ".checkpoints",
            "checkpoint",
            "checkpoints",
            "internal",
        }
    )
    if any(part in protected_components for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {
        ".netrc",
        ".npmrc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }:
        return True
    return any(name.endswith(suffix) for suffix in (".jks", ".key", ".p12", ".pfx", ".pem"))


def _combined_source(decisions: list[PermissionDecision]) -> PermissionDecisionSource:
    if any(decision.source is PermissionDecisionSource.EXPLICIT_RULE for decision in decisions):
        return PermissionDecisionSource.EXPLICIT_RULE
    if any(decision.source is PermissionDecisionSource.MODE for decision in decisions):
        return PermissionDecisionSource.MODE
    return PermissionDecisionSource.DEFAULT
