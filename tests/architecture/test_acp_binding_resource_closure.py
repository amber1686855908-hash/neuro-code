from __future__ import annotations

import ast
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACP_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "agent.py"
_SESSION_RUNTIME_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "session.py"


def _is_direct_binding_background_shutdown(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "shutdown"
        and isinstance(function.value, ast.Attribute)
        and function.value.attr == "background_tasks"
        and isinstance(function.value.value, ast.Name)
        and function.value.value.id == "binding"
    )


def _is_binding_close(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "close"
        and isinstance(function.value, ast.Name)
        and function.value.id == "binding"
    )


def test_acp_uses_binding_close_as_the_resource_cleanup_authority() -> None:
    """ACP owns lifetime decisions but must not bypass binding-owned resources."""

    source = _ACP_PATH.read_text(encoding="utf-8")
    runtime_source = _SESSION_RUNTIME_PATH.read_text(encoding="utf-8")
    trees = (
        ast.parse(source, filename=str(_ACP_PATH)),
        ast.parse(runtime_source, filename=str(_SESSION_RUNTIME_PATH)),
    )

    assert not any(
        isinstance(node, ast.Call) and _is_direct_binding_background_shutdown(node)
        for tree in trees
        for node in ast.walk(tree)
    )
    assert (
        sum(
            isinstance(node, ast.Call) and _is_binding_close(node)
            for tree in trees
            for node in ast.walk(tree)
        )
        >= 1
    )
    assert "await asyncio.shield(binding.close())" in runtime_source
    assert "background_tasks.shutdown" not in runtime_source
    assert "resource_scope" not in source
