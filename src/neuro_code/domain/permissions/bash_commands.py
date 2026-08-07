"""Conservative Bash command decomposition for permission decisions.

提供用于权限决策的保守 Bash 命令分解."""

from __future__ import annotations

import re
from dataclasses import dataclass

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

__all__ = ["BashCommandAnalysis", "BashCommandSegment", "analyze_bash_command"]


@dataclass(frozen=True, slots=True)
class BashCommandSegment:
    """Equivalent command forms that belong to one shell segment.

    表示属于同一个 Shell 片段的等价命令形式."""

    forms: tuple[str, ...]


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
        analyzed.append(BashCommandSegment(tuple(forms)))
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
