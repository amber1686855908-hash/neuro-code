"""Conservative Bash command decomposition for permission decisions.

提供用于权限决策的保守 Bash 命令分解."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_ENV_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_SENSITIVE_ENV = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "GIT_EXEC_PATH",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PATH",
        "PYTHONPATH",
        "SHELLOPTS",
    }
)
_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_WRAPPERS = frozenset({"timeout", "nice", "ionice", "chrt", "stdbuf", "env"})

__all__ = [
    "BashCommandAnalysis",
    "BashCommandFamily",
    "BashCommandSegment",
    "analyze_bash_command",
    "classify_bash_command_family",
]


class BashCommandFamily(StrEnum):
    """Conservative command families eligible for a scoped grant."""

    TEST = "test"
    STATIC_CHECK = "static_check"
    GIT_READ = "git_read"


@dataclass(frozen=True, slots=True)
class BashCommandSegment:
    """Equivalent command forms that belong to one shell segment.

    表示属于同一个 Shell 片段的等价命令形式."""

    forms: tuple[str, ...]
    words: tuple[str, ...] = ()
    contains_assignment: bool = False


@dataclass(frozen=True, slots=True)
class BashCommandAnalysis:
    segments: tuple[BashCommandSegment, ...]
    complete: bool


def _tokenize(script: str) -> list[str] | None:
    if "\n" in script or "\r" in script:
        return None
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    word_started = False
    index = 0

    def flush() -> None:
        nonlocal current, word_started
        if word_started:
            tokens.append("".join(current))
            current = []
            word_started = False

    while index < len(script):
        char = script[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                current.append(char)
            word_started = True
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char == "\\":
                index += 1
                if index >= len(script):
                    return None
                current.append(script[index])
            elif char in {"$", "`"}:
                return None
            else:
                current.append(char)
            word_started = True
            index += 1
            continue

        if char.isspace():
            flush()
            index += 1
            continue
        if char == "#" and not word_started:
            break
        if char in {"'", '"'}:
            quote = char
            word_started = True
            index += 1
            continue
        if char == "\\":
            index += 1
            if index >= len(script):
                return None
            current.append(script[index])
            word_started = True
            index += 1
            continue
        if char in {"$", "`", "(", ")", "<", ">"}:
            return None
        if char in {"&", "|", ";"}:
            flush()
            pair = script[index : index + 2]
            if pair in {"&&", "||"}:
                tokens.append(pair)
                index += 2
                continue
            if char in {"|", ";"}:
                tokens.append(char)
                index += 1
                continue
            return None
        current.append(char)
        word_started = True
        index += 1

    if quote is not None:
        return None
    flush()
    return tokens


def _split_segments(tokens: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = []
    current: list[str] = []
    last_separator: str | None = None
    for token in tokens:
        if token in _SEPARATORS:
            if not current:
                return None
            segments.append(current)
            current = []
            last_separator = token
        else:
            current.append(token)
            last_separator = None
    if current:
        segments.append(current)
    elif last_separator != ";":
        return None
    return segments or None


def _strip_assignments(words: list[str]) -> list[str] | None:
    index = 0
    while index < len(words):
        matched = _ENV_ASSIGNMENT.fullmatch(words[index])
        if matched is None:
            break
        if matched.group(1) in _SENSITIVE_ENV or matched.group(1).startswith("DYLD_"):
            return None
        index += 1
    stripped = words[index:]
    return stripped or None


def _basename(command: str) -> str:
    return command.replace("\\", "/").rsplit("/", 1)[-1]


def _env_inner_index(words: list[str]) -> int | None:
    index = 1
    options_ended = False
    while index < len(words):
        token = words[index]
        if token == "--":
            index += 1
            options_ended = True
            break
        if token != "-" and token.startswith("-"):
            if token in {"-C", "--chdir", "-u", "--unset", "-S", "--split-string"}:
                index += 2
            else:
                index += 1
            continue
        if _ENV_ASSIGNMENT.fullmatch(token):
            matched = _ENV_ASSIGNMENT.fullmatch(token)
            assert matched is not None
            if matched.group(1) in _SENSITIVE_ENV or matched.group(1).startswith("DYLD_"):
                return None
            index += 1
            continue
        break
    if options_ended:
        while index < len(words):
            matched = _ENV_ASSIGNMENT.fullmatch(words[index])
            if matched is None:
                break
            if matched.group(1) in _SENSITIVE_ENV or matched.group(1).startswith("DYLD_"):
                return None
            index += 1
    return index if index < len(words) else None


def _unwrap_once(words: list[str]) -> tuple[bool, list[str] | None]:
    if not words or _basename(words[0]) not in _WRAPPERS:
        return False, None
    wrapper = _basename(words[0])
    index = 1
    if wrapper == "env":
        inner_index = _env_inner_index(words)
        return True, words[inner_index:] if inner_index is not None else None
    if wrapper == "timeout":
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in {"-k", "-s", "--kill-after", "--signal"} else 1
        if index >= len(words):
            return True, None
        index += 1
    elif wrapper == "nice":
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in {"-n", "--adjustment"} else 1
    elif wrapper == "ionice":
        options_with_values = {
            "-c",
            "-n",
            "-p",
            "-P",
            "-u",
            "--class",
            "--classdata",
            "--pid",
            "--pgid",
            "--uid",
        }
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in options_with_values else 1
    elif wrapper == "chrt":
        while index < len(words) and words[index].startswith("-"):
            index += 1
        if index >= len(words):
            return True, None
        index += 1
    elif wrapper == "stdbuf":
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in {"-i", "-o", "-e"} else 1
    return True, words[index:] or None


def _unwrap_wrappers(words: list[str]) -> list[str] | None:
    current = words
    for _ in range(8):
        is_wrapper, inner = _unwrap_once(current)
        if not is_wrapper:
            break
        if inner is None:
            return None
        current = inner
    return current


def _shell_script(words: list[str]) -> str | None:
    if not words or _basename(words[0]) not in {"bash", "sh", "dash", "zsh", "ksh"}:
        return None
    command_flag: int | None = None
    for index, word in enumerate(words[1:], start=1):
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            command_flag = index
            break
    if command_flag is None:
        return None
    for word in words[command_flag + 1 :]:
        if word in {"--", "-"}:
            continue
        if not word.startswith("-"):
            return word
    return None


def _analyze(script: str, depth: int) -> BashCommandAnalysis:
    if depth >= 8:
        return BashCommandAnalysis((), False)
    tokens = _tokenize(script)
    if tokens is None:
        return BashCommandAnalysis((), False)
    split = _split_segments(tokens)
    if split is None:
        return BashCommandAnalysis((), False)

    analyzed: list[BashCommandSegment] = []
    for raw_words in split:
        words = _strip_assignments(raw_words)
        if words is None:
            return BashCommandAnalysis(tuple(analyzed), False)
        unwrapped = _unwrap_wrappers(words)
        if unwrapped is None:
            return BashCommandAnalysis(tuple(analyzed), False)
        forms = [" ".join(words)]
        if unwrapped != words:
            forms.append(" ".join(unwrapped))
        analyzed.append(
            BashCommandSegment(
                tuple(forms),
                tuple(words),
                len(words) != len(raw_words),
            )
        )
        nested = _shell_script(unwrapped)
        if nested is not None:
            nested_analysis = _analyze(nested, depth + 1)
            analyzed.extend(nested_analysis.segments)
            if not nested_analysis.complete:
                return BashCommandAnalysis(tuple(analyzed), False)
    return BashCommandAnalysis(tuple(analyzed), True)


def analyze_bash_command(script: str) -> BashCommandAnalysis:
    """Conservatively split a Bash script for permission evaluation.

    以保守方式拆分 Bash 脚本,供权限评估使用."""

    return _analyze(script, 0)


_PYTHON_EXECUTABLE = re.compile(r"python(?:3(?:\.\d+)?)?\Z")
_UNSAFE_ARGUMENT_CHARS = frozenset("$`<>|;&(){}[]*?")
_GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "rev-parse", "branch"})
_GIT_UNSAFE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--git-dir",
        "--no-index",
        "--output",
        "--textconv",
        "--upload-pack",
        "--work-tree",
    }
)


def _safe_family_argument(value: str) -> bool:
    if not value or "\x00" in value or any(char in value for char in _UNSAFE_ARGUMENT_CHARS):
        return False
    if value.startswith(("/", "~")):
        return False
    path_like = value.replace("\\", "/")
    if any(part == ".." for part in path_like.split("/")):
        return False
    if "=" in value:
        _option, option_value = value.split("=", 1)
        if option_value.startswith(("/", "~")):
            return False
        if any(part == ".." for part in option_value.replace("\\", "/").split("/")):
            return False
    return True


def _single_plain_command(script: str) -> tuple[str, ...] | None:
    analysis = analyze_bash_command(script)
    if not analysis.complete or len(analysis.segments) != 1:
        return None
    segment = analysis.segments[0]
    # A second form means a wrapper was removed.  Nested shell commands add
    # additional segments.  Neither can be safely represented by one family.
    if len(segment.forms) != 1 or segment.contains_assignment or not segment.words:
        return None
    if not all(_safe_family_argument(word) for word in segment.words):
        return None
    return segment.words


def _python_module_command(words: tuple[str, ...]) -> tuple[str, ...] | None:
    if len(words) >= 3 and _PYTHON_EXECUTABLE.fullmatch(words[0]) and words[1] == "-m":
        return words[2:]
    return None


def _uv_run_command(words: tuple[str, ...]) -> tuple[str, ...] | None:
    if len(words) >= 3 and words[:2] == ("uv", "run"):
        return words[2:]
    return None


def _classify_test(words: tuple[str, ...]) -> bool:
    if words[0:1] == ("pytest",):
        return True
    module = _python_module_command(words)
    if module is not None and module[0:1] == ("pytest",):
        return True
    uv = _uv_run_command(words)
    if uv is None:
        return False
    if uv[0:1] == ("pytest",):
        return True
    module = _python_module_command(uv)
    return module is not None and module[0:1] == ("pytest",)


def _classify_static_check(words: tuple[str, ...]) -> bool:
    candidates = [words]
    uv = _uv_run_command(words)
    if uv is not None:
        candidates.append(uv)
    for candidate in tuple(candidates):
        module = _python_module_command(candidate)
        if module is not None:
            candidates.append(module)
    for candidate in candidates:
        if not candidate:
            continue
        if candidate[0] == "mypy":
            return True
        if candidate[0] != "ruff" or len(candidate) < 2:
            continue
        if candidate[1] == "check":
            return not any(
                value in {"--fix", "--fix-only", "--unsafe-fixes"}
                or value.startswith(("--fix=", "--fix-only=", "--unsafe-fixes="))
                for value in candidate[2:]
            )
        if candidate[1] == "format":
            return "--check" in candidate[2:] and not any(
                value in {"--fix", "--fix-only", "--unsafe-fixes"}
                or value.startswith(("--fix=", "--fix-only=", "--unsafe-fixes="))
                for value in candidate[2:]
            )
    return False


def _classify_git_read(words: tuple[str, ...]) -> bool:
    if len(words) < 2 or words[0] != "git" or words[1] not in _GIT_READ_SUBCOMMANDS:
        return False
    subcommand = words[1]
    arguments = words[2:]
    if any(
        value in _GIT_UNSAFE_OPTIONS
        or value.startswith(
            (
                "--config-env=",
                "--exec-path=",
                "--git-dir=",
                "--output=",
                "--upload-pack=",
                "--work-tree=",
            )
        )
        for value in arguments
    ):
        return False
    if subcommand == "branch":
        return arguments == ("--show-current",)
    return True


def classify_bash_command_family(script: str) -> BashCommandFamily | None:
    """Return a family only for one plain, bounded, read-only command shape.

    The classifier intentionally rejects wrappers, shell composition, nested
    interpreters, assignments, absolute/parent paths, and unsafe options.  A
    caller can still grant the exact action when this returns ``None``.
    """

    words = _single_plain_command(script)
    if words is None:
        return None
    if _classify_test(words):
        return BashCommandFamily.TEST
    if _classify_static_check(words):
        return BashCommandFamily.STATIC_CHECK
    if _classify_git_read(words):
        return BashCommandFamily.GIT_READ
    return None
