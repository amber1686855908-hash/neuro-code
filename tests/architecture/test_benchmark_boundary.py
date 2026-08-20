from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "neuro_code"
BENCHMARK = ROOT / "scripts" / "benchmark"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_production_package_does_not_import_benchmark_boundary() -> None:
    violations: list[str] = []
    for path in SOURCE.rglob("*.py"):
        if any(
            module.startswith("scripts.benchmark") or module == "benchmark"
            for module in _imports(path)
        ):
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_benchmark_boundary_is_outside_production_package() -> None:
    assert BENCHMARK.is_dir()
    assert not BENCHMARK.is_relative_to(SOURCE)
    assert (BENCHMARK / "__main__.py").is_file()


def test_benchmark_uses_composition_not_tui_automation() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in BENCHMARK.glob("*.py"))
    assert "ApplicationComposition" in source
    assert "ConversationRunner" in source or "binding.runner.run" in source
    assert "neuro_code.tui" not in source
    assert "textual" not in source
