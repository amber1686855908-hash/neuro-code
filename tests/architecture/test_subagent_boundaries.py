from __future__ import annotations

import ast
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _PROJECT_ROOT / "src" / "neuro_code"
_COMPOSITION_PATH = _SOURCE_ROOT / "bootstrap" / "composition.py"


def _called_symbol(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


class _LegacySubagentBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._function_names: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_names.append(node.name)
        self.generic_visit(node)
        self._function_names.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function_names.append(node.name)
        self.generic_visit(node)
        self._function_names.pop()

    def visit_Call(self, node: ast.Call) -> None:
        symbol = _called_symbol(node.func)
        if symbol == "bind_subagent_executor":
            raise AssertionError(f"production code calls the legacy subagent seam: {self._path}")
        if symbol == "SubagentExecutionService":
            assert self._path == _COMPOSITION_PATH
            assert self._function_names[-1:] == ["bind_subagent_executor"]
        assert all(keyword.arg != "_test_only" for keyword in node.keywords), self._path
        self.generic_visit(node)


def test_production_cannot_enable_legacy_subagent_executor() -> None:
    """Keep the arbitrary executor seam outside production child authority."""

    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _LegacySubagentBoundaryVisitor(path).visit(tree)
