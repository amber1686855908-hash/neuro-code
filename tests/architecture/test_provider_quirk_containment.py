from __future__ import annotations

import ast
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _PROJECT_ROOT / "src" / "neuro_code"
_CORE_ROOTS = (
    _PACKAGE_ROOT / "application",
    _PACKAGE_ROOT / "domain",
    _PACKAGE_ROOT / "infrastructure" / "persistence",
    _PACKAGE_ROOT / "interfaces",
    _PACKAGE_ROOT / "tui.py",
)
_ALLOWED_PROVIDER_PRESENTATION = frozenset(
    {
        _PACKAGE_ROOT / "application" / "ports" / "provider_catalog.py",
        _PACKAGE_ROOT / "application" / "ports" / "provider_services.py",
        _PACKAGE_ROOT / "application" / "ports" / "provider_settings.py",
        _PACKAGE_ROOT / "tui_text.py",
    }
)
_VENDOR_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(kimi|moonshot|glm|zhipu|minimax|minimaxi|ark|volcengine|qianfan|"
    r"baidu|bailian|alibaba|tokenhub|tencent)"
    r"(?:[^a-z0-9]|$)"
)
_WIRE_MARKERS = frozenset(
    {
        "reasoning_details",
        "openai-chat-reasoning-details",
        "max_completion_tokens",
        "reasoning_split",
        "clear_thinking",
        "<|dsml|",
    }
)


def _production_files() -> tuple[Path, ...]:
    files = {
        path
        for root in _CORE_ROOTS
        for path in root.rglob("*.py")
        if path not in _ALLOWED_PROVIDER_PRESENTATION
    }
    return tuple(sorted(files))


def _docstring_constants(tree: ast.AST) -> set[int]:
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            skipped.add(id(first.value))
    return skipped


def _matches_vendor(value: str) -> bool:
    return _VENDOR_PATTERN.search(value.casefold()) is not None


def test_provider_vendor_and_wire_quirks_stay_out_of_core_layers() -> None:
    violations: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skipped_docstrings = _docstring_constants(tree)
        for node in ast.walk(tree):
            value: str | None = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in skipped_docstrings:
                    continue
                value = node.value
            elif isinstance(node, ast.Name):
                value = node.id
            elif isinstance(node, ast.Attribute):
                value = node.attr
            if value is None:
                continue
            marker = next(
                (candidate for candidate in _WIRE_MARKERS if candidate in value.casefold()),
                None,
            )
            if marker is not None or _matches_vendor(value):
                violations.append(f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno}: {value!r}")

    assert not violations, (
        "provider-specific dispatch or wire markers leaked into core:\n" + "\n".join(violations)
    )
